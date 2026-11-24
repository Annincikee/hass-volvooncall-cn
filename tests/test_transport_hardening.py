"""Transport and refresh-token hardening regression tests."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.volvooncall_cn.volvooncall_base import (
    MAX_RETRIES,
    VehicleBaseAPI,
    VolvoAuthError,
    VolvoRateLimitError,
    VolvoTransientError,
)

REFRESH_TOKEN = "refresh +&?=/token"


def _http_error(status, *, headers=None):
    request_info = MagicMock()
    request_info.real_url = (
        "https://apigateway.digitalvolvo.com/app/iam/api/v1/refreshToken"
        "?refreshToken=redacted-by-assertion"
    )
    return aiohttp.ClientResponseError(
        request_info,
        (),
        status=status,
        message="temporary failure",
        headers=headers,
    )


def _session_raising(error):
    response = MagicMock()
    response.raise_for_status = MagicMock(side_effect=error)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=None)
    session = MagicMock()
    session.request = MagicMock(return_value=context)
    return session


def _refreshing_api(session):
    api = VehicleBaseAPI(session, "TEST_USERNAME", "TEST_PASSWORD")
    api._refresh_token = REFRESH_TOKEN
    api._access_token_expire_at = int(time.time()) + 60
    return api


@pytest.mark.asyncio
async def test_429_is_not_quick_retried_and_exposes_retry_after(monkeypatch):
    session = _session_raising(_http_error(429, headers={"Retry-After": "120"}))
    api = _refreshing_api(session)
    sleep = AsyncMock()
    monkeypatch.setattr(
        "custom_components.volvooncall_cn.volvooncall_base.asyncio.sleep",
        sleep,
    )

    with pytest.raises(VolvoRateLimitError) as excinfo:
        await api.update_token()

    assert excinfo.value.retry_after == 120.0
    session.request.assert_called_once()
    sleep.assert_not_awaited()
    assert api._refresh_token == REFRESH_TOKEN


@pytest.mark.asyncio
async def test_refresh_5xx_preserves_token_and_skips_password_login(
    monkeypatch,
):
    session = _session_raising(_http_error(503))
    api = _refreshing_api(session)
    sleep = AsyncMock()
    monkeypatch.setattr(
        "custom_components.volvooncall_cn.volvooncall_base.asyncio.sleep",
        sleep,
    )

    with pytest.raises(VolvoTransientError):
        await api.update_token()

    assert session.request.call_count == MAX_RETRIES
    assert sleep.await_count == MAX_RETRIES - 1
    assert api._refresh_token == REFRESH_TOKEN


@pytest.mark.asyncio
async def test_refresh_timeout_preserves_token_and_skips_password_login(
    monkeypatch,
):
    session = MagicMock()
    session.request = MagicMock(side_effect=asyncio.TimeoutError("timed out"))
    api = _refreshing_api(session)
    sleep = AsyncMock()
    monkeypatch.setattr(
        "custom_components.volvooncall_cn.volvooncall_base.asyncio.sleep",
        sleep,
    )

    with pytest.raises(asyncio.TimeoutError):
        await api.update_token()

    assert session.request.call_count == MAX_RETRIES
    assert sleep.await_count == MAX_RETRIES - 1
    assert api._refresh_token == REFRESH_TOKEN


@pytest.mark.asyncio
async def test_malformed_vehicle_entry_does_not_hide_valid_vehicle():
    api = VehicleBaseAPI(MagicMock(), "TEST_USERNAME", "TEST_PASSWORD")
    api.get_vehicles = AsyncMock(
        return_value=[
            None,
            {},
            {"vinCode": ""},
            {"vinCode": "VALIDVIN000000001", "modelYear": "invalid"},
        ]
    )

    vehicles = await api.get_vehicles_vins()

    assert vehicles == {
        "VALIDVIN000000001": {
            "vinCode": "VALIDVIN000000001",
            "modelYear": "invalid",
        }
    }


@pytest.mark.asyncio
async def test_password_endpoint_403_is_a_credential_rejection(monkeypatch):
    session = _session_raising(_http_error(403))
    api = VehicleBaseAPI(session, "TEST_USERNAME", "TEST_PASSWORD")
    monkeypatch.setattr(
        "custom_components.volvooncall_cn.volvooncall_base.asyncio.sleep",
        AsyncMock(),
    )

    with pytest.raises(VolvoAuthError):
        await api.login()

    session.request.assert_called_once()
