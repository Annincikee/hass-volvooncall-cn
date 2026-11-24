"""Regression tests for the second audit pass.

Each test pins one behaviour that was previously wrong; the docstring names
the symptom so a failure explains why the fix exists.
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import grpc
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.volvooncall_cn import (
    AUTH_BACKOFF_BASE_SECONDS,
    AUTH_STORES_DATA,
    FAILURE_GRACE_CYCLES,
    LOGIN_FORBIDDEN_REAUTH_THRESHOLD,
    MAX_SERVER_RETRY_AFTER,
    VolvoCoordinator,
    _async_flush_tokens,
    async_confirm_vehicle_state,
    async_setup,
)
from custom_components.volvooncall_cn.config_flow import volvo_validation
from custom_components.volvooncall_cn.lock import VolvoWindowSensor
from custom_components.volvooncall_cn.proto.invocation_pb2 import (
    invocationCommResp,
    invocationData,
    invocationStatus,
)
from custom_components.volvooncall_cn.proto.odometer_pb2 import GetOdometerResp
from custom_components.volvooncall_cn.switch import VolvoEngineSwitch
from custom_components.volvooncall_cn.volvooncall_base import (
    REFRESH_FORBIDDEN_TOLERANCE,
    VehicleBaseAPI,
    VolvoAPIError,
    VolvoAuthError,
    VolvoAuthExpiredError,
    VolvoAuthForbiddenError,
    VolvoRateLimitError,
    VolvoRefreshForbiddenError,
)
from custom_components.volvooncall_cn.volvooncall_cn import (
    DOMAIN,
    Vehicle,
    VehicleAPI,
)

VIN = "TESTVIN0000000001"
USERNAME = "TEST_USERNAME"


def _session_payload(prefix="fresh", **overrides):
    data = {
        "refreshToken": f"{prefix}_refresh",
        "globalAccessToken": f"{prefix}_global",
        "accessToken": f"{prefix}_access",
        "jwtToken": f"{prefix}_jwt",
        "expiresIn": 1800,
    }
    data.update(overrides)
    return {"success": True, "data": data}


def _base_api():
    api = VehicleBaseAPI(MagicMock(), USERNAME, "pw")
    api.digitalvolvo_get = AsyncMock()
    api.digitalvolvo_post = AsyncMock()
    return api


class FakeCall:
    """Mimic grpc.aio's UnaryStreamCall: async-iterable plus cancel()."""

    def __init__(self, items, hang=False):
        self._items = list(items)
        self._hang = hang
        self.cancelled = False

    def __aiter__(self):
        async def _gen():
            for item in self._items:
                if isinstance(item, BaseException):
                    raise item
                yield item
            if self._hang:
                await asyncio.Event().wait()

        return _gen()

    def cancel(self):
        self.cancelled = True


def _ack(status):
    return invocationCommResp(data=invocationData(status=status))


def _coordinator(hass, powertrain="b4_b5_b6"):
    api = AsyncMock()
    api.login = AsyncMock()
    api.update_token = AsyncMock()
    api.get_vehicles_vins = AsyncMock(return_value={VIN: {"modelYear": 2024}})
    return VolvoCoordinator(hass, api, 30, powertrain)


def _coordinator_with_real_vehicle(hass):
    coordinator = VolvoCoordinator(hass, MagicMock(), 300)
    vehicle = Vehicle(VIN, MagicMock(), True, supports_electric=False)
    coordinator.data = [vehicle]
    coordinator._vehicles[VIN] = vehicle
    coordinator._vehicle_order.append(VIN)
    return coordinator, vehicle


# ---------------------------------------------------------------------------
# Vehicle: unknown is not a value
# ---------------------------------------------------------------------------


