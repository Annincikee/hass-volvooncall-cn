"""Tests for TM and TA trip-computer telemetry."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.volvooncall_cn import metaMap
from custom_components.volvooncall_cn.proto.fuel_pb2 import FuelData, GetFuelResp
from custom_components.volvooncall_cn.proto.odometer_pb2 import (
    GetOdometerResp,
    odometerData,
)
from custom_components.volvooncall_cn.volvooncall_cn import Vehicle


TRIP_SENSOR_KEYS = {
    "tm_distance",
    "tm_fuel_consumption",
    "tm_energy_consumption",
    "tm_average_speed",
    "ta_distance",
    "ta_fuel_consumption",
    "ta_average_speed",
}


def test_trip_sensor_metadata_is_complete():
    """Each requested TM/TA value should have a distinct HA entity definition."""
    assert TRIP_SENSOR_KEYS <= metaMap.keys()
    assert metaMap["tm_distance"]["unit"] == "km"
    assert metaMap["tm_fuel_consumption"]["unit"] == "L/100km"
    assert metaMap["tm_energy_consumption"]["unit"] == "kWh/100km"
    assert metaMap["tm_average_speed"]["unit"] == "km/h"
    assert metaMap["ta_distance"]["unit"] == "km"
    assert metaMap["ta_fuel_consumption"]["unit"] == "L/100km"
    assert metaMap["ta_average_speed"]["unit"] == "km/h"


@pytest.mark.asyncio
async def test_parse_tm_and_ta_trip_data():
    """Confirmed odometer and fuel fields should populate all TM/TA values."""
    api = MagicMock()
    api.get_fuel_status = AsyncMock(
        return_value=GetFuelResp(
            vin="TEST_VIN",
            data=FuelData(
                fuelAmount=45.5,
                distanceToEmptyKm=580,
                TMFuelAvgConsum=6.8,
                ATFuleAvgConsum=7.2,
            ),
        )
    )
    api.get_odometer = AsyncMock(
        return_value=GetOdometerResp(
            vin="TEST_VIN",
            data=odometerData(
                odometerMeters=12345678,
                tripMeterManualKm=456.7,
                tripMeterAutomaticKm=12.3,
                averageSpeedKmPerHour=43,
                averageSpeedKmPerHourAutomatic=31,
            ),
        )
    )
    vehicle = Vehicle("TEST_VIN", api, True)

    await vehicle._parse_fuel()
    await vehicle._parse_odometer()

    assert vehicle.tm_distance == pytest.approx(456.7)
    assert vehicle.tm_fuel_consumption == pytest.approx(6.8)
    assert vehicle.tm_average_speed == 43
    assert vehicle.ta_distance == pytest.approx(12.3)
    assert vehicle.ta_fuel_consumption == pytest.approx(7.2)
    assert vehicle.ta_average_speed == 31
    # Keep the legacy generic entity backward-compatible; it is TM fuel data.
    assert vehicle.fuel_average_consumption_liters_per_100_km == pytest.approx(6.8)
