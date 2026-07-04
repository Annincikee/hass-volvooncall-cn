"""Tests for parked climatization control."""

from unittest.mock import MagicMock, patch

import pytest

from custom_components.volvooncall_cn.proto.invocation_pb2 import (
    ClimatizationStartReq,
    ClimatizationStopReq,
    SUCCESS,
    invocationCommResp,
    invocationData,
)
from custom_components.volvooncall_cn.volvooncall_cn import VehicleAPI


@pytest.mark.asyncio
async def test_climatization_start_has_no_duration():
    """Starting climatization must not reuse the engine duration setting."""
    api = VehicleAPI(session=None, username="user", password="password")
    api.channel = MagicMock()
    response = invocationCommResp(data=invocationData(status=SUCCESS))

    with patch(
        "custom_components.volvooncall_cn.volvooncall_cn.InvocationServiceStub"
    ) as stub_class:
        stub = stub_class.return_value
        stub.ClimatizationStart.return_value = [response]

        await api.climatization_control("TEST_VIN", True)

    request = stub.ClimatizationStart.call_args.args[0]
    assert isinstance(request, ClimatizationStartReq)
    assert request.head.vin == "TEST_VIN"
    assert request.start is True
    assert not hasattr(request, "startDurationMin")


@pytest.mark.asyncio
async def test_climatization_stop_uses_dedicated_command():
    """Stopping climatization must use its own invocation command."""
    api = VehicleAPI(session=None, username="user", password="password")
    api.channel = MagicMock()
    response = invocationCommResp(data=invocationData(status=SUCCESS))

    with patch(
        "custom_components.volvooncall_cn.volvooncall_cn.InvocationServiceStub"
    ) as stub_class:
        stub = stub_class.return_value
        stub.ClimatizationStop.return_value = [response]

        await api.climatization_control("TEST_VIN", False)

    request = stub.ClimatizationStop.call_args.args[0]
    assert isinstance(request, ClimatizationStopReq)
    assert request.head.vin == "TEST_VIN"