async def test_failed_sources_stay_unknown_on_first_poll():
    """A partly failed first poll must not publish 'unlocked' and 0 L."""
    api = MagicMock()
    api.get_channel = AsyncMock()
    unavailable = AsyncMock(side_effect=RuntimeError("UNAVAILABLE"))
    for name in (
        "get_exterior",
        "get_fuel_status",
        "get_availability",
        "get_location",
        "get_health",
        "get_car_preferences",
        "get_engine_status",
    ):
        setattr(api, name, unavailable)
    odometer = GetOdometerResp()
    odometer.data.odometerMeters = 85_000_000
    api.get_odometer = AsyncMock(return_value=odometer)
    vehicle = Vehicle(VIN, api, True, supports_electric=False)

    await vehicle.update()

    assert vehicle.odo_meter == 85000
    assert vehicle.car_locked is None
    assert vehicle.front_left_door_open is None
    assert vehicle.fuel_amount is None
    assert vehicle.distance_to_empty is None
    assert vehicle.service_warning is None


def test_lock_state_expires_sooner_than_slow_sources():
    """An hour-old 'locked' must not be shown as the current lock state."""
    vehicle = Vehicle(VIN, MagicMock(), True, supports_electric=False)
    vehicle.car_locked = True
    vehicle.fuel_amount = 40
    vehicle._save_to_cache("exterior", {"car_locked": True})
    vehicle._save_to_cache("fuel", {"fuel_amount": 40})
    stale = datetime.now(timezone.utc) - timedelta(minutes=30)
    vehicle._cache_timestamp["exterior"] = stale
    vehicle._cache_timestamp["fuel"] = stale

    assert vehicle._restore_from_cache("exterior") is False
    assert vehicle.car_locked is None
    assert vehicle._restore_from_cache("fuel") is True
    assert vehicle.fuel_amount == 40


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


def test_window_lock_is_unknown_when_a_window_state_is_unknown(
    hass: HomeAssistant,
):
    """Missing window data used to fall through to 'locked'."""
    coordinator, vehicle = _coordinator_with_real_vehicle(hass)
    entity = VolvoWindowSensor(coordinator, 0, "window_lock")
    windows = (
        "front_left_window_open",
        "front_right_window_open",
        "rear_left_window_open",
        "rear_right_window_open",
    )

    assert entity.is_locked is None

    for window in windows:
        setattr(vehicle, window, False)
    assert entity.is_locked is True

    vehicle.rear_left_window_open = None
    assert entity.is_locked is None

    vehicle.front_left_window_open = True
    assert entity.is_locked is False


def test_engine_switch_is_unknown_until_a_source_answers(hass: HomeAssistant):
    coordinator, vehicle = _coordinator_with_real_vehicle(hass)
    entity = VolvoEngineSwitch(coordinator, 0, "engine_switch")

    assert entity.is_on is None

    # Pre-2022 cars never poll the remote-start source; one known value is
    # enough to report a definite state.
    vehicle.engine_running = False
    assert entity.is_on is False

    vehicle.engine_remote_running = True
    assert entity.is_on is True


async def test_control_confirmation_delays_escalate():
    """Three polls a second apart finished before the car could react."""
    coordinator = MagicMock()
    coordinator.async_force_refresh = AsyncMock(return_value=False)
    sleep = AsyncMock()

    with patch("asyncio.sleep", new=sleep):
        with pytest.raises(HomeAssistantError, match="could not be confirmed"):
            await async_confirm_vehicle_state(
                coordinator,
                lambda: True,
                retry_delay=3,
                description="Test control",
            )

    assert [call.args[0] for call in sleep.await_args_list] == [3, 6, 12]


# ---------------------------------------------------------------------------
# Auth layer
# ---------------------------------------------------------------------------


async def test_incomplete_login_response_is_a_clean_error():
    """A response without refreshToken raised a bare KeyError."""
    api = _base_api()
    payload = _session_payload()
    del payload["data"]["refreshToken"]
    api.digitalvolvo_post.return_value = payload

    with pytest.raises(VolvoAPIError, match="refreshToken"):
        await api.login()

    assert api._refresh_token == ""
    assert api._digitalvolvo_access_token == ""
    assert api._vocapi_access_token == ""


