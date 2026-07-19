import hashlib
import logging
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace import LOVELACE_DATA
from homeassistant.components.lovelace.const import MODE_STORAGE
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.util import slugify

from homeassistant.components.sensor import SensorEntity
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD, CONF_SCAN_INTERVAL

from .store import CHARGE_LIMIT_DISABLED, VolvoStore
from .volvooncall_base import (
    DEFAULT_SCAN_INTERVAL,
    VolvoAuthError,
    VolvoAuthExpiredError,
    VolvoAuthThrottledError,
    redact_sensitive,
    vehicle_log_ref,
)
from .volvooncall_cn import VehicleAPI
from .volvooncall_cn import Vehicle
from .volvooncall_cn import DOMAIN
from .const import (
    CONF_POWERTRAIN_TYPE,
    DEFAULT_POWERTRAIN_TYPE,
    ELECTRIC_CONTROL_KEYS,
    ELECTRIC_SENSOR_KEYS,
    POWERTRAIN_HYBRID,
)

PLATFORMS = {
    "sensor": "sensor",
    "binary_sensor": "binary_sensor",
    "device_tracker": "device_tracker",
    "lock": "lock",
    "button": "button",
    "number": "number",
    "switch": "switch",
}

_LOGGER = logging.getLogger(__name__)

# How many consecutive failed polls may serve cached data before entities are
# marked unavailable via UpdateFailed.
FAILURE_GRACE_CYCLES = 5
# How many consecutive credential rejections trigger Home Assistant's
# reauthentication flow. More than one, because the server occasionally
# rejects a valid account transiently under risk control.
LOGIN_REJECTION_REAUTH_THRESHOLD = 3
# Hard ceiling for one update cycle. gRPC and REST calls carry their own 10s
# timeouts; this is only the safety net around the whole pass.
UPDATE_CYCLE_TIMEOUT = 60
AUTH_STORE_VERSION = 1
TOKEN_SAVE_DELAY_SECONDS = 5

FRONTEND_PATH = Path(__file__).parent / "frontend"
FRONTEND_URL_PATH = f"/{DOMAIN}/frontend"
CARD_RESOURCE_PATH = f"{FRONTEND_URL_PATH}/volvo-car-card.js"
CARD_RESOURCE_URL = f"{CARD_RESOURCE_PATH}?v=2.0.2"
CHARGING_CARD_RESOURCE_PATH = f"{FRONTEND_URL_PATH}/volvo-charging-card.js"
CHARGING_CARD_RESOURCE_URL = f"{CHARGING_CARD_RESOURCE_PATH}?v=1.0.0"
CARD_RESOURCES = (
    (CARD_RESOURCE_PATH, CARD_RESOURCE_URL),
    (CHARGING_CARD_RESOURCE_PATH, CHARGING_CARD_RESOURCE_URL),
)


