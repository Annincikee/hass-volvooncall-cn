"""Regression tests for the long-running-stability and security fixes.

Covers:
- 401/403 responses invalidate the access token, are never retried, and the
  raised error/message never contains the refresh token (log-leak fix).
- Other 4xx responses fail fast instead of hammering the API.
- Password logins are rate limited locally (server risk-control mitigation).
- The coordinator serves cached data only for a bounded number of failures,
  then marks entities unavailable via UpdateFailed.
- A revoked session (403) is recovered within the same update cycle.
- Repeated credential rejections raise ConfigEntryAuthFailed (reauth flow).
- Vehicle objects are reused across polls so their fallback cache works.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.volvooncall_cn import (
    FAILURE_GRACE_CYCLES,
    LOGIN_REJECTION_REAUTH_THRESHOLD,
    VolvoCoordinator,
)
from custom_components.volvooncall_cn.volvooncall_base import (
    MAX_RETRIES,
    VehicleBaseAPI,
    VolvoAPIError,
    VolvoApiHttpError,
    VolvoAuthError,
    VolvoAuthExpiredError,
    VolvoAuthThrottledError,
)

SECRET_URL = (
    "https://apigateway.digitalvolvo.com/app/iam/api/v1/refreshToken"
    "?refreshToken=secret-refresh-token"
)


def _http_error(status, url=SECRET_URL):
    request_info = MagicMock()
    request_info.real_url = url
    return aiohttp.ClientResponseError(request_info, (), status=status, message="")


def _session_raising(error):
    response = MagicMock()
    response.raise_for_status = MagicMock(side_effect=error)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=None)
    session = MagicMock()
    session.request = MagicMock(return_value=context)
    return session


# =============================================================================
# HTTP layer
# =============================================================================


@pytest.mark.asyncio
async def test_403_is_not_retried_and_invalidates_token(caplog):
    """A 403 must fail fast, mark the token dead and never leak the token."""
    session = _session_raising(_http_error(403))
    api = VehicleBaseAPI(session, "TEST_USERNAME", "TEST_PASSWORD")
    api._access_token_expire_at = 4102444800  # far future

    with pytest.raises(VolvoAuthExpiredError) as excinfo:
        await api.digitalvolvo_get(SECRET_URL, {})

    session.request.assert_called_once()  # deterministic: no replay
    assert api._access_token_expire_at == 0  # next cycle re-authenticates
    assert "secret-refresh-token" not in str(excinfo.value)
    assert "secret-refresh-token" not in caplog.text


@pytest.mark.asyncio
async def test_4xx_is_not_retried(caplog):
    """Deterministic client errors (e.g. 424) must not be replayed."""
    session = _session_raising(_http_error(424))
    api = VehicleBaseAPI(session, "TEST_USERNAME", "TEST_PASSWORD")

    with pytest.raises(VolvoApiHttpError) as excinfo:
        await api.digitalvolvo_get(SECRET_URL, {})

    session.request.assert_called_once()
    assert "secret-refresh-token" not in str(excinfo.value)
    assert "secret-refresh-token" not in caplog.text


@pytest.mark.asyncio
async def test_5xx_is_retried_with_backoff(monkeypatch):
    """Transient server errors keep the retry loop."""
    session = _session_raising(_http_error(502))
    api = VehicleBaseAPI(session, "TEST_USERNAME", "TEST_PASSWORD")
    monkeypatch.setattr(
        "custom_components.volvooncall_cn.volvooncall_base.asyncio.sleep",
        AsyncMock(),
    )

    with pytest.raises(VolvoApiHttpError):
        await api.digitalvolvo_get(SECRET_URL, {})

    assert session.request.call_count == MAX_RETRIES


@pytest.mark.asyncio
async def test_password_login_is_rate_limited_locally():
    """Back-to-back password logins must be suppressed by the cooldown."""
    api = VehicleBaseAPI(MagicMock(), "TEST_USERNAME", "TEST_PASSWORD")
    api.digitalvolvo_post = AsyncMock(side_effect=VolvoAPIError("bad password"))

    with pytest.raises(VolvoAuthError):
        await api.login()

    api.digitalvolvo_post.reset_mock()
    with pytest.raises(VolvoAuthThrottledError):
        await api.login()

    api.digitalvolvo_post.assert_not_awaited()  # never reached the server


@pytest.mark.asyncio
async def test_token_listener_receives_tokens_after_login():
    """Successful logins must publish tokens for persistence."""
    api = VehicleBaseAPI(MagicMock(), "TEST_USERNAME", "TEST_PASSWORD")
    api.digitalvolvo_post = AsyncMock(
        return_value={
            "success": True,
            "data": {
                "refreshToken": "fresh_refresh",
                "globalAccessToken": "fresh_global",
                "accessToken": "fresh_access",
                "jwtToken": "fresh_jwt",
                "expiresIn": 1800,
            },
        }
    )
    received = []
    api.set_token_listener(received.append)

    await api.login()

    assert received and received[-1]["refresh_token"] == "fresh_refresh"

    # And a new API instance seeded from those tokens skips the password login.
    api2 = VehicleBaseAPI(MagicMock(), "TEST_USERNAME", "TEST_PASSWORD")
    api2.digitalvolvo_post = AsyncMock()
    api2.import_tokens(received[-1])
    await api2.login()
    api2.digitalvolvo_post.assert_not_awaited()


# =============================================================================
# Coordinator layer
# =============================================================================


def _failing_coordinator(hass, error):
    api = AsyncMock()
    api.login = AsyncMock(side_effect=error)
    coordinator = VolvoCoordinator(hass, api, 30)
    coordinator.data = [MagicMock()]
    return coordinator


@pytest.mark.asyncio
async def test_coordinator_grace_window_then_unavailable(hass: HomeAssistant):
    """Cached data may be served only FAILURE_GRACE_CYCLES times in a row."""
    coordinator = _failing_coordinator(hass, Exception("boom"))

    for _ in range(FAILURE_GRACE_CYCLES):
        coordinator._force_next_refresh = True
        result = await coordinator._async_update_data()
        assert result is coordinator.data  # stale but explicit

    coordinator._force_next_refresh = True
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


@pytest.mark.asyncio
async def test_coordinator_reauth_after_repeated_login_rejections(
    hass: HomeAssistant,
):
    """Persistent credential rejections must trigger the reauth flow."""
    coordinator = _failing_coordinator(
        hass, VolvoAuthError("Login rejected by server: bad password")
    )

    for _ in range(LOGIN_REJECTION_REAUTH_THRESHOLD - 1):
        coordinator._force_next_refresh = True
        result = await coordinator._async_update_data()
        assert result is coordinator.data

    coordinator._force_next_refresh = True
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


@pytest.mark.asyncio
async def test_revoked_session_recovers_within_one_cycle(hass: HomeAssistant):
    """A 403 on the vehicle list re-authenticates and retries immediately."""
    api = AsyncMock()
    api.get_vehicles_vins = AsyncMock(
        side_effect=[VolvoAuthExpiredError("403"), {}]
    )
    coordinator = VolvoCoordinator(hass, api, 30)

    result = await coordinator._async_update_data()

    assert result == []
    assert api.get_vehicles_vins.await_count == 2
    # login() ran once for the normal pass, update_token() ran once at the
    # start plus once for the in-cycle recovery.
    assert api.update_token.await_count == 2


@pytest.mark.asyncio
async def test_vehicles_are_reused_across_polls(hass: HomeAssistant):
    """The same Vehicle object must survive polls so its cache is useful."""
    api = AsyncMock()
    api.get_vehicles_vins = AsyncMock(
        return_value={"TEST_VIN_12345678": {"modelYear": "2024"}}
    )
    coordinator = VolvoCoordinator(hass, api, 30)

    with patch(
        "custom_components.volvooncall_cn.volvooncall_cn.Vehicle.update",
        new_callable=AsyncMock,
    ):
        first = await coordinator._async_update_data()
        coordinator.data = first
        coordinator._force_next_refresh = True
        second = await coordinator._async_update_data()

    assert len(first) == len(second) == 1
    assert first[0] is second[0]


@pytest.mark.asyncio
async def test_entity_id_is_slugified(hass: HomeAssistant):
    """Entity ids must be valid (lowercase) even though the VIN is uppercase."""
    from homeassistant.const import Platform

    from custom_components.volvooncall_cn import VolvoEntity

    vehicle = MagicMock()
    vehicle.vin = "TEST_VIN_12345678"
    coordinator = MagicMock()
    coordinator.data = [vehicle]

    entity = VolvoEntity(coordinator, 0, "odo_meter", Platform.SENSOR)

    assert entity.entity_id == "sensor.test_vin_12345678_odometer"
    assert entity.entity_id == entity.entity_id.lower()
    assert entity.unique_id == "TEST_VIN_12345678-odo_meter"
