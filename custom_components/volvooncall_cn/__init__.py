import hashlib
import logging
import asyncio
import math
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace import LOVELACE_DATA
from homeassistant.components.lovelace.const import MODE_STORAGE
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util, slugify

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD, CONF_SCAN_INTERVAL

from .polling import POLLING_MODE_FIXED, PollingSchedule
from .store import CHARGE_LIMIT_DISABLED, VolvoStore
from .volvooncall_base import (
    DEFAULT_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    VolvoAuthError,
    VolvoAuthExpiredError,
    VolvoAuthForbiddenError,
    VolvoAuthThrottledError,
    VolvoRateLimitError,
    VehicleBaseAPI,
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
    VALIDATED_TOKENS_DATA,
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
# The same for 403s from the password endpoint. Risk control answers 403 while
# it throttles an account, so these need a longer streak (spread over about
# half an hour by the auth backoff below) before the user is asked to step in.
LOGIN_FORBIDDEN_REAUTH_THRESHOLD = 6
# Backoff applied to the whole poll cycle after an authentication-class
# failure (session rejected even after an in-cycle refresh, or a refused
# login). Without it a persistent 403 cost two refresh calls and two vehicle
# listings every scan interval, indefinitely — more traffic at exactly the
# moment the server is asking for less.
AUTH_BACKOFF_BASE_SECONDS = 60
AUTH_BACKOFF_MAX_SECONDS = 30 * 60
# Upper bound for a server-supplied Retry-After. An absurd value (or a date
# far in the future) would otherwise silence polling for as long as it says.
MAX_SERVER_RETRY_AFTER = 60 * 60
# While a backoff window suppresses polling no failures are counted, so cached
# data needs a time limit as well as FAILURE_GRACE_CYCLES.
MIN_CACHED_DATA_GRACE_SECONDS = 5 * 60
# Hard ceiling for one update cycle. It has to exceed the worst case the
# transport layer can legitimately produce, or a degraded network makes every
# cycle die here before any data lands. See REQUEST_BUDGET in
# volvooncall_base: one REST call can spend up to
# TIMEOUT * MAX_RETRIES + backoff, the auth pass and the vehicle listing are
# sequential, and _parse_battery adds two more REST calls on top.
UPDATE_CYCLE_TIMEOUT = 240
# Updating vehicles concurrently prevents one slow car from starving every
# vehicle after it, while a small bound avoids turning a multi-car account
# into an API burst.
MAX_CONCURRENT_VEHICLE_UPDATES = 2
# Once the account listing is known, the cycle deadline scales by bounded
# concurrency batches. This covers a slow battery + pile + order + auto-stop
# chain without giving a hung call an unbounded lifetime.
VEHICLE_BATCH_TIMEOUT = 180
# Upper bound on queued forced refreshes, so a burst of control commands
# cannot pile up an unbounded backlog of real polls.
MAX_PENDING_FORCE_REFRESHES = 8
# A control command must never wait for the coordinator's full 150-second
# update budget multiple times. The command has already been accepted at this
# point; this shorter deadline only bounds the follow-up state confirmation.
CONTROL_CONFIRMATION_TIMEOUT = 45
# The delay before each attempt doubles (retry_delay, 2x, 4x): a car needs
# several seconds to act and report back, and three polls fired a second
# apart were all over before the new state could possibly be visible.
CONTROL_CONFIRMATION_ATTEMPTS = 3
# Consecutive failed charge-limit stop attempts before backing off. Without
# this the coordinator re-sends the stop every poll forever, one warning and
# one extra REST round trip each time.
CHARGE_LIMIT_STOP_MAX_FAILURES = 3
CHARGE_LIMIT_STOP_BACKOFF_SECONDS = 30 * 60
# Consecutive listBindCar responses a vehicle may be missing from before its
# entities go unavailable. The object itself is kept so positional entity
# lookups stay correct; only availability flips.
VEHICLE_ABSENCE_GRACE_CYCLES = 3
# --- Dynamic polling -------------------------------------------------------
# Keep the in-use interval for a while after the engine was last seen
# running, so parking, locking and plugging in show up promptly instead of
# one long idle interval later.
IN_USE_LINGER_SECONDS = 5 * 60
# A failed poll must not wait out a 30-minute night interval before trying
# again. Backoff windows (auth, Retry-After, per-source) still gate what a
# retry is actually allowed to request.
POLL_RETRY_INTERVAL_SECONDS = 120
# A partly failing cycle (some data source down, coordinator otherwise fine)
# gets this many quick retries. Bounded, because a permanently failing source
# would otherwise pin the whole schedule to the retry interval.
DEGRADED_FAST_RETRY_CYCLES = 3
# Home-charge states that mean a session is running or changing.
ACTIVE_HOME_CHARGE_STATES = frozenset({"starting", "charging", "stopping"})
AUTH_STORE_VERSION = 1
TOKEN_SAVE_DELAY_SECONDS = 5
AUTH_STORES_DATA = f"{DOMAIN}_auth_stores"

FRONTEND_PATH = Path(__file__).parent / "frontend"
FRONTEND_URL_PATH = f"/{DOMAIN}/frontend"
CARD_FILENAMES = ("volvo-car-card.js", "volvo-charging-card.js")


async def async_confirm_vehicle_state(
    coordinator,
    state_matches: Callable[[], bool],
    *,
    retry_delay: float,
    description: str,
    source_is_fresh: Callable[[], bool] | None = None,
    source_generation: Callable[[], object] | None = None,
) -> None:
    """Force bounded polls until a control command's state is visible."""
    initial_generation = (
        source_generation() if source_generation is not None else None
    )
    try:
        async with asyncio.timeout(CONTROL_CONFIRMATION_TIMEOUT):
            for attempt in range(CONTROL_CONFIRMATION_ATTEMPTS):
                await asyncio.sleep(retry_delay * (2**attempt))
                refresh_succeeded = await coordinator.async_force_refresh()
                if not refresh_succeeded:
                    continue
                if source_is_fresh is not None and not source_is_fresh():
                    continue
                if (
                    source_generation is not None
                    and source_generation() == initial_generation
                ):
                    continue
                if state_matches():
                    return
    except TimeoutError:
        pass

    raise HomeAssistantError(
        f"{description} command was sent but the requested state "
        "could not be confirmed"
    )


async def async_refresh_after_control(
    coordinator,
    *,
    description: str,
    source_is_fresh: Callable[[], bool] | None = None,
    source_generation: Callable[[], object] | None = None,
) -> None:
    """Run one bounded post-control refresh."""
    initial_generation = (
        source_generation() if source_generation is not None else None
    )
    try:
        async with asyncio.timeout(CONTROL_CONFIRMATION_TIMEOUT):
            refresh_succeeded = await coordinator.async_force_refresh()
    except TimeoutError as err:
        raise HomeAssistantError(
            f"{description} was saved, but the immediate vehicle refresh "
            "timed out"
        ) from err
    if not refresh_succeeded or (
        source_is_fresh is not None and not source_is_fresh()
    ) or (
        source_generation is not None
        and source_generation() == initial_generation
    ):
        raise HomeAssistantError(
            f"{description} was saved, but fresh vehicle data could not "
            "be retrieved"
        )


def vehicle_sources_are_fresh(vehicle, *sources: str) -> bool:
    """Return whether a control's target vehicle sources refreshed cleanly."""
    return bool(
        vehicle is not None
        and vehicle._consecutive_failures == 0
        and all(
            vehicle._data_source_status.get(source) is True
            for source in sources
        )
    )


def vehicle_source_generation(vehicle, *sources: str):
    """Return a comparable success generation for selected vehicle sources."""
    if vehicle is None:
        return None
    return tuple(
        vehicle._data_source_generation.get(source, -1)
        for source in sources
    )


def _card_resources() -> tuple[tuple[str, str], ...]:
    """Return (path, cache-busted url) for each bundled Lovelace card.

    The query parameter is derived from the file content instead of a
    hand-maintained version string. A hand-written one silently drifts —
    3.2.0 shipped volvo-car-card.js 2.2.0 behind "?v=2.0.2", so upgrading
    users kept the browser-cached copy of the old card.

    Runs blocking file reads; call it from an executor.
    """
    resources = []
    for filename in CARD_FILENAMES:
        resource_path = f"{FRONTEND_URL_PATH}/{filename}"
        try:
            digest = hashlib.sha256(
                (FRONTEND_PATH / filename).read_bytes()
            ).hexdigest()[:10]
        except OSError as err:
            _LOGGER.warning("Cannot read bundled card %s: %s", filename, err)
            continue
        resources.append((resource_path, f"{resource_path}?v={digest}"))
    return tuple(resources)


async def _async_register_card_resource(
    hass: HomeAssistant, card_resources: tuple[tuple[str, str], ...]
) -> None:
    """Register the bundled Lovelace cards when storage resources are used."""
    lovelace = hass.data.get(LOVELACE_DATA)
    if lovelace is None:
        _LOGGER.warning(
            "Lovelace is not loaded; add %s as module resources manually",
            ", ".join(url for _, url in card_resources),
        )
        return

    if lovelace.resource_mode != MODE_STORAGE:
        _LOGGER.info(
            "Lovelace resources use YAML mode; add %s as module resources",
            ", ".join(url for _, url in card_resources),
        )
        return

    resources = lovelace.resources
    await resources.async_get_info()
    items = resources.async_items() or []
    for resource_path, resource_url in card_resources:
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


CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Serve and register the bundled Volvo Home Assistant cards."""
    card_resources = await hass.async_add_executor_job(_card_resources)

    # `hasattr` alone is not enough: the attribute exists but is None until
    # the http component has been set up.
    if getattr(hass, "http", None) is None:
        _LOGGER.warning(
            "Home Assistant HTTP component is unavailable; add %s manually",
            ", ".join(url for _, url in card_resources),
        )
        return True

    # The cards are a convenience. They lean on Lovelace internals that change
    # between Home Assistant releases, and a failure here must not take the
    # vehicle integration down with it.
    try:
        await hass.http.async_register_static_paths(
            [StaticPathConfig(FRONTEND_URL_PATH, str(FRONTEND_PATH), True)]
        )
        await _async_register_card_resource(hass, card_resources)
    except Exception as err:
        _LOGGER.warning(
            "Could not register the bundled Lovelace cards (%s: %s); add %s "
            "as module resources manually",
            type(err).__name__,
            err,
            ", ".join(url for _, url in card_resources),
        )
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
) -> Store:
    """Persist session tokens so restarts refresh instead of re-logging in.

    Frequent password logins (e.g. across quick Home Assistant restarts) have
    been observed to trip Volvo's server-side risk control.
    """
    auth_store = _auth_store_for(hass, username)
    validated_tokens = hass.data.get(VALIDATED_TOKENS_DATA, {}).pop(
        username, None
    )
    if not hass.data.get(VALIDATED_TOKENS_DATA):
        hass.data.pop(VALIDATED_TOKENS_DATA, None)
    saved_tokens = validated_tokens or await auth_store.async_load()
    if saved_tokens:
        volvo_api.import_tokens(saved_tokens)
    if validated_tokens:
        await auth_store.async_save(validated_tokens)
    volvo_api.set_token_listener(
        lambda tokens: auth_store.async_delay_save(
            lambda: tokens, TOKEN_SAVE_DELAY_SECONDS
        )
    )
    return auth_store


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

    current_api = coordinator.volvo_api
    credentials_changed = not (
        getattr(current_api, "_username", None) == username
        and getattr(current_api, "_password", None) == password
    )
    if credentials_changed:
        # Cancel pending writes from the old runtime before any reload path.
        # This must also run when powertrain and password are saved together,
        # otherwise an old delayed token write can win after new validation.
        current_api.set_token_listener(None)
        stores = hass.data.get(AUTH_STORES_DATA, {}).pop(entry_id, [])
        for auth_store in stores:
            await auth_store.async_remove()

    if coordinator.powertrain_type != powertrain_type:
        if powertrain_type != POWERTRAIN_HYBRID:
            remove_electric_entity_registry_entries(hass, entry_id)
        hass.config_entries.async_schedule_reload(entry_id)
        return

    if credentials_changed:
        # Cached Vehicle objects hold the API instance too, so reload the
        # coordinator and every vehicle atomically after credential changes.
        hass.config_entries.async_schedule_reload(entry_id)
        return

    coordinator.set_polling_config(
        interval, PollingSchedule.from_config(config_data)
    )
    # The update_interval setter only changes the numeric value; it does not
    # move an already scheduled callback. Re-arm it so long -> short changes
    # take effect immediately instead of waiting for the old interval.
    if getattr(coordinator, "_listeners", None):
        coordinator._schedule_refresh()

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
    coordinator = None
    try:
        auth_store = await _async_attach_token_store(hass, volvo_api, username)
        # Replace, not append: every reload and every ConfigEntryNotReady
        # retry builds a new Store for the same file, and appending kept all
        # of them alive for the lifetime of the process.
        hass.data.setdefault(AUTH_STORES_DATA, {})[entry.entry_id] = [
            auth_store
        ]
        coordinator = VolvoCoordinator(
            hass,
            volvo_api,
            interval,
            powertrain_type,
            config_entry=entry,
            polling_schedule=PollingSchedule.from_config(config_data),
        )
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

        # Home Assistant owns this listener and removes it on unload or a
        # failed setup retry.
        entry.async_on_unload(
            entry.add_update_listener(async_update_options)
        )

        # Fetch initial data so entities have data when they subscribe.
        await coordinator.async_config_entry_first_refresh()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        domain_data = hass.data.get(DOMAIN, {})
        if (
            coordinator is not None
            and domain_data.get(entry.entry_id) is coordinator
        ):
            domain_data.pop(entry.entry_id, None)
        if hasattr(volvo_api, "close"):
            volvo_api.set_token_listener(None)
            try:
                await volvo_api.close()
            except Exception as close_err:  # pragma: no cover - defensive
                _LOGGER.warning(
                    "Could not close API client after setup failure (%s)",
                    type(close_err).__name__,
                )
        raise

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
            volvo_api = coordinator.volvo_api
            volvo_api.set_token_listener(None)
            await _async_flush_tokens(hass, entry.entry_id, volvo_api)
            await volvo_api.close()
    return unload_ok


async def _async_flush_tokens(hass, entry_id, volvo_api) -> None:
    """Write the live session to disk before the runtime goes away.

    Token writes are debounced by TOKEN_SAVE_DELAY_SECONDS. A reload inside
    that window made the next setup read the previous refresh token from
    disk, which the server may already have rotated out.
    """
    stores = hass.data.get(AUTH_STORES_DATA, {}).get(entry_id) or []
    if not stores or not isinstance(volvo_api, VehicleBaseAPI):
        # No store: credentials changed and the stores were removed on
        # purpose.
        return
    try:
        await stores[-1].async_save(volvo_api.export_tokens())
    except Exception as err:
        _LOGGER.warning(
            "Could not persist session tokens on unload (%s)",
            type(err).__name__,
        )


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete every private token store associated with a removed entry."""
    stores = hass.data.get(AUTH_STORES_DATA, {}).pop(entry.entry_id, [])
    removed_store_keys = set()
    for auth_store in stores:
        removed_store_keys.add(auth_store.key)
        await auth_store.async_remove()

    # Cover entries created before the in-memory registry existed, and legacy
    # entries whose account was overridden in options.
    usernames = {
        str(config.get(CONF_USERNAME, ""))
        for config in (entry.data, entry.options)
        if config.get(CONF_USERNAME)
    }
    for username in usernames:
        auth_store = _auth_store_for(hass, username)
        if auth_store.key not in removed_store_keys:
            await auth_store.async_remove()

    if not hass.data.get(AUTH_STORES_DATA):
        hass.data.pop(AUTH_STORES_DATA, None)


class VolvoCoordinator(DataUpdateCoordinator):
    """My custom coordinator."""

    def __init__(
        self,
        hass,
        volvo_api,
        scan_interval,
        powertrain_type=DEFAULT_POWERTRAIN_TYPE,
        *,
        config_entry: ConfigEntry | None = None,
        polling_schedule: PollingSchedule | None = None,
    ):
        """Initialize my coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            # Name of the data. For logging purposes.
            name="Volvo On Call CN sensor",
            # Polling interval. Will only be polled if there are subscribers.
            update_interval=timedelta(seconds=scan_interval),
            config_entry=config_entry,
        )
        self.volvo_api = volvo_api
        self.powertrain_type = powertrain_type
        self.supports_electric = powertrain_type == POWERTRAIN_HYBRID
        self.store_datas = []
        self._last_update_started_at = None
        # Outstanding forced refreshes. A single boolean loses requests: two
        # concurrent async_force_refresh() calls both set it, the first one
        # through the update body consumes it, and its finally-clause then
        # clears the second caller's request — which is silently downgraded
        # to a throttled no-op. A counter gives every caller its own token.
        self._pending_force_refreshes = 0
        # Identity tokens let a timed-out caller remove only its own queued
        # request. A plain counter cannot distinguish that request from a
        # concurrent caller's token.
        self._force_refresh_tokens: list[object] = []
        self._force_refresh_task_tokens: dict[asyncio.Task, object] = {}
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
        # VIN -> consecutive polls the car was missing from listBindCar.
        self._absent_cycles = {}
        # VIN -> (consecutive stop failures, monotonic time to retry after).
        self._charge_limit_stop_failures = {}
        self._reload_for_new_vehicle_pending = False
        self._server_retry_after_monotonic = 0.0
        # Authentication-class failures back the whole cycle off.
        self._login_forbidden = 0
        self._auth_failure_streak = 0
        self._auth_backoff_until_monotonic = 0.0
        # Monotonic time of the first failure in the current streak.
        self._first_failure_monotonic = None
        # Dynamic polling. With no schedule the configured scan interval is
        # used as is and none of this state is touched.
        self._base_scan_interval = scan_interval
        self._polling_schedule = polling_schedule
        self.polling_mode = POLLING_MODE_FIXED
        self._last_in_use_monotonic = None
        self._real_polls = 0
        self._real_polls_at_last_interval = 0
        self._degraded_fast_retries = 0
        self._apply_polling_interval()

    def set_polling_config(
        self, scan_interval, polling_schedule: PollingSchedule | None
    ) -> None:
        """Apply a changed scan interval and/or dynamic schedule."""
        self._base_scan_interval = scan_interval
        self._polling_schedule = polling_schedule
        self.update_interval = timedelta(seconds=scan_interval)
        if polling_schedule is None:
            self.polling_mode = POLLING_MODE_FIXED
            self._degraded_fast_retries = 0
            for vehicle in self._vehicles.values():
                vehicle.cache_age_allowance = timedelta(0)
        self._apply_polling_interval()

    @staticmethod
    def _vehicle_in_use(vehicle) -> bool:
        return (
            getattr(vehicle, "engine_running", None) is True
            or getattr(vehicle, "engine_remote_running", None) is True
        )

    def _vehicle_charging(self, vehicle) -> bool:
        if not self.supports_electric:
            return False
        if getattr(vehicle, "battery_charging_status", None) == "charging":
            return True
        if (
            getattr(vehicle, "home_charge_status", None)
            in ACTIVE_HOME_CHARGE_STATES
        ):
            return True
        power = getattr(vehicle, "charging_power", None)
        return isinstance(power, (int, float)) and power > 0

    @staticmethod
    def _vehicle_degraded(vehicle) -> bool:
        if getattr(vehicle, "_consecutive_failures", 0):
            return True
        statuses = getattr(vehicle, "_data_source_status", None)
        return isinstance(statuses, dict) and any(
            status is False for status in statuses.values()
        )

    def _apply_polling_interval(self) -> None:
        """Pick the next poll delay from the clock and the vehicles' state.

        Runs after every update pass. Home Assistant reads update_interval
        when it arms the next refresh, which happens right after
        _async_update_data returns or raises, so setting it here takes effect
        for the very next poll.
        """
        schedule = self._polling_schedule
        if schedule is None:
            return

        now_monotonic = time.monotonic()
        vehicles = [
            vehicle
            for vehicle in self._vehicles.values()
            if not getattr(vehicle, "is_unbound", False)
        ]
        if any(self._vehicle_in_use(vehicle) for vehicle in vehicles):
            self._last_in_use_monotonic = now_monotonic
        in_use = (
            self._last_in_use_monotonic is not None
            and now_monotonic - self._last_in_use_monotonic
            <= IN_USE_LINGER_SECONDS
        )
        charging = any(self._vehicle_charging(vehicle) for vehicle in vehicles)

        now = dt_util.now()
        mode, mode_interval = schedule.select(
            now, in_use=in_use, charging=charging
        )
        interval = float(mode_interval)

        boundary = schedule.seconds_until_faster(now, interval)
        if boundary is not None:
            # One second past the boundary, so the poll that fires there
            # already sees the new mode.
            interval = max(boundary + 1.0, float(MIN_SCAN_INTERVAL))

        polled = self._real_polls != self._real_polls_at_last_interval
        self._real_polls_at_last_interval = self._real_polls
        if self._consecutive_failures > 0:
            interval = min(interval, POLL_RETRY_INTERVAL_SECONDS)
        elif any(self._vehicle_degraded(vehicle) for vehicle in vehicles):
            if polled:
                self._degraded_fast_retries += 1
            if self._degraded_fast_retries <= DEGRADED_FAST_RETRY_CYCLES:
                interval = min(interval, POLL_RETRY_INTERVAL_SECONDS)
        else:
            self._degraded_fast_retries = 0

        # A source cache is normally one poll old when it has to stand in for
        # a failed refresh. With long intervals that alone would exceed the
        # short limit on lock/door data, blanking the lock on a single missed
        # poll, so the limit is extended by the interval the mode asks for.
        allowance = timedelta(seconds=mode_interval)
        for vehicle in self._vehicles.values():
            vehicle.cache_age_allowance = allowance

        if (
            mode != self.polling_mode
            or self.update_interval is None
            or abs(self.update_interval.total_seconds() - interval) >= 1
        ):
            _LOGGER.debug(
                "Dynamic polling: mode=%s, next poll in %.0fs "
                "(in_use=%s, charging=%s)",
                mode,
                interval,
                in_use,
                charging,
            )
        self.polling_mode = mode
        self.update_interval = timedelta(seconds=interval)

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
        vehicleInfos = vehicleInfos if isinstance(vehicleInfos, dict) else {}
        vehicle = self._vehicles.get(vin)
        if vehicle is None:
            try:
                modelYear = int(vehicleInfos.get("modelYear") or 2020)
            except (TypeError, ValueError):
                _LOGGER.warning(
                    "Invalid model year for %s; using the compatibility default",
                    vehicle_log_ref(vin),
                )
                modelYear = 2020
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
            # Platforms enumerate vehicles only during entry setup. If a car
            # is bound later, reload once so HA creates its entities instead
            # of keeping it invisible until the next manual restart.
            if (
                self.data is not None
                and self.config_entry is not None
                and not self._reload_for_new_vehicle_pending
            ):
                self._reload_for_new_vehicle_pending = True
                self.hass.config_entries.async_schedule_reload(
                    self.config_entry.entry_id
                )
        else:
            # The API object may have been replaced after an options change.
            vehicle._api = self.volvo_api
        # listBindCar was already fetched for this poll; hand the names over
        # so Vehicle.update() does not repeat the same request.
        vehicle.apply_vehicle_info(vehicleInfos)
        return vehicle

    async def _store_for(self, vin):
        """Return the persistent store for a VIN, loading it on first sight."""
        store = self._stores.get(vin)
        if store is None:
            store = VolvoStore(self.hass, vin)
            await store.load_create_data()
            self._stores[vin] = store
        return store

    def _note_vehicle_presence(self, present_vins):
        """Flag vehicles that have dropped out of the account listing.

        The Vehicle object is deliberately kept in _vehicles and in the
        emitted list: VolvoEntity falls back to a positional lookup, so
        removing an entry would make a removed car's entities start showing
        a *different* car's data. Only availability is flipped.
        """
        for vin, vehicle in self._vehicles.items():
            if vin in present_vins:
                self._absent_cycles.pop(vin, None)
                vehicle.is_unbound = False
                continue

            absent = self._absent_cycles.get(vin, 0) + 1
            self._absent_cycles[vin] = absent
            if absent < VEHICLE_ABSENCE_GRACE_CYCLES:
                continue
            if not getattr(vehicle, "is_unbound", False):
                _LOGGER.warning(
                    "%s has been missing from the account listing for %s "
                    "polls; marking its entities unavailable",
                    vehicle_log_ref(vin),
                    absent,
                )
            vehicle.is_unbound = True

    def _scan_interval_seconds(self) -> float:
        if self.update_interval is None:
            return float(DEFAULT_SCAN_INTERVAL)
        return self.update_interval.total_seconds()

    def _throttle_interval_seconds(self) -> float:
        """Return the interval that early, unforced refreshes are held to.

        With dynamic polling the current interval can be half an hour; an
        explicit refresh request (homeassistant.update_entity) should not be
        answered from cache for that long. The fastest configured interval is
        a rate the user has already accepted.
        """
        interval = self._scan_interval_seconds()
        if self._polling_schedule is not None:
            interval = min(interval, self._polling_schedule.fastest_interval)
        return interval

    def _may_serve_cached_data(self, now) -> bool:
        """Return whether cached data is still fresh enough to stand in.

        Bounded by failure count and by time: polls suppressed by a backoff
        window are not counted as failures, so the count alone would let
        entities show a frozen snapshot for the whole window.
        """
        if self.data is None:
            return False
        if self._consecutive_failures > FAILURE_GRACE_CYCLES:
            return False
        if self._first_failure_monotonic is None:
            return True
        grace = max(
            FAILURE_GRACE_CYCLES * self._scan_interval_seconds(),
            MIN_CACHED_DATA_GRACE_SECONDS,
        )
        return now - self._first_failure_monotonic <= grace

    def _note_auth_failure(self) -> None:
        """Back the poll cycle off exponentially after an auth-class failure."""
        self._auth_failure_streak += 1
        delay = min(
            AUTH_BACKOFF_BASE_SECONDS
            * (2 ** min(self._auth_failure_streak - 1, 10)),
            AUTH_BACKOFF_MAX_SECONDS,
        )
        self._auth_backoff_until_monotonic = time.monotonic() + delay
        _LOGGER.warning(
            "Authentication failure #%s; pausing Volvo polling for %ss",
            self._auth_failure_streak,
            delay,
        )

    def _handle_update_failure(self, err):
        """Serve cached data during short outages, go unavailable on long ones."""
        now = time.monotonic()
        if self._consecutive_failures == 0 or self._first_failure_monotonic is None:
            self._first_failure_monotonic = now
        self._consecutive_failures += 1
        self._last_failure_reason = redact_sensitive(err)
        _LOGGER.error(
            "Coordinator update failed (failure #%s): %s",
            self._consecutive_failures,
            self._last_failure_reason,
        )

        if self._may_serve_cached_data(now):
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
            f"Error communicating with API: {self._last_failure_reason}",
            retry_after=getattr(err, "retry_after", None),
        ) from err

    async def _async_update_data(self):
        """Fetch data, then choose the delay before the next poll."""
        try:
            return await self._async_poll_vehicles()
        finally:
            self._apply_polling_interval()

    async def _async_poll_vehicles(self):
        """Fetch data from API endpoint with caching and failure grading."""
        async with self._update_lock:
            now = time.monotonic()
            # Consume one forced-refresh token, if any caller queued one.
            task = asyncio.current_task()
            caller_token = (
                self._force_refresh_task_tokens.get(task)
                if task is not None
                else None
            )
            if caller_token is not None:
                # Consume this caller's own token. It may already have been
                # consumed by a scheduled update that ran while the caller
                # was queued behind the coordinator refresh lock.
                force_refresh = caller_token in self._force_refresh_tokens
                if force_refresh:
                    self._force_refresh_tokens.remove(caller_token)
                    self._pending_force_refreshes -= 1
            else:
                # Scheduled refreshes may satisfy the oldest queued forced
                # request before its caller acquires the HA refresh lock.
                force_refresh = self._pending_force_refreshes > 0
                if force_refresh:
                    self._pending_force_refreshes -= 1
                    if self._force_refresh_tokens:
                        self._force_refresh_tokens.pop(0)
            if force_refresh and caller_token is None:
                _LOGGER.debug("Scheduled poll consumed a forced refresh token")
            blocked_until = max(
                self._server_retry_after_monotonic,
                self._auth_backoff_until_monotonic,
            )
            if now < blocked_until:
                retry_after = blocked_until - now
                if self._may_serve_cached_data(now):
                    _LOGGER.debug(
                        "Skipping Volvo refresh for %.1fs (rate limit or "
                        "authentication backoff)",
                        retry_after,
                    )
                    return self.data
                raise UpdateFailed(
                    "Volvo API backoff window is still active: "
                    f"{self._last_failure_reason}",
                    retry_after=retry_after,
                )
            # Home Assistant arms the next poll exactly one interval after
            # the previous one fired whenever that poll finished within the
            # same wall-clock second, so event-loop jitter of a few
            # microseconds made a strict comparison drop scheduled polls and
            # silently double the interval. The margin only has to separate
            # those from genuinely early, unforced refresh requests.
            scan_interval = self._throttle_interval_seconds()
            throttle_window = max(scan_interval - 1.0, scan_interval * 0.9)
            if (
                not force_refresh
                and self.data is not None
                and self._last_update_started_at is not None
                and self.update_interval is not None
                and now - self._last_update_started_at < throttle_window
            ):
                if self._consecutive_failures > FAILURE_GRACE_CYCLES:
                    # The grace window is already exhausted and entities are
                    # unavailable. Returning cached data here counts as a
                    # successful update to DataUpdateCoordinator, so every
                    # entity would come back to life showing stale values
                    # without a single request having been made.
                    raise UpdateFailed(
                        "Error communicating with API: "
                        f"{self._last_failure_reason}"
                    )
                _LOGGER.debug(
                    "Skipping Volvo refresh because the global scan interval "
                    "has not elapsed"
                )
                return self.data

            self._last_update_started_at = now
            self._real_polls += 1

            try:
                async with asyncio.timeout(UPDATE_CYCLE_TIMEOUT) as cycle_timeout:
                    await self.volvo_api.login()
                    await self.volvo_api.update_token()

                    vinVehicleMaps = await self._async_get_vehicle_map()
                    if not vinVehicleMaps and not self._vehicles:
                        raise RuntimeError(
                            "The account returned no vehicles; setup will retry"
                        )
                    vehicle_batches = (
                        len(vinVehicleMaps)
                        + MAX_CONCURRENT_VEHICLE_UPDATES
                        - 1
                    ) // MAX_CONCURRENT_VEHICLE_UPDATES
                    if vehicle_batches:
                        cycle_timeout.reschedule(
                            asyncio.get_running_loop().time()
                            + VEHICLE_BATCH_TIMEOUT * vehicle_batches
                        )
                    vehicles = []
                    store_datas = []
                    semaphore = asyncio.Semaphore(
                        MAX_CONCURRENT_VEHICLE_UPDATES
                    )

                    async def update_vehicle(vin, vehicle_infos):
                        async with semaphore:
                            try:
                                vehicle = self._vehicle_for(vin, vehicle_infos)
                                await vehicle.update()
                            except Exception as err:
                                vehicle = self._vehicles.get(vin)
                                if vehicle is not None:
                                    vehicle._consecutive_failures += 1
                                    failure_count = vehicle._consecutive_failures
                                else:
                                    failure_count = 1
                                _LOGGER.error(
                                    "Failed to update %s (failure #%d): %s",
                                    vehicle_log_ref(vin),
                                    failure_count,
                                    redact_sensitive(err),
                                )
                                return False

                            vehicle._consecutive_failures = 0
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
                            return True

                    update_tasks = []
                    async with asyncio.TaskGroup() as task_group:
                        for vin, vehicleInfos in vinVehicleMaps.items():
                            update_tasks.append(
                                task_group.create_task(
                                    update_vehicle(vin, vehicleInfos)
                                )
                            )

                    if update_tasks and not any(
                        task.result() for task in update_tasks
                    ):
                        raise RuntimeError(
                            "No vehicle data source refreshed successfully"
                        )

                    # A vehicle missing from one listing (account hiccup, car
                    # resold) keeps its last object so entity indexes stay
                    # valid; it simply stops receiving updates. After
                    # VEHICLE_ABSENCE_GRACE_CYCLES consecutive absences it is
                    # treated as unbound and its entities go unavailable
                    # instead of serving a frozen snapshot forever.
                    self._note_vehicle_presence(set(vinVehicleMaps))

                    for vin in self._vehicle_order:
                        if vin in self._vehicles:
                            vehicles.append(self._vehicles[vin])
                            store = self._stores.get(vin)
                            if store is None:
                                store = await self._store_for(vin)
                            store_datas.append(store)

                    # Track successful update
                    self._consecutive_failures = 0
                    self._first_failure_monotonic = None
                    self._login_rejections = 0
                    self._login_forbidden = 0
                    self._auth_failure_streak = 0
                    self._auth_backoff_until_monotonic = 0.0
                    self._server_retry_after_monotonic = 0.0
                    self.store_datas = store_datas
                    return vehicles

            except VolvoRateLimitError as err:
                if err.retry_after is not None:
                    self._server_retry_after_monotonic = time.monotonic() + min(
                        err.retry_after, MAX_SERVER_RETRY_AFTER
                    )
                return self._handle_update_failure(err)
            except VolvoAuthThrottledError as err:
                # The local cooldown refused a password login; not a verdict
                # on the credentials, just wait for the next cycle.
                return self._handle_update_failure(err)
            except VolvoAuthError as err:
                if isinstance(err, VolvoAuthForbiddenError):
                    self._login_forbidden += 1
                else:
                    self._login_rejections += 1
                if (
                    self._login_rejections >= LOGIN_REJECTION_REAUTH_THRESHOLD
                    or self._login_forbidden
                    >= LOGIN_FORBIDDEN_REAUTH_THRESHOLD
                ):
                    # Repeated rejections: stop polling and ask the user to
                    # re-enter the password.
                    raise ConfigEntryAuthFailed(
                        redact_sensitive(err)
                    ) from err
                self._note_auth_failure()
                return self._handle_update_failure(err)
            except VolvoAuthExpiredError as err:
                # The session is still rejected after the in-cycle refresh,
                # or the refresh endpoint itself answered 403.
                self._note_auth_failure()
                return self._handle_update_failure(err)
            except ConfigEntryAuthFailed:
                raise
            except Exception as err:
                return self._handle_update_failure(err)

    async def async_force_refresh(self) -> bool:
        """Refresh immediately after a control command, bypassing throttling.

        Each caller owns an identity token consumed inside _async_update_data.
        This preserves concurrent requests and also lets a timed-out caller
        withdraw only its own still-queued token.
        """
        token = object()
        queued = False
        if self._pending_force_refreshes < MAX_PENDING_FORCE_REFRESHES:
            self._force_refresh_tokens.append(token)
            self._pending_force_refreshes += 1
            queued = True
        task = asyncio.current_task()
        if task is not None:
            self._force_refresh_task_tokens[task] = token
        try:
            await self.async_refresh()
        finally:
            token_was_consumed = (
                queued and token not in self._force_refresh_tokens
            )
            # If cancellation happened while waiting for the coordinator
            # refresh lock, _async_update_data has not consumed this token.
            # Remove exactly this caller's token so a later scheduled poll is
            # not accidentally promoted to a forced update.
            if token in self._force_refresh_tokens:
                self._force_refresh_tokens.remove(token)
                self._pending_force_refreshes -= 1
            if task is not None:
                self._force_refresh_task_tokens.pop(task, None)
        return (
            token_was_consumed
            and self.last_update_success
            and self._consecutive_failures == 0
        )

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
        if (
            not math.isfinite(battery_value)
            or not 0 <= battery_value <= 100
            or battery_value < limit
        ):
            return

        home_charge_status = str(vehicle.home_charge_status or "").lower()
        if home_charge_status not in ("starting", "charging"):
            self._charge_limit_stop_failures.pop(vehicle.vin, None)
            return

        failures, retry_after = self._charge_limit_stop_failures.get(
            vehicle.vin, (0, 0.0)
        )
        now = time.monotonic()
        if failures >= CHARGE_LIMIT_STOP_MAX_FAILURES and now < retry_after:
            # Backing off: the pile keeps refusing. Re-sending every poll
            # only produces one warning and one extra REST round trip per
            # 30 seconds, indefinitely.
            _LOGGER.debug(
                "Skipping charge-limit stop for %s; backing off for %ds",
                vehicle_log_ref(vehicle.vin),
                int(retry_after - now),
            )
            return

        _LOGGER.info(
            "%s reached the configured %d%% limit; stopping home charge",
            vehicle_log_ref(vehicle.vin),
            limit,
        )
        try:
            await vehicle.stop_home_charge()
        except Exception as err:
            failures += 1
            self._charge_limit_stop_failures[vehicle.vin] = (
                failures,
                now + CHARGE_LIMIT_STOP_BACKOFF_SECONDS,
            )
            log = (
                _LOGGER.error
                if failures == CHARGE_LIMIT_STOP_MAX_FAILURES
                else _LOGGER.warning
            )
            log(
                "Failed to stop home charge for %s at the %d%% limit "
                "(failure #%d): %s",
                vehicle_log_ref(vehicle.vin),
                limit,
                failures,
                redact_sensitive(err),
            )
        else:
            self._charge_limit_stop_failures.pop(vehicle.vin, None)

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
        vehicle = self.vehicle
        if vehicle is None or getattr(vehicle, "is_unbound", False):
            return False
        if (
            getattr(vehicle, "_consecutive_failures", 0)
            > FAILURE_GRACE_CYCLES
        ):
            return False
        return super().available

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
