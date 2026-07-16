"""Security regression tests."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.volvooncall_cn.volvooncall_base import (
    VehicleBaseAPI,
    VolvoAPIError,
    redact_sensitive,
)


def test_redact_sensitive_removes_tokens_from_log_strings():
    raw = (
        "url='https://apigateway.digitalvolvo.com/app/iam/api/v1/"
        "refreshToken?refreshToken=secret-refresh-token' "
        "authorization: Bearer secret-access-token "
        "X-Token: secret-x-token "
        "phone=TEST_PRIVATE_PHONE&vin=TEST_PRIVATE_VIN"
    )

    redacted = redact_sensitive(raw)

    assert "secret-refresh-token" not in redacted
    assert "secret-access-token" not in redacted
    assert "secret-x-token" not in redacted
    assert "TEST_PRIVATE_PHONE" not in redacted
    assert "TEST_PRIVATE_VIN" not in redacted
    assert "refreshToken=<redacted>" in redacted
    assert "Bearer <redacted>" in redacted
    assert "X-Token: <redacted>" in redacted


@pytest.mark.asyncio
async def test_business_rejection_is_not_retried():
    """A deterministic API rejection must never be replayed."""
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = AsyncMock(
        return_value={
            "success": False,
            "code": 400,
            "msg": "rejected",
        }
    )
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=None)
    session = MagicMock()
    session.request = MagicMock(return_value=context)
    api = VehicleBaseAPI(session, "TEST_USERNAME", "TEST_PASSWORD")

    with pytest.raises(VolvoAPIError, match="rejected"):
        await api.digitalvolvo_post(
            "https://apigateway.digitalvolvo.com/app/test",
            {},
            {},
        )

    session.request.assert_called_once()
