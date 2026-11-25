"""Tests for dynamic polling: schedule maths, coordinator wiring, options."""

from datetime import datetime, time as dt_time, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant import data_entry_flow
from homeassistant.const import CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.volvooncall_cn import (
    DEGRADED_FAST_RETRY_CYCLES,
    IN_USE_LINGER_SECONDS,
    POLL_RETRY_INTERVAL_SECONDS,
    VolvoCoordinator,
    async_update_options,
)
from custom_components.volvooncall_cn.const import (
    CONF_CHARGING_SCAN_INTERVAL,
    CONF_DAY_SCAN_INTERVAL,
    CONF_DAY_START,
    CONF_DYNAMIC_POLLING,
    CONF_DYNAMIC_POLLING_SETTINGS,
    CONF_IN_USE_SCAN_INTERVAL,
    CONF_IN_USE_WINDOWS,
    CONF_NIGHT_SCAN_INTERVAL,
    CONF_NIGHT_START,
    CONF_POWERTRAIN_TYPE,
    DEFAULT_CHARGING_SCAN_INTERVAL,
    DEFAULT_DAY_SCAN_INTERVAL,
    DEFAULT_IN_USE_SCAN_INTERVAL,
    DEFAULT_IN_USE_WINDOWS,
    DEFAULT_NIGHT_SCAN_INTERVAL,
    POWERTRAIN_FUEL,
    POWERTRAIN_HYBRID,
)
from custom_components.volvooncall_cn.polling import (
    POLLING_MODE_CHARGING,
    POLLING_MODE_DAY,
    POLLING_MODE_FIXED,
    POLLING_MODE_IN_USE,
    POLLING_MODE_NIGHT,
    PollingSchedule,
    format_time_windows,
    parse_time_of_day,
    parse_time_windows,
)
from custom_components.volvooncall_cn.volvooncall_cn import (
    DOMAIN,
    SOURCE_CACHE_MAX_AGE_OVERRIDES,
    SOURCE_REFRESH_EVERY_CYCLES,
    Vehicle,
)

VIN = "TESTVIN0000000001"
NOW_PATCH = "custom_components.volvooncall_cn.dt_util.now"
MONOTONIC_PATCH = "custom_components.volvooncall_cn.time.monotonic"


def _at(hour, minute=0, second=0):
    return datetime(2026, 11, 24, hour, minute, second, tzinfo=timezone.utc)


def _vehicle(**state):
    values = {
        "is_unbound": False,
        "engine_running": False,
        "engine_remote_running": False,
        "battery_charging_status": "idle",
        "home_charge_status": None,
        "charging_power": 0,
        "_consecutive_failures": 0,
        "_data_source_status": {"exterior": True},
    }
    values.update(state)
    return SimpleNamespace(**values)


def _coordinator(hass, *vehicles, at=None, powertrain=POWERTRAIN_HYBRID):
    """Build a dynamic-polling coordinator holding the given fake vehicles."""
    with patch(NOW_PATCH, return_value=at or _at(12)):
        coordinator = VolvoCoordinator(
            hass,
            MagicMock(),
            30,
            powertrain,
            polling_schedule=PollingSchedule(),
        )
    for index, vehicle in enumerate(vehicles):
        coordinator._vehicles[f"VIN{index}"] = vehicle
    return coordinator


def _apply(coordinator, at):
    with patch(NOW_PATCH, return_value=at):
        coordinator._apply_polling_interval()
    return coordinator.polling_mode, coordinator.update_interval.total_seconds()


# =============================================================================
# Parsing
# =============================================================================


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("07:00", dt_time(7, 0)),
        ("7:05", dt_time(7, 5)),
        ("22:30:00", dt_time(22, 30)),
        ("07：00", dt_time(7, 0)),  # full-width colon
    ],
)
def test_parse_time_of_day(text, expected):
    assert parse_time_of_day(text) == expected


@pytest.mark.parametrize("text", ["", "7", "24:00", "07:60", "abc", "07-00"])
def test_parse_time_of_day_rejects_garbage(text):
    with pytest.raises(ValueError):
        parse_time_of_day(text)


