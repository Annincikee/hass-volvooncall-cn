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

from homeassistant.components.sensor import SensorEntity
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD, CONF_SCAN_INTERVAL

from .store import VolvoStore
from .volvooncall_base import DEFAULT_SCAN_INTERVAL
from .volvooncall_cn import VehicleAPI
from .volvooncall_cn import Vehicle
from .volvooncall_cn import DOMAIN
from .const import (
    CONF_POWERTRAIN_TYPE,
    DEFAULT_POWERTRAIN_TYPE,
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

FRONTEND_PATH = Path(__file__).parent / "frontend"
FRONTEND_URL_PATH = f"/{DOMAIN}/frontend"
CARD_RESOURCE_PATH = f"{FRONTEND_URL_PATH}/volvo-car-card.js"
CARD_RESOURCE_URL = f"{CARD_RESOURCE_PATH}?v=2.0.2"


async def _async_register_card_resource(hass: HomeAssistant) -> None:
    """Register the bundled Lovelace card when storage resources are used."""
    lovelace = hass.data.get(LOVELACE_DATA)
    if lovelace is None:
        _LOGGER.warning(
            "Lovelace is not loaded; add %s as a module resource manually",
            CARD_RESOURCE_URL,
        )
        return

    if lovelace.resource_mode != MODE_STORAGE:
        _LOGGER.info(
            "Lovelace resources use YAML mode; add %s as a module resource",
            CARD_RESOURCE_URL,
        )
        return

    resources = lovelace.resources
    await resources.async_get_info()
    for item in resources.async_items() or []:
        url = item.get("url", "")
        if url.split("?", 1)[0] != CARD_RESOURCE_PATH:
            continue
        if url != CARD_RESOURCE_URL:
            await resources.async_update_item(
                item["id"],
                {"res_type": "module", "url": CARD_RESOURCE_URL},
            )
        return

    await resources.async_create_item(
        {"res_type": "module", "url": CARD_RESOURCE_URL}
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
    electric_suffixes = tuple(f"-{key}" for key in ELECTRIC_SENSOR_KEYS)
    for registry_entry in er.async_entries_for_config_entry(
        registry, config_entry_id
    ):
        if registry_entry.unique_id.endswith(electric_suffixes):
            registry.async_remove(registry_entry.entity_id)


async def async_update_options(hass: HomeAssistant, config_entry: ConfigEntry):
    # entry = {**config_entry.data, **config_entry.options}
    config_data = {**config_entry.data, **config_entry.options}
    entry_id = config_entry.entry_id

    username = config_data.get(CONF_USERNAME)
    password = config_data.get(CONF_PASSWORD)
    interval = config_data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    powertrain_type = config_data.get(
        CONF_POWERTRAIN_TYPE, DEFAULT_POWERTRAIN_TYPE
    )
    _LOGGER.info("new interval: %s", interval)
    session = async_get_clientsession(hass)
    volvo_api = VehicleAPI(session=session, username=username, password=password)
    hass.data.setdefault(DOMAIN, {})
    if config_entry.entry_id in hass.data[DOMAIN]:
        coordinator = hass.data[DOMAIN][entry_id]
        if coordinator.powertrain_type != powertrain_type:
            if powertrain_type != POWERTRAIN_HYBRID:
                remove_electric_entity_registry_entries(hass, entry_id)
            await hass.config_entries.async_reload(entry_id)
            return
        coordinator.volvo_api = volvo_api
        coordinator.update_interval = timedelta(seconds=interval)


async def async_setup_entry(hass, entry):
    """Config entry example."""
    session = async_get_clientsession(hass)

    config_data = {**entry.data, **entry.options}
    username = config_data.get(CONF_USERNAME)
    password = config_data.get(CONF_PASSWORD)
    interval = config_data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    powertrain_type = config_data.get(
        CONF_POWERTRAIN_TYPE, DEFAULT_POWERTRAIN_TYPE
    )
    volvo_api = VehicleAPI(session=session, username=username, password=password)
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
        hass.data[DOMAIN].pop(entry.entry_id, None)
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
        self._update_lock = asyncio.Lock()
        # Connection health tracking
        self._consecutive_failures = 0
        self._last_failure_reason = None


    async def _retry_with_backoff(self, func, max_retries=2, initial_delay=1.0):
        """Retry a function with exponential backoff."""
        delay = initial_delay
        last_error = None
        
        for attempt in range(max_retries + 1):
            try:
                return await func()
            except Exception as err:
                last_error = err
                if attempt < max_retries:
                    _LOGGER.warning(
                        f"Attempt {attempt + 1}/{max_retries + 1} failed: {err}. "
                        f"Retrying in {delay}s..."
                    )
                    await asyncio.sleep(delay)
                    delay *= 2  # Exponential backoff
                else:
                    _LOGGER.error(f"All {max_retries + 1} attempts failed: {err}")
                    raise last_error

    async def _async_update_data(self):
        """Fetch data from API endpoint with retry and caching support."""
        async with self._update_lock:
            now = datetime.now(timezone.utc)
            if (
                self.data is not None
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
                async with asyncio.timeout(30):
                    # Retry login and token update
                    await self._retry_with_backoff(self.volvo_api.login, max_retries=2)
                    await self._retry_with_backoff(self.volvo_api.update_token, max_retries=2)

                    vinVehicleMaps = await self.volvo_api.get_vehicles_vins()
                    vehicles = []
                    store_datas = []

                    for vin, vehicleInfos in vinVehicleMaps.items():
                        modelYear = int(vehicleInfos.get("modelYear", 2020))
                        isAaos = modelYear >= 2022
                        vehicle = Vehicle(
                            vin,
                            self.volvo_api,
                            isAaos,
                            supports_electric=self.supports_electric,
                        )

                        # Try to update, but don't fail completely
                        try:
                            await vehicle.update()
                            vehicle._consecutive_failures = 0
                            # Note: _last_successful_update is updated by _save_to_cache() in each parse method
                        except Exception as err:
                            vehicle._consecutive_failures += 1
                            _LOGGER.error(
                                f"Failed to update vehicle {vin} (failure #{vehicle._consecutive_failures}): {err}"
                            )
                            # Don't raise - continue with cached data

                        vehicles.append(vehicle)

                        store_data = VolvoStore(self.hass, vin)
                        await store_data.load_create_data()
                        if self.supports_electric:
                            await store_data.async_capture_full_charge_range(
                                vehicle.battery_charge_level_percentage,
                                vehicle.electric_range,
                                datetime.now(timezone.utc).isoformat(),
                                vehicle.charge_data_source,
                                vehicle.battery_charging_status,
                                vehicle.charging_power,
                            )
                        store_datas.append(store_data)

                    # Track successful update
                    self._consecutive_failures = 0
                    self.store_datas = store_datas
                    return vehicles

            except Exception as err:
                # Track failure but still return vehicles with cache
                self._consecutive_failures += 1
                self._last_failure_reason = str(err)
                _LOGGER.error(
                    f"Coordinator update failed (failure #{self._consecutive_failures}): {err}"
                )

                # If we have existing data (vehicles from previous update), return it
                if self.data is not None:
                    _LOGGER.warning("Returning cached vehicle data due to update failure")
                    return self.data

                # Only raise if we have no data at all (first load)
                raise UpdateFailed(f"Error communicating with API: {err}")

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
        self.entity_id = f"{platform}.{self.coordinator.data[self.idx].vin}_{metaMap[self.metaMapKey]['entity_id']}"

    @property
    def icon(self):
        return metaMap[self.metaMapKey]["icon"]

    @property
    def device_class(self):
        return metaMap[self.metaMapKey]["device_class"]

    @property
    def device_info(self) -> DeviceInfo:
        """Return a inique set of attributes for each vehicle."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.data[self.idx].vin)},
            name="Volvo " + self.coordinator.data[self.idx].series_name,
            model=self.coordinator.data[self.idx].series_name + " " + self.coordinator.data[self.idx].model_name,
            manufacturer="Volvo",
        )

    @property
    def unique_id(self) -> str:
        """Return a unique ID."""
        return f"{self.coordinator.data[self.idx].vin}-{self.metaMapKey}"

    @property
    def translation_key(self) -> str:
        return self.metaMapKey

    @property
    def has_entity_name(self) -> bool:
        return True

    @property
    def translation_placeholders(self):
        return {"nickname": (self.coordinator.data[self.idx].nickname)}