async def test_incomplete_refresh_response_keeps_the_session():
    """A half-applied refresh left new tokens with the old expiry."""
    api = _base_api()
    api._refresh_token = "current_refresh"
    api._digitalvolvo_access_token = "current_access"
    api._access_token_expire_at = 0
    api.digitalvolvo_get.return_value = _session_payload("new", jwtToken="")

    with pytest.raises(VolvoAPIError, match="jwtToken"):
        await api.login()

    assert api._refresh_token == "current_refresh"
    assert api._digitalvolvo_access_token == "current_access"
    api.digitalvolvo_post.assert_not_awaited()


async def test_refresh_403_keeps_the_token_until_it_persists():
    """One risk-control 403 used to discard the token and password-login."""
    api = _base_api()
    api._refresh_token = "still_valid_refresh"
    api._access_token_expire_at = 0
    api.digitalvolvo_get.side_effect = VolvoAuthExpiredError("403", status=403)
    api.digitalvolvo_post.return_value = _session_payload("recovered")

    for _ in range(REFRESH_FORBIDDEN_TOLERANCE - 1):
        with pytest.raises(VolvoRefreshForbiddenError):
            await api.login()
        assert api._refresh_token == "still_valid_refresh"
    api.digitalvolvo_post.assert_not_awaited()

    # A persistent streak is treated as a real rejection after all.
    await api.login()

    api.digitalvolvo_post.assert_awaited_once()
    assert api._refresh_token == "recovered_refresh"
    assert api._refresh_forbidden_streak == 0


async def test_refresh_401_is_still_an_immediate_rejection():
    api = _base_api()
    api._refresh_token = "dead_refresh"
    api._access_token_expire_at = 0
    api.digitalvolvo_get.side_effect = VolvoAuthExpiredError("401", status=401)
    api.digitalvolvo_post.return_value = _session_payload("recovered")

    await api.login()

    api.digitalvolvo_post.assert_awaited_once()
    assert api._refresh_token == "recovered_refresh"


async def test_password_403_is_classified_apart_from_a_rejection():
    forbidden = _base_api()
    forbidden.digitalvolvo_post.side_effect = VolvoAuthExpiredError(
        "403", status=403
    )
    with pytest.raises(VolvoAuthForbiddenError):
        await forbidden.login()

    rejected = _base_api()
    rejected.digitalvolvo_post.side_effect = VolvoAuthExpiredError(
        "401", status=401
    )
    with pytest.raises(VolvoAuthError) as excinfo:
        await rejected.login()
    assert not isinstance(excinfo.value, VolvoAuthForbiddenError)


# ---------------------------------------------------------------------------
# gRPC layer
# ---------------------------------------------------------------------------


def _unauthenticated():
    return grpc.aio.AioRpcError(
        grpc.StatusCode.UNAUTHENTICATED,
        grpc.aio.Metadata(),
        grpc.aio.Metadata(),
    )


async def test_grpc_unauthenticated_invalidates_an_established_session():
    """Only REST 401/403 used to trigger re-authentication."""
    api = VehicleAPI(MagicMock(), USERNAME, "pw")
    api._access_token_expire_at = int(time.time()) + 3600

    with pytest.raises(grpc.aio.AioRpcError):
        await api._first_response(FakeCall([_unauthenticated()]))

    assert api._access_token_expire_at == 0


async def test_grpc_unauthenticated_does_not_churn_a_fresh_session():
    api = VehicleAPI(MagicMock(), USERNAME, "pw")
    expires_at = int(time.time()) + 3600
    api._access_token_expire_at = expires_at
    api._session_established_monotonic = time.monotonic()

    with pytest.raises(grpc.aio.AioRpcError):
        await api._first_response(FakeCall([_unauthenticated()]))

    assert api._access_token_expire_at == expires_at


async def test_invocation_reports_a_failure_that_follows_sent():
    """SENT followed by CAR_OFFLINE used to be reported as success."""
    api = VehicleAPI(MagicMock(), USERNAME, "pw")
    call = FakeCall(
        [_ack(invocationStatus.SENT), _ack(invocationStatus.CAR_OFFLINE)]
    )

    with pytest.raises(HomeAssistantError, match="车辆离线"):
        await api._run_invocation(call, "Test", follow_up_seconds=1)

    assert call.cancelled is True