def test_parse_time_windows_accepts_chinese_punctuation():
    windows = parse_time_windows("7:00-9:00，17：00～19:30")
    assert windows == (
        (dt_time(7, 0), dt_time(9, 0)),
        (dt_time(17, 0), dt_time(19, 30)),
    )
    assert format_time_windows(windows) == "07:00-09:00,17:00-19:30"


def test_parse_time_windows_empty_means_no_windows():
    assert parse_time_windows("") == ()
    assert parse_time_windows(None) == ()


@pytest.mark.parametrize("text", ["07:00", "07:00-07:00", "07:00-25:00", "a-b"])
def test_parse_time_windows_rejects_garbage(text):
    with pytest.raises(ValueError):
        parse_time_windows(text)


# =============================================================================
# Schedule
# =============================================================================


def test_defaults_are_distinct_and_ordered():
    """In use < charging < day < night, and charging is not a dense poll."""
    assert (
        DEFAULT_IN_USE_SCAN_INTERVAL
        < DEFAULT_CHARGING_SCAN_INTERVAL
        < DEFAULT_DAY_SCAN_INTERVAL
        < DEFAULT_NIGHT_SCAN_INTERVAL
    )
    assert DEFAULT_CHARGING_SCAN_INTERVAL >= 5 * 60


@pytest.mark.parametrize(
    ("moment", "state", "expected"),
    [
        (_at(12), {}, (POLLING_MODE_DAY, DEFAULT_DAY_SCAN_INTERVAL)),
        (_at(23), {}, (POLLING_MODE_NIGHT, DEFAULT_NIGHT_SCAN_INTERVAL)),
        (_at(3), {}, (POLLING_MODE_NIGHT, DEFAULT_NIGHT_SCAN_INTERVAL)),
        (_at(8), {}, (POLLING_MODE_IN_USE, DEFAULT_IN_USE_SCAN_INTERVAL)),
        (_at(18, 30), {}, (POLLING_MODE_IN_USE, DEFAULT_IN_USE_SCAN_INTERVAL)),
        # Window end is exclusive.
        (_at(9), {}, (POLLING_MODE_DAY, DEFAULT_DAY_SCAN_INTERVAL)),
        # Charging overnight is polled at the charging interval, not night's.
        (
            _at(2),
            {"charging": True},
            (POLLING_MODE_CHARGING, DEFAULT_CHARGING_SCAN_INTERVAL),
        ),
        (
            _at(12),
            {"charging": True},
            (POLLING_MODE_CHARGING, DEFAULT_CHARGING_SCAN_INTERVAL),
        ),
        # Driving outside any window still gets the in-use interval.
        (
            _at(2),
            {"in_use": True},
            (POLLING_MODE_IN_USE, DEFAULT_IN_USE_SCAN_INTERVAL),
        ),
        # Shortest interval wins when several modes apply.
        (
            _at(8),
            {"charging": True},
            (POLLING_MODE_IN_USE, DEFAULT_IN_USE_SCAN_INTERVAL),
        ),
    ],
)
def test_select_mode(moment, state, expected):
    assert PollingSchedule().select(moment, **state) == expected


def test_slow_charging_interval_never_slows_the_day():
    schedule = PollingSchedule(charging_interval=1800, day_interval=300)
    assert schedule.select(_at(12), charging=True) == (POLLING_MODE_DAY, 300)


def test_window_wrapping_midnight():
    schedule = PollingSchedule(
        in_use_windows=parse_time_windows("23:00-01:00")
    )
    assert schedule.select(_at(23, 30))[0] == POLLING_MODE_IN_USE
    assert schedule.select(_at(0, 30))[0] == POLLING_MODE_IN_USE
    assert schedule.select(_at(1, 0))[0] == POLLING_MODE_NIGHT


def test_boundary_cap_wakes_up_for_the_commute_window():
    """A night poll at 06:55 must not sleep through the 07:00 window."""
    schedule = PollingSchedule()
    assert schedule.seconds_until_faster(
        _at(6, 55), DEFAULT_NIGHT_SCAN_INTERVAL
    ) == pytest.approx(300)


def test_boundary_cap_ignores_slower_and_distant_boundaries():
    schedule = PollingSchedule()
    # 21:55 -> night at 22:00 is slower than day: nothing to wake up for.
    assert schedule.seconds_until_faster(
        _at(21, 55), DEFAULT_DAY_SCAN_INTERVAL
    ) is None
    # 03:00 -> the next faster boundary is hours beyond the interval.
    assert schedule.seconds_until_faster(
        _at(3), DEFAULT_NIGHT_SCAN_INTERVAL
    ) is None


