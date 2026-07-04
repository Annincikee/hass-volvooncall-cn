"""Tests for battery and home charging-pile telemetry."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.volvooncall_cn.proto.battery_pb2 import (
    Battery,
    GetBatteryResponse,
)
from custom_components.volvooncall_cn.volvooncall_cn import Vehicle


@pytest.mark.asyncio
async def test_parse_battery_from_grpc():
    """BatteryService data should populate all electric sensors."""
    api = MagicMock()
    api.get_battery_status = AsyncMock(
        return_value=GetBatteryResponse(
            vin="TEST_VIN",
            battery=Battery(
                batteryChargeLevelPercentage=76.5,
                estimatedDistanceToEmptyKm=54,
                estimatedChargingTimeToFullMinutes=42,
                chargerConnectionStatus=2,
                chargingStatus=1,
                chargingPowerWatts=3680,
            ),
        )
    )
    vehicle = Vehicle("TEST_VIN", api, True)

    await vehicle._parse_battery()

    assert vehicle.battery_charge_level_percentage == 76.5
    assert vehicle.electric_range == 54
    assert vehicle.battery_charging_status == "charging"
    assert vehicle.charger_connection_status == "connected_ac"
    assert vehicle.estimated_charging_time == 42
    assert vehicle.charging_power == 3.68
    assert vehicle.charge_data_source == "grpc_battery"
    api.get_charge_pile_status.assert_not_called()


@pytest.mark.asyncio
async def test_parse_battery_falls_back_to_charge_pile():
    """The linked home-pile API should supply telemetry when gRPC is absent."""
    api = MagicMock()
    api.get_battery_status = AsyncMock(side_effect=RuntimeError("UNIMPLEMENTED"))
    api.get_charge_pile_status = AsyncMock(
        return_value={
            "pile": {
                "equipmentName": "Home charger",
                "address": "Garage",
            },
            "status": {
                "batteryChargeLevelPercentage": "63.0",
                "estimatedDrivingKm": "48",
                "connectorStatus": 3,
                "startChargeSeqStat": 1,
                "estimatedChargingTime": "55",
                "power": "7.2",
            },
        }
    )
    vehicle = Vehicle("TEST_VIN", api, True)

    await vehicle._parse_battery()

    assert vehicle.battery_charge_level_percentage == 63.0
    assert vehicle.electric_range == 48
    assert vehicle.battery_charging_status == "charging"
    assert vehicle.charger_connection_status == "charging"
    assert vehicle.estimated_charging_time == 55
    assert vehicle.charging_power == 7.2
    assert vehicle.charge_data_source == "charge_pile_api"
    assert vehicle.charge_pile_name == "Home charger"
    assert vehicle.charge_pile_address == "Garage"