async def test_invocation_accepts_sent_when_the_stream_just_ends():
    api = VehicleAPI(MagicMock(), USERNAME, "pw")
    call = FakeCall([_ack(invocationStatus.SENT)])

    response = await api._run_invocation(call, "Test", follow_up_seconds=1)

    assert response.data.status == invocationStatus.SENT
    assert call.cancelled is True


async def test_invocation_follow_up_wait_is_bounded():
    """A stream the server keeps open must not block the command."""
    api = VehicleAPI(MagicMock(), USERNAME, "pw")
    call = FakeCall([_ack(invocationStatus.SENT)], hang=True)

    response = await api._run_invocation(
        call, "Test", follow_up_seconds=0.01
    )

    assert response.data.status == invocationStatus.SENT
    assert call.cancelled is True


async def test_invocation_rejects_a_refused_first_message():
    api = VehicleAPI(MagicMock(), USERNAME, "pw")
    call = FakeCall([_ack(invocationStatus.CAR_OFFLINE)])

    with pytest.raises(HomeAssistantError):
        await api._run_invocation(call, "Test", follow_up_seconds=1)


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


async def test_scheduled_poll_on_the_interval_boundary_is_not_dropped(
    hass: HomeAssistant,
):
    """Microseconds of loop jitter used to skip the poll and double the gap."""
    coordinator = _coordinator(hass)
    coordinator.data = [MagicMock()]
    coordinator._last_update_started_at = time.monotonic() - 29.999

    with patch(
        "custom_components.volvooncall_cn.Vehicle.update", new=AsyncMock()
    ):
        await coordinator._async_update_data()

    coordinator.volvo_api.login.assert_awaited_once()


async def test_early_unforced_refresh_is_still_throttled(hass: HomeAssistant):
    coordinator = _coordinator(hass)
    cached = [MagicMock()]
    coordinator.data = cached
    coordinator._last_update_started_at = time.monotonic() - 15

    assert await coordinator._async_update_data() is cached
    coordinator.volvo_api.login.assert_not_awaited()


async def test_persistent_session_rejection_backs_the_cycle_off(
    hass: HomeAssistant,
):
    """A lasting 403 used to cost four requests every scan interval."""
    coordinator = _coordinator(hass)
    api = coordinator.volvo_api
    api.get_vehicles_vins = AsyncMock(
        side_effect=VolvoAuthExpiredError("403", status=403)
    )
    cached = [MagicMock()]
    coordinator.data = cached

    assert await coordinator._async_update_data() is cached
    assert api.get_vehicles_vins.await_count == 2  # one in-cycle recovery
    first_delay = coordinator._auth_backoff_until_monotonic - time.monotonic()
    assert AUTH_BACKOFF_BASE_SECONDS - 5 < first_delay <= AUTH_BACKOFF_BASE_SECONDS

    # Inside the window nothing is sent, not even for a forced refresh.
    coordinator._pending_force_refreshes += 1
    assert await coordinator._async_update_data() is cached
    assert api.get_vehicles_vins.await_count == 2

    # The window grows while the rejection lasts.
    coordinator._auth_backoff_until_monotonic = 0.0
    coordinator._pending_force_refreshes += 1
    await coordinator._async_update_data()
    second_delay = coordinator._auth_backoff_until_monotonic - time.monotonic()
    assert second_delay > first_delay * 1.5


async def test_backoff_window_does_not_serve_stale_data_forever(
    hass: HomeAssistant,
):
    """Suppressed polls count no failures, so the cache needs a time limit."""
    coordinator = _coordinator(hass)
    coordinator.data = [MagicMock()]
    coordinator._consecutive_failures = 1
    coordinator._first_failure_monotonic = time.monotonic() - 3600
    coordinator._auth_backoff_until_monotonic = time.monotonic() + 600

    with pytest.raises(UpdateFailed) as excinfo:
        await coordinator._async_update_data()

    assert 0 < excinfo.value.retry_after <= 600
    coordinator.volvo_api.login.assert_not_awaited()