def test_from_config_enabled_by_default():
    """Entries created before the option existed get the default schedule."""
    assert PollingSchedule.from_config({}) == PollingSchedule()
    assert PollingSchedule.from_config({CONF_DYNAMIC_POLLING: False}) is None


def test_from_config_reads_values_and_survives_bad_ones():
    schedule = PollingSchedule.from_config(
        {
            CONF_DYNAMIC_POLLING: True,
            CONF_IN_USE_SCAN_INTERVAL: 45,
            CONF_CHARGING_SCAN_INTERVAL: 5,  # below the minimum
            CONF_DAY_SCAN_INTERVAL: "oops",
            CONF_NIGHT_SCAN_INTERVAL: 999999,
            CONF_DAY_START: "06:30",
            CONF_NIGHT_START: "nonsense",
            CONF_IN_USE_WINDOWS: "08:00-08:30",
        }
    )
    assert schedule.in_use_interval == 45
    assert schedule.charging_interval == 30
    assert schedule.day_interval == DEFAULT_DAY_SCAN_INTERVAL
    assert schedule.night_interval == 3600
    assert schedule.day_start == dt_time(6, 30)
    assert schedule.night_start == dt_time(22, 0)
    assert schedule.in_use_windows == ((dt_time(8, 0), dt_time(8, 30)),)
    assert schedule.fastest_interval == 30


# =============================================================================
# Coordinator
# =============================================================================


async def test_fixed_mode_is_untouched(hass: HomeAssistant):
    """Without a schedule the configured interval is never rewritten."""
    coordinator = VolvoCoordinator(hass, MagicMock(), 45)
    coordinator._vehicles["A"] = _vehicle(engine_running=True)
    coordinator._apply_polling_interval()
    assert coordinator.polling_mode == POLLING_MODE_FIXED
    assert coordinator.update_interval == timedelta(seconds=45)
    assert coordinator._throttle_interval_seconds() == 45


async def test_initial_interval_comes_from_the_clock(hass: HomeAssistant):
    coordinator = _coordinator(hass, at=_at(3))
    assert coordinator.polling_mode == POLLING_MODE_NIGHT
    assert coordinator.update_interval == timedelta(
        seconds=DEFAULT_NIGHT_SCAN_INTERVAL
    )


async def test_interval_follows_time_of_day(hass: HomeAssistant):
    coordinator = _coordinator(hass, _vehicle())
    assert _apply(coordinator, _at(12)) == (
        POLLING_MODE_DAY,
        DEFAULT_DAY_SCAN_INTERVAL,
    )
    assert _apply(coordinator, _at(8)) == (
        POLLING_MODE_IN_USE,
        DEFAULT_IN_USE_SCAN_INTERVAL,
    )
    assert _apply(coordinator, _at(2)) == (
        POLLING_MODE_NIGHT,
        DEFAULT_NIGHT_SCAN_INTERVAL,
    )


@pytest.mark.parametrize(
    "state",
    [
        {"battery_charging_status": "charging"},
        {"home_charge_status": "starting"},
        {"charging_power": 2.9},
    ],
)
async def test_charging_vehicle_uses_charging_interval(hass, state):
    coordinator = _coordinator(hass, _vehicle(), _vehicle(**state))
    assert _apply(coordinator, _at(2)) == (
        POLLING_MODE_CHARGING,
        DEFAULT_CHARGING_SCAN_INTERVAL,
    )


async def test_fuel_car_never_enters_charging_mode(hass: HomeAssistant):
    coordinator = _coordinator(
        hass,
        _vehicle(battery_charging_status="charging"),
        powertrain=POWERTRAIN_FUEL,
    )
    assert _apply(coordinator, _at(2))[0] == POLLING_MODE_NIGHT


async def test_unbound_vehicle_is_ignored(hass: HomeAssistant):
    coordinator = _coordinator(
        hass, _vehicle(is_unbound=True, engine_running=True)
    )
    assert _apply(coordinator, _at(2))[0] == POLLING_MODE_NIGHT


