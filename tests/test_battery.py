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
                averageEnergyConsumptionKwhPer100Km=18.4,
                estimatedDistanceToEmptyKm=54,
                estimatedChargingTimeToFullMinutes=42,
                chargerConnectionStatus=2,
                chargingStatus=1,
                chargingPowerWatts=3680,
            ),
        )
    )
    api.get_charge_pile_status = AsyncMock(return_value=None)
    vehicle = Vehicle("TEST_VIN", api, True)

    await vehicle._parse_battery()

    assert vehicle.battery_charge_level_percentage == 76.5
    assert vehicle.electric_range == 54
    assert vehicle.tm_energy_consumption == 18.4
    assert vehicle.battery_charging_status == "charging"
    assert vehicle.charger_connection_status == "connected_ac"
    assert vehicle.estimated_charging_time == 42
    assert vehicle.charging_power == 3.68
    assert vehicle.charge_data_source == "grpc_battery"
    api.get_charge_pile_status.assert_awaited_once_with("TEST_VIN")


@pytest.mark.asyncio
async def test_charge_pile_never_supplies_vehicle_battery_or_range():
    """Pile charging data must not become vehicle battery telemetry."""
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

    assert vehicle.battery_charge_level_percentage is None
    assert vehicle.electric_range is None
    assert vehicle.tm_energy_consumption is None
    assert vehicle.battery_charging_status == "charging"
    assert vehicle.charger_connection_status == "charging"
    assert vehicle.estimated_charging_time == 55
    assert vehicle.charging_power == 7.2
    assert vehicle.charge_data_source == "charge_pile_api"
    assert vehicle.charge_pile_name == "Home charger"
    assert vehicle.charge_pile_address == "Garage"


@pytest.mark.asyncio
async def test_charge_pile_cannot_override_battery_service_values():
    """Vehicle battery and range always remain sourced from BatteryService."""
    api = MagicMock()
    api.get_battery_status = AsyncMock(
        return_value=GetBatteryResponse(
            vin="TEST_VIN",
            battery=Battery(
                batteryChargeLevelPercentage=76.5,
                averageEnergyConsumptionKwhPer100Km=18.4,
                estimatedDistanceToEmptyKm=54,
            ),
        )
    )
    api.get_charge_pile_status = AsyncMock(
        return_value={
            "pile": {"equipmentName": "Home charger"},
            "status": {
                "batteryChargeLevelPercentage": "1.0",
                "estimatedDrivingKm": "2",
                "connectorStatus": 1,
                "startChargeSeqStat": 0,
            },
        }
    )
    vehicle = Vehicle("TEST_VIN", api, True)

    await vehicle._parse_battery()

    assert vehicle.battery_charge_level_percentage == 76.5
    assert vehicle.electric_range == 54
    assert vehicle.tm_energy_consumption == 18.4
    assert vehicle.charge_data_source == "grpc_battery+charge_pile_api"
