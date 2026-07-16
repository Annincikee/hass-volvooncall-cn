"""Auth session tests: prefer the refresh token over password re-login."""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.volvooncall_cn.volvooncall_base import VehicleBaseAPI


def _auth_payload(prefix, expires_in=1800):
    return {
        "success": True,
        "data": {
            "refreshToken": f"{prefix}_refresh",
            "globalAccessToken": f"{prefix}_global",
            "accessToken": f"{prefix}_access",
            "jwtToken": f"{prefix}_jwt",
            "expiresIn": expires_in,
        },
    }


def _api():
    api = VehicleBaseAPI(MagicMock(), "TEST_USERNAME", "TEST_PASSWORD")
    api.digitalvolvo_get = AsyncMock()
    api.digitalvolvo_post = AsyncMock()
    return api


@pytest.mark.asyncio
async def test_update_token_refreshes_without_password_login():
    """Near expiry with a live refresh token must use the refresh endpoint."""
    api = _api()
    api._refresh_token = "old_refresh"
    api._access_token_expire_at = int(time.time()) + 60  # < 10 min headroom
    api.digitalvolvo_get.return_value = _auth_payload("refreshed")

    await api.update_token()

    api.digitalvolvo_get.assert_awaited_once()
    assert "refreshToken" in api.digitalvolvo_get.await_args.args[0]
    api.digitalvolvo_post.assert_not_awaited()  # no password re-login
    assert api._refresh_token == "refreshed_refresh"
    assert api._digitalvolvo_access_token == "refreshed_access"


@pytest.mark.asyncio
async def test_login_is_a_noop_while_the_session_is_alive():
    """A valid session must not trigger a password login on every poll."""
    api = _api()
    api._refresh_token = "live_refresh"
    api._access_token_expire_at = int(time.time()) + 300  # still valid

    await api.login()

    api.digitalvolvo_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_login_performs_password_auth_without_a_session():
    """The first login (no refresh token) must authenticate with the password."""
    api = _api()
    api.digitalvolvo_post.return_value = _auth_payload("fresh")

    await api.login()

    api.digitalvolvo_post.assert_awaited_once()
    assert "auth" in api.digitalvolvo_post.await_args.args[0]
    assert api._refresh_token == "fresh_refresh"


@pytest.mark.asyncio
async def test_expired_refresh_token_falls_back_to_password_login():
    """A dead refresh token must be recovered with a full password login."""
    api = _api()
    api._refresh_token = "dead_refresh"
    api._access_token_expire_at = int(time.time()) + 60
    api.digitalvolvo_get.side_effect = RuntimeError("refresh token expired")
    api.digitalvolvo_post.return_value = _auth_payload("recovered")

    await api.update_token()

    api.digitalvolvo_get.assert_awaited_once()  # refresh attempted first
    api.digitalvolvo_post.assert_awaited_once()  # then password recovery
    assert "auth" in api.digitalvolvo_post.await_args.args[0]
    assert api._refresh_token == "recovered_refresh"


@pytest.mark.asyncio
async def test_login_then_update_token_issue_no_auth_calls_when_healthy():
    """The coordinator's login+update_token pair is silent on a healthy token."""
    api = _api()
    api._refresh_token = "healthy_refresh"
    api._access_token_expire_at = int(time.time()) + 3600

    await api.login()
    await api.update_token()

    api.digitalvolvo_post.assert_not_awaited()
    api.digitalvolvo_get.assert_not_awaited()