@pytest.mark.parametrize("flag", ["engine_running", "engine_remote_running"])
async def test_running_engine_uses_in_use_interval_then_lingers(hass, flag):
    """In-use polling continues briefly after the engine stops."""
    vehicle = _vehicle(**{flag: True})
    coordinator = _coordinator(hass, vehicle)
    with patch(MONOTONIC_PATCH, return_value=1000.0):
        assert _apply(coordinator, _at(2)) == (
            POLLING_MODE_IN_USE,
            DEFAULT_IN_USE_SCAN_INTERVAL,
        )
    setattr(vehicle, flag, False)
    with patch(MONOTONIC_PATCH, return_value=1000.0 + IN_USE_LINGER_SECONDS):
        assert _apply(coordinator, _at(2))[0] == POLLING_MODE_IN_USE
    with patch(
        MONOTONIC_PATCH, return_value=1001.0 + IN_USE_LINGER_SECONDS
    ):
        assert _apply(coordinator, _at(2))[0] == POLLING_MODE_NIGHT


async def test_next_poll_is_pulled_forward_to_a_faster_boundary(hass):
    coordinator = _coordinator(hass, _vehicle())
    mode, interval = _apply(coordinator, _at(6, 55))
    assert mode == POLLING_MODE_NIGHT
    assert interval == pytest.approx(301)
    # Seconds before the boundary the minimum poll interval still applies.
    assert _apply(coordinator, _at(6, 59, 50))[1] == 30


async def test_cache_allowance_tracks_the_mode_interval(hass: HomeAssistant):
    """The allowance is the mode's interval, not a shortened retry delay."""
    vehicle = _vehicle(_data_source_status={"exterior": False})
    coordinator = _coordinator(hass, vehicle)
    coordinator._real_polls += 1
    mode, interval = _apply(coordinator, _at(2))
    assert mode == POLLING_MODE_NIGHT
    assert interval == POLL_RETRY_INTERVAL_SECONDS
    assert vehicle.cache_age_allowance == timedelta(
        seconds=DEFAULT_NIGHT_SCAN_INTERVAL
    )


async def test_degraded_vehicle_gets_bounded_fast_retries(hass: HomeAssistant):
    """A permanently failing source must not pin polling to the retry rate."""
    vehicle = _vehicle(_data_source_status={"exterior": False})
    coordinator = _coordinator(hass, vehicle)
    for _ in range(DEGRADED_FAST_RETRY_CYCLES):
        coordinator._real_polls += 1
        assert _apply(coordinator, _at(2))[1] == POLL_RETRY_INTERVAL_SECONDS
    # A pass that did not actually poll (throttled) spends no retry.
    assert _apply(coordinator, _at(2))[1] == POLL_RETRY_INTERVAL_SECONDS
    coordinator._real_polls += 1
    assert _apply(coordinator, _at(2))[1] == DEFAULT_NIGHT_SCAN_INTERVAL

    # Recovery re-arms the fast retries for the next incident.
    vehicle._data_source_status["exterior"] = True
    coordinator._real_polls += 1
    _apply(coordinator, _at(2))
    assert coordinator._degraded_fast_retries == 0


async def test_failed_poll_retries_soon_instead_of_next_night_interval(
    hass: HomeAssistant,
):
    api = AsyncMock()
    api.login = AsyncMock(side_effect=Exception("boom"))
    with patch(NOW_PATCH, return_value=_at(2)):
        coordinator = VolvoCoordinator(
            hass, api, 30, polling_schedule=PollingSchedule()
        )
        coordinator.data = [MagicMock()]
        assert coordinator.update_interval.total_seconds() == (
            DEFAULT_NIGHT_SCAN_INTERVAL
        )
        assert await coordinator._async_update_data() is coordinator.data
    assert coordinator._consecutive_failures == 1
    assert coordinator.update_interval.total_seconds() == (
        POLL_RETRY_INTERVAL_SECONDS
    )