async def test_server_retry_after_is_capped(hass: HomeAssistant):
    coordinator = _coordinator(hass)
    coordinator.volvo_api.login = AsyncMock(
        side_effect=VolvoRateLimitError("busy", 10**9)
    )
    coordinator.data = [MagicMock()]

    await coordinator._async_update_data()

    window = coordinator._server_retry_after_monotonic - time.monotonic()
    assert 0 < window <= MAX_SERVER_RETRY_AFTER


async def test_password_403_needs_a_longer_streak_before_reauth(
    hass: HomeAssistant,
):
    """Three risk-control 403s used to demand a manual reauthentication."""
    assert LOGIN_FORBIDDEN_REAUTH_THRESHOLD - 1 <= FAILURE_GRACE_CYCLES
    coordinator = _coordinator(hass)
    coordinator.volvo_api.login = AsyncMock(
        side_effect=VolvoAuthForbiddenError("Login rejected by server: 403")
    )
    cached = [MagicMock()]
    coordinator.data = cached

    for _ in range(LOGIN_FORBIDDEN_REAUTH_THRESHOLD - 1):
        coordinator._pending_force_refreshes += 1
        assert await coordinator._async_update_data() is cached
        coordinator._auth_backoff_until_monotonic = 0.0

    coordinator._pending_force_refreshes += 1
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_card_registration_failure_does_not_break_setup(
    hass: HomeAssistant,
):
    """The cards depend on Lovelace internals; the car must still load."""
    original_http = getattr(hass, "http", None)
    hass.http = MagicMock()
    hass.http.async_register_static_paths = AsyncMock(
        side_effect=RuntimeError("lovelace changed")
    )
    try:
        assert await async_setup(hass, {}) is True
    finally:
        hass.http = original_http


async def test_unload_flushes_the_live_session_to_disk(hass: HomeAssistant):
    """A reload inside the save debounce re-read the previous refresh token."""
    api = VehicleBaseAPI(MagicMock(), USERNAME, "pw")
    api._refresh_token = "live_refresh"
    store = MagicMock()
    store.async_save = AsyncMock()
    hass.data[AUTH_STORES_DATA] = {"entry": [store]}

    await _async_flush_tokens(hass, "entry", api)
    store.async_save.assert_awaited_once()
    assert store.async_save.await_args.args[0]["refresh_token"] == "live_refresh"

    # Stores removed after a credential change must not be written back.
    store.async_save.reset_mock()
    hass.data[AUTH_STORES_DATA] = {}
    await _async_flush_tokens(hass, "entry", api)
    store.async_save.assert_not_awaited()
    hass.data.pop(AUTH_STORES_DATA, None)


# ---------------------------------------------------------------------------
# Config flow
# ---------------------------------------------------------------------------


async def test_config_flow_shares_the_password_login_cooldown(
    hass: HomeAssistant,
):
    """Every submit of the form used to be a real password login."""
    post = AsyncMock(side_effect=VolvoAPIError("密码错误"))

    with patch.object(VehicleAPI, "digitalvolvo_post", post):
        first = await volvo_validation(hass, USERNAME, "wrong-1")
        second = await volvo_validation(hass, USERNAME, "wrong-2")

    assert "密码错误" in first["base"]
    assert second == {"base": "login_cooldown"}
    post.assert_awaited_once()


async def test_config_flow_honours_the_running_integrations_cooldown(
    hass: HomeAssistant,
):
    running_api = SimpleNamespace(
        _username=USERNAME, _last_password_login_at=int(time.time())
    )
    hass.data[DOMAIN] = {"entry": SimpleNamespace(volvo_api=running_api)}
    post = AsyncMock(return_value=_session_payload())

    try:
        with patch.object(VehicleAPI, "digitalvolvo_post", post):
            errors = await volvo_validation(hass, USERNAME, "pw")
    finally:
        hass.data.pop(DOMAIN, None)

    assert errors == {"base": "login_cooldown"}
    post.assert_not_awaited()
