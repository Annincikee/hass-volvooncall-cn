"""Tests for parked climatization control."""

from unittest.mock import AsyncMock, MagicMock

import pytest

import custom_components.volvooncall_cn.volvooncall_cn as volvo_module
from custom_components.volvooncall_cn.proto.invocation_pb2 import (
    ClimatizationStartReq,
    ClimatizationStopReq,
    invocationCommResp,
    invocationData,
    invocationStatus,
)
from custom_components.volvooncall_cn.volvooncall_cn import Vehicle, VehicleAPI


class FakeInvocationStub:
    """Capture climatization requests made through InvocationService."""

    calls = []

    def __init__(self, channel):
        self.channel = channel

    def ClimatizationStart(self, req, metadata=None, timeout=None):
        self.calls.append(("start", req, metadata, timeout))
        return [
            invocationCommResp(
                data=invocationData(status=invocationStatus.SUCCESS)
            )
        ]

    def ClimatizationStop(self, req, metadata=None, timeout=None):
        self.calls.append(("stop", req, metadata, timeout))
        return [
            invocationCommResp(
                data=invocationData(status=invocationStatus.SUCCESS)
            )
        ]


@pytest.mark.asyncio
async def test_climatization_control_uses_dedicated_start_and_stop(monkeypatch):
    """Parked climatization must not use the engine remote-start duration path."""
    FakeInvocationStub.calls = []
    monkeypatch.setattr(volvo_module, "InvocationServiceStub", FakeInvocationStub)
    api = VehicleAPI(MagicMock(), "13800000000", "password")
    api.channel = object()

    await api.climatization_control("TEST_VIN", True)
    await api.climatization_control("TEST_VIN", False)

    assert len(FakeInvocationStub.calls) == 2

    start_call, stop_call = FakeInvocationStub.calls
    assert start_call[0] == "start"
    assert isinstance(start_call[1], ClimatizationStartReq)
    assert start_call[1].head.vin == "TEST_VIN"
    assert start_call[1].start is True
    assert start_call[1].compartmentTemperatureCelsius == 0
    assert start_call[2] == [("vin", "TEST_VIN")]

    assert stop_call[0] == "stop"
    assert isinstance(stop_call[1], ClimatizationStopReq)
    assert stop_call[1].head.vin == "TEST_VIN"
    assert stop_call[2] == [("vin", "TEST_VIN")]


@pytest.mark.asyncio
async def test_vehicle_climatization_methods_delegate_without_duration():
    api = MagicMock()
    api.climatization_control = AsyncMock()
    vehicle = Vehicle("TEST_VIN", api, True)

    await vehicle.climatization_start()
    await vehicle.climatization_stop()

    assert api.climatization_control.await_args_list[0].args == (
        "TEST_VIN",
        True,
    )
    assert api.climatization_control.await_args_list[1].args == (
        "TEST_VIN",
        False,
    )
