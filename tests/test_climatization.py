"""Tests for parked climatization control."""

from unittest.mock import AsyncMock, MagicMock

import pytest

import custom_components.volvooncall_cn.switch as switch_module
import custom_components.volvooncall_cn.volvooncall_cn as volvo_module
from custom_components.volvooncall_cn.proto.invocation_pb2 import (
    ClimatizationStartReq,
    ClimatizationStopReq,
    invocationCommResp,
    invocationData,
    invocationStatus,
)
from custom_components.volvooncall_cn.switch import (
    CLIMATIZATION_CHARGING_RUNTIME,
    CLIMATIZATION_DEFAULT_RUNTIME,
    VolvoClimatizationSwitch,
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "connection_status",
        "charging_status",
        "charging_power",
        "expected_delay",
    ),
    [
        (None, None, None, CLIMATIZATION_DEFAULT_RUNTIME),
        ("connected_ac", "idle", 0, CLIMATIZATION_CHARGING_RUNTIME),
        ("plugged_in", "idle", 0, CLIMATIZATION_CHARGING_RUNTIME),
        ("disconnected", "charging", 0, CLIMATIZATION_CHARGING_RUNTIME),
        ("disconnected", "idle", 3.6, CLIMATIZATION_CHARGING_RUNTIME),
    ],
)
async def test_climatization_switch_schedules_local_auto_off(
    monkeypatch,
    connection_status,
    charging_status,
    charging_power,
    expected_delay,
):
    api = MagicMock()
    api.climatization_control = AsyncMock()
    vehicle = Vehicle("TEST_VIN", api, True)
    vehicle.charger_connection_status = connection_status
    vehicle.battery_charging_status = charging_status
    vehicle.charging_power = charging_power
    coordinator = MagicMock()
    coordinator.data = [vehicle]
    switch = VolvoClimatizationSwitch(
        coordinator, 0, "climatization_switch"
    )
    switch._hass = MagicMock()
    switch.async_write_ha_state = MagicMock()
    scheduled = {}

    def fake_async_call_later(hass, delay, action):
        scheduled["hass"] = hass
        scheduled["delay"] = delay
        scheduled["action"] = action
        scheduled["cancel"] = MagicMock()
        return scheduled["cancel"]

    monkeypatch.setattr(
        switch_module, "async_call_later", fake_async_call_later
    )

    await switch.async_turn_on()

    assert switch.is_on is True
    assert scheduled["delay"] == expected_delay
    api.climatization_control.assert_awaited_once_with("TEST_VIN", True)

    scheduled["action"](None)

    assert switch.is_on is False
    assert switch.async_write_ha_state.call_count == 2


@pytest.mark.asyncio
async def test_climatization_switch_turn_off_cancels_local_auto_off(
    monkeypatch,
):
    api = MagicMock()
    api.climatization_control = AsyncMock()
    vehicle = Vehicle("TEST_VIN", api, True)
    coordinator = MagicMock()
    coordinator.data = [vehicle]
    switch = VolvoClimatizationSwitch(
        coordinator, 0, "climatization_switch"
    )
    switch._hass = MagicMock()
    switch.async_write_ha_state = MagicMock()
    cancel = MagicMock()

    monkeypatch.setattr(
        switch_module,
        "async_call_later",
        lambda hass, delay, action: cancel,
    )

    await switch.async_turn_on()
    await switch.async_turn_off()

    assert switch.is_on is False
    cancel.assert_called_once()
    assert api.climatization_control.await_args_list[0].args == (
        "TEST_VIN",
        True,
    )
    assert api.climatization_control.await_args_list[1].args == (
        "TEST_VIN",
        False,
    )
