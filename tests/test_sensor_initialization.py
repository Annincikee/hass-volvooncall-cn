"""Sensors expose first-refresh data immediately after platform setup."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant

from custom_components.volvooncall_cn import VolvoCoordinator
from custom_components.volvooncall_cn.sensor import (
    VolvoConnectionStatusSensor,
    VolvoFullChargeRangeSensor,
    VolvoSensor,
)


def _coordinator_with_vehicle(hass: HomeAssistant):
    coordinator = VolvoCoordinator(hass, MagicMock(), 300)
    vehicle = MagicMock()
    vehicle.vin = "TESTVIN0000000001"
    vehicle.get.return_value = 12345
    vehicle.connection_status = "connected"
    vehicle.last_update_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    vehicle._consecutive_failures = 0
    vehicle.get_cache_info.return_value = {"exterior": {"status": "fresh"}}
    coordinator.data = [vehicle]
    coordinator._vehicles[vehicle.vin] = vehicle
    coordinator._vehicle_order.append(vehicle.vin)
    return coordinator, vehicle


def test_standard_sensor_uses_first_refresh_value(hass: HomeAssistant):
    coordinator, _ = _coordinator_with_vehicle(hass)

    sensor = VolvoSensor(coordinator, 0, "odo_meter")

    assert sensor.native_value == 12345
    assert sensor.native_unit_of_measurement == "km"


def test_connection_sensor_uses_first_refresh_value(hass: HomeAssistant):
    coordinator, _ = _coordinator_with_vehicle(hass)

    sensor = VolvoConnectionStatusSensor(
        coordinator, 0, "connection_status"
    )

    assert sensor.native_value == "connected"
    assert sensor.extra_state_attributes["consecutive_failures"] == 0


def test_full_charge_sensor_uses_persisted_value(hass: HomeAssistant):
    coordinator, vehicle = _coordinator_with_vehicle(hass)
    store_data = {
        "full_charge_electric_range": 87,
        "full_charge_sampled_at": "2026-01-01T00:00:00+00:00",
        "full_charge_sample_count": 3,
        "full_charge_data_source": "grpc_battery",
    }
    store = MagicMock()
    store.get.side_effect = store_data.get
    coordinator._stores[vehicle.vin] = store

    sensor = VolvoFullChargeRangeSensor(
        coordinator, 0, "full_charge_electric_range"
    )

    assert sensor.native_value == 87
    assert sensor.extra_state_attributes["sample_count"] == 3