async def _async_register_card_resource(hass: HomeAssistant) -> None:
    """Register the bundled Lovelace cards when storage resources are used."""
    lovelace = hass.data.get(LOVELACE_DATA)
    if lovelace is None:
        _LOGGER.warning(
            "Lovelace is not loaded; add %s as module resources manually",
            ", ".join(url for _, url in CARD_RESOURCES),
        )
        return

    if lovelace.resource_mode != MODE_STORAGE:
        _LOGGER.info(
            "Lovelace resources use YAML mode; add %s as module resources",
            ", ".join(url for _, url in CARD_RESOURCES),
        )
        return

    resources = lovelace.resources
    await resources.async_get_info()
    items = resources.async_items() or []
    for resource_path, resource_url in CARD_RESOURCES:
        existing = next(
            (
                item
                for item in items
                if item.get("url", "").split("?", 1)[0] == resource_path
            ),
            None,
        )
        if existing is None:
            await resources.async_create_item(
                {"res_type": "module", "url": resource_url}
            )
        elif existing.get("url") != resource_url:
            await resources.async_update_item(
                existing["id"],
                {"res_type": "module", "url": resource_url},
            )


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Serve and register the bundled Volvo Home Assistant card."""
    if not hasattr(hass, "http"):
        _LOGGER.warning(
            "Home Assistant HTTP component is unavailable; add %s manually",
            CARD_RESOURCE_URL,
        )
        return True

    await hass.http.async_register_static_paths(
        [StaticPathConfig(FRONTEND_URL_PATH, str(FRONTEND_PATH), True)]
    )
    await _async_register_card_resource(hass)
    return True


def remove_electric_entity_registry_entries(hass, config_entry_id):
    """Remove stale electric entities after switching to fuel."""
    registry = er.async_get(hass)
    electric_suffixes = tuple(
        f"-{key}" for key in ELECTRIC_SENSOR_KEYS + ELECTRIC_CONTROL_KEYS
    )
    for registry_entry in er.async_entries_for_config_entry(
        registry, config_entry_id
    ):
        if registry_entry.unique_id.endswith(electric_suffixes):
            registry.async_remove(registry_entry.entity_id)


def _auth_store_for(hass: HomeAssistant, username: str) -> Store:
    """Return the token store for an account (key hashed for privacy)."""
    digest = hashlib.sha256((username or "").encode()).hexdigest()[:16]
    return Store(
        hass,
        AUTH_STORE_VERSION,
        f"{DOMAIN}.auth_{digest}",
        private=True,
    )


async def _async_attach_token_store(
    hass: HomeAssistant, volvo_api: VehicleAPI, username: str
) -> None:
    """Persist session tokens so restarts refresh instead of re-logging in.

    Frequent password logins (e.g. across quick Home Assistant restarts) have
    been observed to trip Volvo's server-side risk control.
    """
    auth_store = _auth_store_for(hass, username)
    saved_tokens = await auth_store.async_load()
    if saved_tokens:
        volvo_api.import_tokens(saved_tokens)
    volvo_api.set_token_listener(
        lambda tokens: auth_store.async_delay_save(
            lambda: tokens, TOKEN_SAVE_DELAY_SECONDS
        )
    )


async def async_update_options(hass: HomeAssistant, config_entry: ConfigEntry):
    config_data = {**config_entry.data, **config_entry.options}
    entry_id = config_entry.entry_id

    username = config_data.get(CONF_USERNAME)
    password = config_data.get(CONF_PASSWORD)
    interval = config_data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    powertrain_type = config_data.get(
        CONF_POWERTRAIN_TYPE, DEFAULT_POWERTRAIN_TYPE
    )
    _LOGGER.info("new interval: %s", interval)
    hass.data.setdefault(DOMAIN, {})
    coordinator = hass.data[DOMAIN].get(entry_id)
    if coordinator is None:
        return

    if coordinator.powertrain_type != powertrain_type:
        if powertrain_type != POWERTRAIN_HYBRID:
            remove_electric_entity_registry_entries(hass, entry_id)
        await hass.config_entries.async_reload(entry_id)
        return

    coordinator.update_interval = timedelta(seconds=interval)

    # Only rebuild the API client when the credentials actually changed;
    # replacing it discards a healthy session and forces a fresh login.
    old_api = coordinator.volvo_api
    if (
        getattr(old_api, "_username", None) == username
        and getattr(old_api, "_password", None) == password
    ):
        return

    session = async_get_clientsession(hass)
    volvo_api = VehicleAPI(session=session, username=username, password=password)
    await _async_attach_token_store(hass, volvo_api, username)
    coordinator.volvo_api = volvo_api
    if hasattr(old_api, "close"):
        await old_api.close()


async def async_setup_entry(hass, entry):
    """Set up the integration from a config entry."""
    session = async_get_clientsession(hass)

    config_data = {**entry.data, **entry.options}
    username = config_data.get(CONF_USERNAME)
    password = config_data.get(CONF_PASSWORD)
    interval = config_data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    powertrain_type = config_data.get(
        CONF_POWERTRAIN_TYPE, DEFAULT_POWERTRAIN_TYPE
    )
    volvo_api = VehicleAPI(session=session, username=username, password=password)
    await _async_attach_token_store(hass, volvo_api, username)
    hass.data.setdefault(DOMAIN, {})
    coordinator = hass.data[DOMAIN][entry.entry_id] = VolvoCoordinator(
        hass, volvo_api, interval, powertrain_type
    )

    # Fetch initial data so we have data when entities subscribe
    #
    # If the refresh fails, async_config_entry_first_refresh will
    # raise ConfigEntryNotReady and setup will try again later
    #
    # If you do not want to retry setup on failure, use
    # coordinator.async_refresh() instead
    #
    if not entry.update_listeners:
        entry.add_update_listener(async_update_options)
    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_migrate_entry(hass, entry):
    """Default existing entries to hybrid to preserve electric entities."""
    if entry.version < 3:
        data = dict(entry.data)
        data.setdefault(CONF_POWERTRAIN_TYPE, DEFAULT_POWERTRAIN_TYPE)
        hass.config_entries.async_update_entry(entry, data=data, version=3)
    return True


async def async_unload_entry(hass, entry):
    """Unload an entry so powertrain changes can rebuild entities."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id, None)
        if coordinator is not None and hasattr(coordinator.volvo_api, "close"):
            await coordinator.volvo_api.close()
    return unload_ok


