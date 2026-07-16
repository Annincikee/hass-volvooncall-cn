"""Security regression tests."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.volvooncall_cn.volvooncall_base import (
    VehicleBaseAPI,
    VolvoAPIError,
    redact_sensitive,
    vehicle_log_ref,
)


def test_redact_sensitive_removes_tokens_from_log_strings():
    phone = "138" + "0013" + "8000"
    vin = "LVSHFAAL" + "1MF" + "123456"
    raw = (
        "url='https://apigateway.digitalvolvo.com/app/iam/api/v1/"
        "refreshToken?refreshToken=secret-refresh-token' "
        "authorization: Bearer secret-access-token "
        "X-Token: secret-x-token "
        "password=correct-horse-battery-staple "
        "phone=TEST_PRIVATE_PHONE&vin=TEST_PRIVATE_VIN "
        f"phoneNumber: {phone} vinCode: {vin} "
        "deviceid=device-123 uuid=uuid-123 connectorId=pile-123 "
        "orderNo=order-123&tradeNo=trade-123&memberId=member-123 "
        "latitude=31.2304 longitude=121.4737"
    )

    redacted = redact_sensitive(raw)

    assert "secret-refresh-token" not in redacted
    assert "secret-access-token" not in redacted
    assert "secret-x-token" not in redacted
    assert "TEST_PRIVATE_PHONE" not in redacted
    assert "TEST_PRIVATE_VIN" not in redacted
    assert "correct-horse-battery-staple" not in redacted
    assert phone not in redacted
    assert vin not in redacted
    assert "device-123" not in redacted
    assert "uuid-123" not in redacted
    assert "pile-123" not in redacted
    assert "order-123" not in redacted
    assert "trade-123" not in redacted
    assert "member-123" not in redacted
    assert "31.2304" not in redacted
    assert "121.4737" not in redacted
    assert "refreshToken=<redacted>" in redacted
    assert "authorization: <redacted>" in redacted
    assert "X-Token: <redacted>" in redacted


def test_vehicle_log_ref_is_stable_and_does_not_expose_vin():
    vin = "LVSHFAAL" + "1MF" + "123456"

    reference = vehicle_log_ref(vin)

    assert reference == vehicle_log_ref(vin)
    assert reference.startswith("vehicle-")
    assert vin not in reference
    assert len(reference) == len("vehicle-") + 10


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


@pytest.mark.asyncio
async def test_transport_log_does_not_include_traceback_or_sensitive_values(caplog):
    phone = "138" + "0013" + "8000"
    vin = "LVSHFAAL" + "1MF" + "123456"
    session = MagicMock()
    session.request.side_effect = RuntimeError(
        f"phone={phone} vin={vin} password=secret"
    )
    api = VehicleBaseAPI(session, phone, "secret")

    with caplog.at_level("WARNING"), pytest.raises(RuntimeError):
        await api._request_digitalvolvo("GET", "https://example.invalid", {}, max_attempts=1)

    assert phone not in caplog.text
    assert vin not in caplog.text
    assert "password=secret" not in caplog.text
    assert "Traceback" not in caplog.text
