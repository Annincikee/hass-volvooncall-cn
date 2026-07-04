"""Tests for vehicle powertrain filtering."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.volvooncall_cn import VolvoCoordinator
from custom_components.volvooncall_cn import remove_electric_entity_registry_entries
from custom_components.volvooncall_cn.const import (
    ELECTRIC_SENSOR_KEYS,
    POWERTRAIN_FUEL,
    POWERTRAIN_T8,
)
from custom_components.volvooncall_cn.sensor import sensor_keys_for_powertrain
from custom_components.volvooncall_cn.volvooncall_cn import Vehicle
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry


def test_fuel_powertrain_hides_all_electric_sensors():
    """B4/B5/B6 should not create any electric-related HA sensors."""
    fuel_keys = set(sensor_keys_for_powertrain(False))

    assert fuel_keys.isdisjoint(ELECTRIC_SENSOR_KEYS)


def test_t8_powertrain_exposes_all_electric_sensors():
    """T8 should keep every electric-related HA sensor."""
    t8_keys = set(sensor_keys_for_powertrain(True))

    assert set(ELECTRIC_SENSOR_KEYS) <= t8_keys


def test_coordinator_maps_powertrain_to_electric_support(hass):
    """The selected model group should control coordinator capabilities."""
    fuel = VolvoCoordinator(hass, MagicMock(), 30, POWERTRAIN_FUEL)
    t8 = VolvoCoordinator(hass, MagicMock(), 30, POWERTRAIN_T8)

    assert fuel.supports_electric is False
    assert t8.supports_electric is True


def test_switching_to_fuel_removes_existing_electric_registry_entries(hass):
    """Previously-created T8 entities should not remain visible after a switch."""
    config_entry = MockConfigEntry(domain="volvooncall_cn")
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    electric = registry.async_get_or_create(
        "sensor",
        "volvooncall_cn",
        "TEST_VIN-electric_range",
        config_entry=config_entry,
    )
    full_charge = registry.async_get_or_create(
        "sensor",
        "volvooncall_cn",
        "TEST_VIN-full_charge_electric_range",
        config_entry=config_entry,
    )
    fuel = registry.async_get_or_create(
        "sensor",
        "volvooncall_cn",
        "TEST_VIN-tm_distance",
        config_entry=config_entry,
    )

    remove_electric_entity_registry_entries(hass, config_entry.entry_id)

    assert registry.async_get(electric.entity_id) is None
    assert registry.async_get(full_charge.entity_id) is None
    assert registry.async_get(fuel.entity_id) is not None


@pytest.mark.asyncio
async def test_fuel_vehicle_does_not_poll_battery_service():
    """B4/B5/B6 updates should not call BatteryService or charge-pile APIs."""
    api = MagicMock()
    api.get_vehicles = AsyncMock(
        return_value=[
            {
                "vinCode": "TEST_VIN",
                "seriesName": "XC60",
                "modelName": "B5",
            }
        ]
    )
    api.get_channel = AsyncMock()
    api.get_battery_status = AsyncMock()
    api.get_charge_pile_status = AsyncMock()
    vehicle = Vehicle("TEST_VIN", api, True, supports_electric=False)

    for method_name in (
        "_parse_exterior",
        "_parse_odometer",
        "_parse_fuel",
        "_parse_availability",
        "_parse_location",
        "_parse_engine_status",
        "_parse_health",
        "_parse_car_preference",
    ):
        setattr(vehicle, method_name, AsyncMock())

    await vehicle.update()

    api.get_battery_status.assert_not_called()
    api.get_charge_pile_status.assert_not_called()