async def test_successful_poll_applies_vehicle_state(hass: HomeAssistant):
    """End to end: a poll that finds the car charging switches the interval."""
    api = AsyncMock()
    api.get_vehicles_vins = AsyncMock(
        return_value={VIN: {"modelYear": 2023, "seriesCode": "246"}}
    )

    async def fake_update(self):
        self.battery_charging_status = "charging"
        self.charging_power = 2.9

    with (
        patch(NOW_PATCH, return_value=_at(2)),
        patch.object(Vehicle, "update", fake_update),
        patch(
            "custom_components.volvooncall_cn.VolvoStore"
        ) as store_class,
    ):
        store = store_class.return_value
        store.load_create_data = AsyncMock()
        store.async_capture_full_charge_range = AsyncMock()
        coordinator = VolvoCoordinator(
            hass, api, 30, polling_schedule=PollingSchedule()
        )
        coordinator._async_enforce_charge_limit = AsyncMock()
        vehicles = await coordinator._async_update_data()

    assert [vehicle.vin for vehicle in vehicles] == [VIN]
    assert coordinator.polling_mode == POLLING_MODE_CHARGING
    assert coordinator.update_interval.total_seconds() == (
        DEFAULT_CHARGING_SCAN_INTERVAL
    )
    assert vehicles[0].cache_age_allowance == timedelta(
        seconds=DEFAULT_CHARGING_SCAN_INTERVAL
    )


async def test_home_assistant_arms_the_next_poll_with_the_new_interval(
    hass: HomeAssistant,
):
    """The interval chosen after a poll is the one HA actually schedules."""
    api = AsyncMock()
    api.login = AsyncMock(side_effect=Exception("boom"))
    with patch(NOW_PATCH, return_value=_at(2)):
        coordinator = VolvoCoordinator(
            hass, api, 30, polling_schedule=PollingSchedule()
        )
        coordinator.data = [MagicMock()]
        unsubscribe = coordinator.async_add_listener(lambda: None)
        try:
            await coordinator.async_refresh()
            timer = coordinator._unsub_refresh.__self__
            delay = timer.when() - hass.loop.time()
        finally:
            unsubscribe()
    assert POLL_RETRY_INTERVAL_SECONDS - 2 < delay <= (
        POLL_RETRY_INTERVAL_SECONDS + 1
    )


async def test_manual_refresh_is_not_throttled_for_the_whole_night_interval(
    hass: HomeAssistant,
):
    coordinator = _coordinator(hass, at=_at(2))
    assert coordinator.update_interval.total_seconds() == (
        DEFAULT_NIGHT_SCAN_INTERVAL
    )
    assert coordinator._throttle_interval_seconds() == (
        DEFAULT_IN_USE_SCAN_INTERVAL
    )