class VolvoCoordinator(DataUpdateCoordinator):
    """My custom coordinator."""

    def __init__(
        self,
        hass,
        volvo_api,
        scan_interval,
        powertrain_type=DEFAULT_POWERTRAIN_TYPE,
    ):
        """Initialize my coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            # Name of the data. For logging purposes.
            name="Volvo On Call CN sensor",
            # Polling interval. Will only be polled if there are subscribers.
            update_interval=timedelta(seconds=scan_interval),
        )
        self.volvo_api = volvo_api
        self.powertrain_type = powertrain_type
        self.supports_electric = powertrain_type == POWERTRAIN_HYBRID
        self.store_datas = []
        self._last_update_started_at = None
        self._force_next_refresh = False
        self._update_lock = asyncio.Lock()
        # Vehicles and stores are keyed by VIN and reused across polls, so the
        # per-source fallback cache inside Vehicle survives between cycles and
        # a partial outage degrades to last-known values instead of defaults.
        self._vehicles = {}
        self._vehicle_order = []
        self._stores = {}
        # Connection health tracking
        self._consecutive_failures = 0
        self._login_rejections = 0
        self._last_failure_reason = None

    async def _async_get_vehicle_map(self):
        """Fetch the account's vehicles, re-authenticating once on 401/403.

        The server sometimes revokes a session ahead of its expiry (risk
        control); without this the integration would keep replaying the dead
        token until the scheduled refresh window, which has been observed to
        take ~20 minutes.
        """
        try:
            return await self.volvo_api.get_vehicles_vins()
        except VolvoAuthExpiredError:
            _LOGGER.warning(
                "Session was revoked by the server; re-authenticating now"
            )
            # The rejected request already marked the token expired, so this
            # runs the refresh-token → password-login recovery chain.
            await self.volvo_api.update_token()
            return await self.volvo_api.get_vehicles_vins()

    def _vehicle_for(self, vin, vehicleInfos):
        """Return the reusable Vehicle for a VIN, creating it on first sight."""
        vehicle = self._vehicles.get(vin)
        if vehicle is None:
            modelYear = int(vehicleInfos.get("modelYear", 2020))
            isAaos = modelYear >= 2022
            vehicle = Vehicle(
                vin,
                self.volvo_api,
                isAaos,
                supports_electric=self.supports_electric,
                series_code=vehicleInfos.get("seriesCode"),
            )
            self._vehicles[vin] = vehicle
            self._vehicle_order.append(vin)
        else:
            # The API object may have been replaced after an options change.
            vehicle._api = self.volvo_api
        return vehicle

    async def _store_for(self, vin):
        """Return the persistent store for a VIN, loading it on first sight."""
        store = self._stores.get(vin)
        if store is None:
            store = VolvoStore(self.hass, vin)
            await store.load_create_data()
            self._stores[vin] = store
        return store

    def _handle_update_failure(self, err):
        """Serve cached data during short outages, go unavailable on long ones."""
        self._consecutive_failures += 1
        self._last_failure_reason = redact_sensitive(err)
        _LOGGER.error(
            "Coordinator update failed (failure #%s): %s",
            self._consecutive_failures,
            self._last_failure_reason,
        )

        if (
            self.data is not None
            and self._consecutive_failures <= FAILURE_GRACE_CYCLES
        ):
            _LOGGER.warning(
                "Returning cached vehicle data due to update failure (%s/%s "
                "before entities become unavailable)",
                self._consecutive_failures,
                FAILURE_GRACE_CYCLES,
            )
            return self.data

        # No data at all (first load) or the grace window is exhausted:
        # let entities show unavailable instead of stale values forever.
        raise UpdateFailed(
            f"Error communicating with API: {self._last_failure_reason}"
        ) from err

    async def _async_update_data(self):
        """Fetch data from API endpoint with caching and failure grading."""
        async with self._update_lock:
            now = datetime.now(timezone.utc)
            force_refresh = self._force_next_refresh
            self._force_next_refresh = False
            if (
                not force_refresh
                and self.data is not None
                and self._last_update_started_at is not None
                and self.update_interval is not None
                and now - self._last_update_started_at < self.update_interval
            ):
                _LOGGER.debug(
                    "Skipping Volvo refresh because the global scan interval "
                    "has not elapsed"
                )
                return self.data

            self._last_update_started_at = now

            try:
                async with asyncio.timeout(UPDATE_CYCLE_TIMEOUT):
                    await self.volvo_api.login()
                    await self.volvo_api.update_token()

                    vinVehicleMaps = await self._async_get_vehicle_map()
                    vehicles = []
                    store_datas = []

                    for vin, vehicleInfos in vinVehicleMaps.items():
                        vehicle = self._vehicle_for(vin, vehicleInfos)

                        # Try to update, but don't fail completely
                        try:
                            await vehicle.update()
                            vehicle._consecutive_failures = 0
                            # Note: _last_successful_update is updated by _save_to_cache() in each parse method
                        except Exception as err:
                            vehicle._consecutive_failures += 1
                            _LOGGER.error(
                                "Failed to update %s (failure #%d): %s",
                                vehicle_log_ref(vin),
                                vehicle._consecutive_failures,
                                redact_sensitive(err),
                            )
                            # Don't raise - continue with cached data

                        store_data = await self._store_for(vin)
                        if self.supports_electric:
                            await store_data.async_capture_full_charge_range(
                                vehicle.battery_charge_level_percentage,
                                vehicle.electric_range,
                                datetime.now(timezone.utc).isoformat(),
                                vehicle.charge_data_source,
                                vehicle.battery_charging_status,
                                vehicle.charging_power,
                            )
                            await self._async_enforce_charge_limit(
                                vehicle, store_data
                            )

                    # A vehicle missing from one listing (account hiccup, car
                    # resold) keeps its last object so entity indexes stay
                    # valid; it simply stops receiving updates.
                    for vin in self._vehicle_order:
                        if vin in self._vehicles:
                            vehicles.append(self._vehicles[vin])
                            store = self._stores.get(vin)
                            if store is None:
                                store = await self._store_for(vin)
                            store_datas.append(store)

                    # Track successful update
                    self._consecutive_failures = 0
                    self._login_rejections = 0
                    self.store_datas = store_datas
                    return vehicles

            except VolvoAuthThrottledError as err:
                # The local cooldown refused a password login; not a verdict
                # on the credentials, just wait for the next cycle.
                return self._handle_update_failure(err)
            except VolvoAuthError as err:
                self._login_rejections += 1
                if self._login_rejections >= LOGIN_REJECTION_REAUTH_THRESHOLD:
                    # Repeated explicit credential rejections: stop polling
                    # and ask the user to re-enter the password.
                    raise ConfigEntryAuthFailed(
                        redact_sensitive(err)
                    ) from err
                return self._handle_update_failure(err)
            except ConfigEntryAuthFailed:
                raise
            except Exception as err:
                return self._handle_update_failure(err)

    async def async_force_refresh(self):
        """Refresh immediately after a control command, bypassing throttling."""
        self._force_next_refresh = True
        try:
            await self.async_refresh()
        finally:
            # DataUpdateCoordinator can occasionally coalesce refresh calls.
            # Never let a force flag leak into a later scheduled poll.
            self._force_next_refresh = False

    async def _async_enforce_charge_limit(self, vehicle, store_data):
        """Stop an active home-charge session once the SoC limit is reached.

        The Volvo CN API has no native charge-limit setting, so the limit is
        enforced here on every poll: only home-pile sessions can be stopped
        remotely, and 100% means the limit is disabled. Resending the stop on
        the next poll while the pile still reports charging is the retry path
        for a stop command that has not taken effect yet.
        """
        limit = store_data.get_charge_limit()
        if limit >= CHARGE_LIMIT_DISABLED:
            return

        battery_level = vehicle.battery_charge_level_percentage
        try:
            battery_value = float(battery_level)
        except (TypeError, ValueError):
            return
        if battery_value < limit:
            return

        home_charge_status = str(vehicle.home_charge_status or "").lower()
        if home_charge_status not in ("starting", "charging"):
            return

        _LOGGER.info(
            "%s reached the configured %d%% limit; stopping home charge",
            vehicle_log_ref(vehicle.vin),
            limit,
        )
        try:
            await vehicle.stop_home_charge()
        except Exception as err:
            _LOGGER.warning(
                "Failed to stop home charge for %s at the %d%% limit: %s",
                vehicle_log_ref(vehicle.vin),
                limit,
                redact_sensitive(err),
            )

metaMap = {
    "car_lock": {
        "name": "Lock",
        "device_class": None,
        "icon": "",
        "unit": "",
        "entity_id": "lock",
    },
    "window_lock": {
        "name": "Winodw Lock",
        "device_class": None,
        "icon": "",
        "unit": "",
        "entity_id": "window_lock",
    },
    # "remote_door_unlock": {
    #    "name": "Remote Door Unlock",
    #    "device_class": "lock",
    #    "icon": "",
    #    "unit": "",
    # },
    "distance_to_empty": {
        "name": "Distance to empty",
        "device_class": None,
        "icon": "mdi:ruler",
        "unit": "km",
        "entity_id": "distance_to_empty",
        "state_class": "measurement",
    },
    "tail_gate_open": {
        "name": "Tail gate",
        "device_class": "door",
        "icon": "mdi:car-back",
        "unit": "",
        "entity_id": "tail_gate",
    },
    "rear_right_door_open": {
        "name": "Rear right door",
        "device_class": "door",
        "icon": "",
        "unit": "",
        "entity_id": "rear_right_door",
    },
    "rear_left_door_open": {
        "name": "Rear left door",
        "device_class": "door",
        "icon": "",
        "unit": "",
        "entity_id": "rear_left_door",
    },
    "front_right_door_open": {
        "name": "Front right door",
        "device_class": "door",
        "icon": "",
        "unit": "",
        "entity_id": "front_right_door",
    },
    "front_left_door_open": {
        "name": "Front left door",
        "device_class": "door",
        "icon": "",
        "unit": "",
        "entity_id": "front_left_door",
    },
    "hood_open": {
        "name": "Hood",
        "device_class": "door",
        "icon": "",
        "unit": "",
        "entity_id": "hood",
    },
    "sunroof_open": {
        "name": "Sunroof",
        "device_class": "window",
        "icon": "mdi:home-roof",
        "unit": "",
        "entity_id": "sunroof",
    },
    "engine_running": {
        "name": "Engine",
        "device_class": "power",
        "icon": "",
        "unit": "",
        "entity_id": "engine",
    },
    "odo_meter": {
        "name": "Odometer",
        "device_class": None,
        "icon": "mdi:speedometer",
        "unit": "km",
        "entity_id": "odometer",
        "state_class": "total_increasing",
    },
    "front_left_window_open": {
        "name": "Front left window",
        "device_class": "window",
        "icon": "",
        "unit": "",
        "entity_id": "front_left_window",
    },
    "front_right_window_open": {
        "name": "Front right window",
        "device_class": "window",
        "icon": "",
        "unit": "",
        "entity_id": "front_right_window",
    },
    "rear_left_window_open": {
        "name": "Rear left window",
        "device_class": "window",
        "icon": "",
        "unit": "",
        "entity_id": "rear_left_window",
    },
    "rear_right_window_open": {
        "name": "Rear right window",
        "device_class": "window",
        "icon": "",
        "unit": "",
        "entity_id": "rear_right_window",
    },
    "fuel_amount": {
        "name": "Fuel amount",
        "device_class": "volume_storage",
        "icon": "mdi:gas-station",
        "unit": "L",
        "entity_id": "fuel_amount",
        "state_class": "measurement",
    },
    "fuel_average_consumption_liters_per_100_km": {
        "name": "Fuel average consumption liters per 100 km",
        "device_class": None,
        "icon": "mdi:gas-station",
        "unit": "L/100km",
        "entity_id": "fuel_average_consumption_liters_per_100_km",
        "state_class": "measurement",
    },
    "tm_distance": {
        "name": "TM distance",
        "device_class": "distance",
        "icon": "mdi:map-marker-distance",
        "unit": "km",
        "entity_id": "tm_distance",
        "state_class": "measurement",
    },
    "tm_fuel_consumption": {
        "name": "TM fuel consumption",
        "device_class": None,
        "icon": "mdi:gas-station",
        "unit": "L/100km",
        "entity_id": "tm_fuel_consumption",
        "state_class": "measurement",
    },
    "tm_energy_consumption": {
        "name": "TM energy consumption",
        "device_class": None,
        "icon": "mdi:lightning-bolt",
        "unit": "kWh/100km",
        "entity_id": "tm_energy_consumption",
        "state_class": "measurement",
    },
    "tm_average_speed": {
        "name": "TM average speed",
        "device_class": "speed",
        "icon": "mdi:speedometer",
        "unit": "km/h",
        "entity_id": "tm_average_speed",
        "state_class": "measurement",
    },
    "ta_distance": {
        "name": "TA distance",
        "device_class": "distance",
        "icon": "mdi:map-marker-distance",
        "unit": "km",
        "entity_id": "ta_distance",
        "state_class": "measurement",
    },
    "ta_fuel_consumption": {
        "name": "TA fuel consumption",
        "device_class": None,
        "icon": "mdi:gas-station",
        "unit": "L/100km",
        "entity_id": "ta_fuel_consumption",
        "state_class": "measurement",
    },
    "ta_average_speed": {
        "name": "TA average speed",
        "device_class": "speed",
        "icon": "mdi:speedometer",
        "unit": "km/h",
        "entity_id": "ta_average_speed",
        "state_class": "measurement",
    },
    "battery_charge_level_percentage": {
        "name": "Battery charge level",
        "device_class": "battery",
        "icon": "mdi:battery",
        "unit": "%",
        "entity_id": "battery_charge_level",
        "state_class": "measurement",
    },
    "electric_range": {
        "name": "Electric range",
        "device_class": "distance",
        "icon": "mdi:map-marker-distance",
        "unit": "km",
        "entity_id": "electric_range",
        "state_class": "measurement",
    },
    "full_charge_electric_range": {
        "name": "Full charge electric range",
        "device_class": "distance",
        "icon": "mdi:battery-check",
        "unit": "km",
        "entity_id": "full_charge_electric_range",
        "state_class": "measurement",
    },
    "battery_charging_status": {
        "name": "Charging status",
        "device_class": None,
        "icon": "mdi:ev-station",
        "unit": None,
        "entity_id": "charging_status",
    },
    "charger_connection_status": {
        "name": "Charger connection status",
        "device_class": None,
        "icon": "mdi:power-plug",
        "unit": None,
        "entity_id": "charger_connection_status",
    },
    "estimated_charging_time": {
        "name": "Estimated charging time",
        "device_class": "duration",
        "icon": "mdi:timer-outline",
        "unit": "min",
        "entity_id": "estimated_charging_time",
        "state_class": "measurement",
    },
    "charging_power": {
        "name": "Charging power",
        "device_class": "power",
        "icon": "mdi:lightning-bolt",
        "unit": "kW",
        "entity_id": "charging_power",
        "state_class": "measurement",
    },
    "charging_voltage": {
        "name": "Charging voltage",
        "device_class": "voltage",
        "icon": "mdi:sine-wave",
        "unit": "V",
        "entity_id": "charging_voltage",
        "state_class": "measurement",
    },
    "charging_current": {
        "name": "Charging current",
        "device_class": "current",
        "icon": "mdi:current-ac",
        "unit": "A",
        "entity_id": "charging_current",
        "state_class": "measurement",
    },
    "charging_session_energy": {
        "name": "Charging session energy",
        "device_class": "energy",
        "icon": "mdi:battery-charging-100",
        "unit": "kWh",
        "entity_id": "charging_session_energy",
        "state_class": "total_increasing",
    },
    "home_charge_switch": {
        "name": "Home Charge",
        "device_class": None,
        "icon": "mdi:ev-plug-type2",
        "unit": "",
        "entity_id": "home_charge_switch",
    },
    "charge_limit_number": {
        "name": "Charge Limit",
        "device_class": None,
        "icon": "mdi:battery-charging-90",
        "unit": "%",
        "entity_id": "charge_limit",
    },
    "plug_and_charge_switch": {
        "name": "Plug And Charge",
        "device_class": None,
        "icon": "mdi:power-plug-battery",
        "unit": "",
        "entity_id": "plug_and_charge_switch",
    },
    # TODO
    # "fuel_amount_level": {
    #    "name": "Fuel amount level",
    #    "device_class": None,
    #    "icon": "mdi:gas-station",
    #    "unit": "%",
    # },
    "position": {
        "name": "Position",
        "device_class": None,
        "icon": "",
        "unit": "",
        "entity_id": "position",
    },
    "position_wgs84": {
        "name": "Position WGS84",
        "device_class": None,
        "icon": "",
        "unit": "",
        "entity_id": "position_wgs84",
    },
    "flash_button": {
        "name": "Flash",
        "device_class": None,
        "icon": "mdi:car-light-high",
        "unit": "",
        "entity_id": "flash",
    },
    "honk_flash_button": {
        "name": "Honk And Flash",
        "device_class": None,
        "icon": "mdi:alarm-light",
        "unit": "",
        "entity_id": "honk_and_flash",
    },
    "engine_duration_number": {
        "name": "Engine Duration",
        "device_class": None,
        "icon": "mdi:clock-time-eight-outline",
        "unit": "Minute",
        "entity_id": "engine_duration",
    },
    "engine_switch": {
        "name": "Engine Remote control",
        "device_class": None,
        "icon": "mdi:engine-outline",
        "unit": "",
        "entity_id": "engine_remote_control",
    },
    "climatization_switch": {
        "name": "Climatization",
        "device_class": None,
        "icon": "mdi:air-conditioner",
        "unit": "",
        "entity_id": "climatization",
    },
    "honk_button": {
        "name": "Honk",
        "device_class": None,
        "icon": "mdi:bugle",
        "unit": "",
        "entity_id": "honk",
    },
    "app_sign_in_button": {
        "name": "App Sign In",
        "device_class": None,
        "icon": "mdi:calendar-check",
        "unit": "",
        "entity_id": "app_sign_in",
    },
    "tail_gate_switch": {
        "name": "Tailgate control",
        "device_class": None,
        "icon": "mdi:car-back",
        "unit": "",
        "entity_id": "tailgate_control",
    },
    "sunroof_switch": {
        "name": "Sunroof control",
        "device_class": None,
        "icon": "mdi:home-roof",
        "unit": "",
        "entity_id": "sunroof_control",
    },
    "service_warning_msg": {
        "name": "Service Warning Message",
        "device_class": None,
        "icon": "mdi:car-wrench",
        "unit": None,
        "entity_id": "service_warning_msg",
    },
    "service_warning": {
        "name": "Service Warning",
        "device_class": "problem",
        "icon": "mdi:car-wrench",
        "unit": None,
        "entity_id": "service_warning",
    },
    "brake_fluid_level_warning": {
        "name": "Brake Fluid Level Warning",
        "device_class": "problem",
        "icon": "mdi:car-brake-fluid-level",
        "unit": None,
        "entity_id": "brake_fluid_level_warning",
    },
    "engine_coolant_level_warning": {
        "name": "Engine Coolant Level Warning",
        "device_class": "problem",
        "icon": "mdi:car-coolant-level",
        "unit": None,
        "entity_id": "engine_coolant_level_warning",
    },
    "oil_level_warning": {
        "name": "Oil Level Warning",
        "device_class": "problem",
        "icon": "mdi:oil-level",
        "unit": None,
        "entity_id": "oil_level_warning",
    },
    "washer_fluid_level_warning": {
        "name": "Washer Fluid Level Warning",
        "device_class": "problem",
        "icon": "mdi:wiper-wash",
        "unit": None,
        "entity_id": "washer_fluid_level_warning",
    },
    "front_left_tyre_pressure_warning": {
        "name": "Front Left Tyre Pressure Warning",
        "device_class": "problem",
        "icon": "mdi:car-tire-alert",
        "unit": None,
        "entity_id": "front_left_tyre_pressure_warning",
    },
    "front_right_tyre_pressure_warning": {
        "name": "Front Right Tyre Pressure Warning",
        "device_class": "problem",
        "icon": "mdi:car-tire-alert",
        "unit": None,
        "entity_id": "front_right_tyre_pressure_warning",
    },
    "rear_left_tyre_pressure_warning": {
        "name": "Rear Left Tyre Pressure Warning",
        "device_class": "problem",
        "icon": "mdi:car-tire-alert",
        "unit": None,
        "entity_id": "rear_left_tyre_pressure_warning",
    },
    "rear_right_tyre_pressure_warning": {
        "name": "Rear Right Tyre Pressure Warning",
        "device_class": "problem",
        "icon": "mdi:car-tire-alert",
        "unit": None,
        "entity_id": "rear_right_tyre_pressure_warning",
    },
    "connection_status": {
        "name": "Connection Status",
        "device_class": None,
        "icon": "mdi:connection",
        "unit": None,
        "entity_id": "connection_status",
        "entity_category": EntityCategory.DIAGNOSTIC,
    },
}


class VolvoEntity(CoordinatorEntity):
    def __init__(self, coordinator, idx, metaMapKey, platform):
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(coordinator, context=idx)
        self.idx = idx
        self.metaMapKey = metaMapKey
        # The VIN is captured once so the entity's identity stays stable even
        # if the API reorders the vehicle list between polls.
        self.vin = self.coordinator.data[self.idx].vin
        # entity_id object ids must be lowercase; a raw VIN is rejected from
        # Home Assistant 2027.2 on.
        self.entity_id = (
            f"{platform}."
            f"{slugify(self.vin)}_{metaMap[self.metaMapKey]['entity_id']}"
        )

    @property
    def vehicle(self):
        """Return this entity's Vehicle, looked up by VIN.

        Falls back to positional lookup so tests (and older coordinators)
        that only provide coordinator.data keep working.
        """
        vehicles = getattr(self.coordinator, "_vehicles", None)
        if isinstance(vehicles, dict):
            vehicle = vehicles.get(self.vin)
            if vehicle is not None:
                return vehicle
        data = self.coordinator.data or []
        for candidate in data:
            if getattr(candidate, "vin", None) == self.vin:
                return candidate
        if 0 <= self.idx < len(data):
            return data[self.idx]
        return None

    def _get_store(self):
        """Return this entity's persistent store, looked up by VIN."""
        stores = getattr(self.coordinator, "_stores", None)
        if isinstance(stores, dict):
            store = stores.get(self.vin)
            if store is not None:
                return store
        store_datas = getattr(self.coordinator, "store_datas", None) or []
        if 0 <= self.idx < len(store_datas):
            return store_datas[self.idx]
        return None

    @property
    def available(self) -> bool:
        return super().available and self.vehicle is not None

    @property
    def icon(self):
        return metaMap[self.metaMapKey]["icon"]

    @property
    def device_class(self):
        return metaMap[self.metaMapKey]["device_class"]

    @property
    def device_info(self) -> DeviceInfo:
        """Return a unique set of attributes for each vehicle."""
        vehicle = self.vehicle
        series_name = getattr(vehicle, "series_name", "") or ""
        model_name = getattr(vehicle, "model_name", "") or ""
        return DeviceInfo(
            identifiers={(DOMAIN, self.vin)},
            name="Volvo " + series_name,
            model=(series_name + " " + model_name).strip(),
            manufacturer="Volvo",
        )

    @property
    def unique_id(self) -> str:
        """Return a unique ID."""
        return f"{self.vin}-{self.metaMapKey}"

    @property
    def translation_key(self) -> str:
        return self.metaMapKey

    @property
    def has_entity_name(self) -> bool:
        return True

    @property
    def translation_placeholders(self):
        return {"nickname": getattr(self.vehicle, "nickname", "") or ""}