async def test_setup_entry_without_the_option_polls_dynamically(
    hass: HomeAssistant,
):
    """An upgraded entry (no stored option) starts in dynamic mode."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_USERNAME: "TEST_USERNAME",
            CONF_PASSWORD: "TEST_PASSWORD",
            CONF_SCAN_INTERVAL: 30,
        },
        unique_id="TEST_USERNAME",
    )
    entry.add_to_hass(hass)
    api = AsyncMock()
    api.set_token_listener = MagicMock()
    api.export_tokens = MagicMock(return_value={})
    api.get_vehicles_vins = AsyncMock(
        return_value={VIN: {"modelYear": "2024"}}
    )
    with (
        patch(NOW_PATCH, return_value=_at(2)),
        patch(
            "custom_components.volvooncall_cn.VehicleAPI", return_value=api
        ),
        patch(
            "custom_components.volvooncall_cn.Vehicle.update", new=AsyncMock()
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        coordinator = hass.data[DOMAIN][entry.entry_id]
        assert coordinator.polling_mode == POLLING_MODE_NIGHT
        assert coordinator.update_interval == timedelta(
            seconds=DEFAULT_NIGHT_SCAN_INTERVAL
        )
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_update_options_switches_dynamic_polling_on_and_off(
    hass: HomeAssistant,
):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_USERNAME: "TEST_USERNAME",
            CONF_PASSWORD: "TEST_PASSWORD",
            CONF_SCAN_INTERVAL: 30,
        },
        unique_id="TEST_USERNAME",
    )
    entry.add_to_hass(hass)
    api = MagicMock()
    api._username = "TEST_USERNAME"
    api._password = "TEST_PASSWORD"
    coordinator = VolvoCoordinator(hass, api, 30)
    vehicle = _vehicle()
    coordinator._vehicles["A"] = vehicle
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    hass.config_entries.async_update_entry(
        entry,
        options={
            CONF_SCAN_INTERVAL: 90,
            CONF_DYNAMIC_POLLING: True,
            CONF_NIGHT_SCAN_INTERVAL: 1200,
        },
    )
    with patch(NOW_PATCH, return_value=_at(2)):
        await async_update_options(hass, entry)
    assert coordinator.polling_mode == POLLING_MODE_NIGHT
    assert coordinator.update_interval == timedelta(seconds=1200)

    hass.config_entries.async_update_entry(
        entry, options={CONF_SCAN_INTERVAL: 90, CONF_DYNAMIC_POLLING: False}
    )
    await async_update_options(hass, entry)
    assert coordinator.polling_mode == POLLING_MODE_FIXED
    assert coordinator.update_interval == timedelta(seconds=90)
    assert vehicle.cache_age_allowance == timedelta(0)


# =============================================================================
# Vehicle layer
# =============================================================================


def _cached_vehicle():
    vehicle = Vehicle(VIN, MagicMock(), True, supports_electric=False)
    vehicle._save_to_cache("health", {"service_warning": False})
    vehicle._poll_cycle = 1
    return vehicle


def test_throttled_source_is_due_after_its_nominal_time():
    """Long intervals must not stretch a cycle-counted cadence to hours."""
    vehicle = _cached_vehicle()
    assert vehicle._should_refresh("health") is False

    nominal = SOURCE_REFRESH_EVERY_CYCLES["health"] * 30
    refreshed = vehicle._source_refreshed_monotonic["health"]
    target = "custom_components.volvooncall_cn.volvooncall_cn.time.monotonic"
    with patch(target, return_value=refreshed + nominal):
        # At the nominal 30s cadence the cycle count stays in charge.
        assert vehicle._should_refresh("health") is False
    with patch(target, return_value=refreshed + nominal * 1.5):
        assert vehicle._should_refresh("health") is True


def test_cache_allowance_extends_the_exterior_limit():
    vehicle = Vehicle(VIN, MagicMock(), True, supports_electric=False)
    vehicle._save_to_cache("exterior", {"car_locked": True})
    vehicle._cache_timestamp["exterior"] -= (
        SOURCE_CACHE_MAX_AGE_OVERRIDES["exterior"] + timedelta(minutes=5)
    )

    vehicle.cache_age_allowance = timedelta(minutes=30)
    vehicle.car_locked = None
    assert vehicle._restore_from_cache("exterior") is True
    assert vehicle.car_locked is True

    vehicle.cache_age_allowance = timedelta(0)
    assert vehicle._restore_from_cache("exterior") is False
    assert vehicle.car_locked is None


# =============================================================================
# Options flow
# =============================================================================


def _entry(hass, **options):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_USERNAME: "TEST_USERNAME",
            CONF_PASSWORD: "TEST_PASSWORD",
            CONF_SCAN_INTERVAL: 30,
        },
        options=options,
        unique_id="TEST_USERNAME",
    )
    entry.add_to_hass(hass)
    return entry


async def _submit(hass, entry, user_input):
    with patch(
        "custom_components.volvooncall_cn.config_flow.volvo_validation",
        new=AsyncMock(return_value={}),
    ) as validation:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=user_input
        )
    return result, validation


async def test_options_flow_enables_dynamic_polling_with_defaults(hass):
    entry = _entry(hass)
    result, validation = await _submit(
        hass,
        entry,
        {
            CONF_PASSWORD: "",
            CONF_SCAN_INTERVAL: 30,
            CONF_POWERTRAIN_TYPE: POWERTRAIN_HYBRID,
            CONF_DYNAMIC_POLLING: True,
        },
    )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_DYNAMIC_POLLING] is True
    assert entry.options[CONF_IN_USE_WINDOWS] == DEFAULT_IN_USE_WINDOWS
    assert entry.options[CONF_NIGHT_SCAN_INTERVAL] == (
        DEFAULT_NIGHT_SCAN_INTERVAL
    )
    assert CONF_DYNAMIC_POLLING_SETTINGS not in entry.options
    validation.assert_not_awaited()
    assert PollingSchedule.from_config(entry.options) == PollingSchedule()


async def test_options_flow_stores_normalized_custom_settings(hass):
    entry = _entry(hass)
    result, _validation = await _submit(
        hass,
        entry,
        {
            CONF_PASSWORD: "",
            CONF_SCAN_INTERVAL: 30,
            CONF_POWERTRAIN_TYPE: POWERTRAIN_HYBRID,
            CONF_DYNAMIC_POLLING: True,
            CONF_DYNAMIC_POLLING_SETTINGS: {
                CONF_IN_USE_SCAN_INTERVAL: 45,
                CONF_IN_USE_WINDOWS: "8:00-9:30，18：00-19:00",
                CONF_CHARGING_SCAN_INTERVAL: 600,
                CONF_DAY_SCAN_INTERVAL: 300,
                CONF_DAY_START: "06:30:00",
                CONF_NIGHT_SCAN_INTERVAL: 1200,
                CONF_NIGHT_START: "23:00:00",
            },
        },
    )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_IN_USE_WINDOWS] == "08:00-09:30,18:00-19:00"
    assert entry.options[CONF_DAY_START] == "06:30"
    assert entry.options[CONF_NIGHT_START] == "23:00"
    schedule = PollingSchedule.from_config(entry.options)
    assert schedule.in_use_interval == 45
    assert schedule.charging_interval == 600
    assert schedule.day_interval == 300
    assert schedule.night_interval == 1200


async def test_options_flow_keeps_stored_settings_when_section_is_omitted(hass):
    entry = _entry(
        hass,
        **{CONF_DYNAMIC_POLLING: True, CONF_NIGHT_SCAN_INTERVAL: 1200},
    )
    result, _validation = await _submit(
        hass,
        entry,
        {
            CONF_PASSWORD: "",
            CONF_SCAN_INTERVAL: 60,
            CONF_POWERTRAIN_TYPE: POWERTRAIN_HYBRID,
        },
    )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_DYNAMIC_POLLING] is True
    assert entry.options[CONF_NIGHT_SCAN_INTERVAL] == 1200


@pytest.mark.parametrize(
    ("settings", "error"),
    [
        ({CONF_IN_USE_WINDOWS: "morning"}, "invalid_in_use_windows"),
        (
            {CONF_DAY_START: "08:00:00", CONF_NIGHT_START: "08:00:00"},
            "invalid_day_night_time",
        ),
    ],
)
async def test_options_flow_rejects_bad_settings_before_any_login(
    hass, settings, error
):
    entry = _entry(hass)
    result, validation = await _submit(
        hass,
        entry,
        {
            CONF_PASSWORD: "a_new_password",
            CONF_SCAN_INTERVAL: 30,
            CONF_POWERTRAIN_TYPE: POWERTRAIN_HYBRID,
            CONF_DYNAMIC_POLLING: True,
            CONF_DYNAMIC_POLLING_SETTINGS: settings,
        },
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"] == {"base": error}
    validation.assert_not_awaited()
    assert CONF_DYNAMIC_POLLING not in entry.options


async def test_options_flow_can_switch_dynamic_polling_off(hass):
    entry = _entry(hass)
    result, _validation = await _submit(
        hass,
        entry,
        {
            CONF_PASSWORD: "",
            CONF_SCAN_INTERVAL: 45,
            CONF_POWERTRAIN_TYPE: POWERTRAIN_HYBRID,
            CONF_DYNAMIC_POLLING: False,
        },
    )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_DYNAMIC_POLLING] is False
    assert PollingSchedule.from_config({**entry.data, **entry.options}) is None


async def test_initial_flow_offers_the_dynamic_polling_switch(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    defaults = {
        marker.schema: marker.default()
        for marker in result["data_schema"].schema
        if marker.schema == CONF_DYNAMIC_POLLING
    }
    assert defaults == {CONF_DYNAMIC_POLLING: True}


async def test_options_flow_rejects_out_of_range_interval(hass):
    entry = _entry(hass)
    with pytest.raises(data_entry_flow.InvalidData):
        await _submit(
            hass,
            entry,
            {
                CONF_PASSWORD: "",
                CONF_SCAN_INTERVAL: 30,
                CONF_POWERTRAIN_TYPE: POWERTRAIN_HYBRID,
                CONF_DYNAMIC_POLLING_SETTINGS: {CONF_NIGHT_SCAN_INTERVAL: 5},
            },
        )
