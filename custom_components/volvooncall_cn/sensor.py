from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.const import Platform

from . import VolvoCoordinator, VolvoEntity, metaMap
from .volvooncall_cn import DOMAIN
from .const import ELECTRIC_SENSOR_KEYS

_LOGGER = logging.getLogger(__name__)

BASE_SENSOR_KEYS = (
    "distance_to_empty",
    "odo_meter",
    "fuel_amount",
    "fuel_average_consumption_liters_per_100_km",
    "tm_distance",
    "tm_fuel_consumption",
    "tm_average_speed",
    "ta_distance",
    "ta_fuel_consumption",
    "ta_average_speed",
    "service_warning_msg",
)


def sensor_keys_for_powertrain(supports_electric):
    """Return only sensors supported by the selected powertrain."""
    if supports_electric:
        return BASE_SENSOR_KEYS + ELECTRIC_SENSOR_KEYS
    return BASE_SENSOR_KEYS


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Configure sensors from a config entry created in the integrations UI."""
    coordinator: VolvoCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    entities = []
    for idx, _ in enumerate(coordinator.data):
        for sensor_key in sensor_keys_for_powertrain(
            coordinator.supports_electric
        ):
            if sensor_key == "full_charge_electric_range":
                entities.append(
                    VolvoFullChargeRangeSensor(coordinator, idx, sensor_key)
                )
            else:
                entities.append(VolvoSensor(coordinator, idx, sensor_key))
        entities.append(VolvoConnectionStatusSensor(coordinator, idx, "connection_status"))
        # entities.append(VolvoSensor(coordinator, idx, "fuel_amount_level"))

    async_add_entities(entities)


class VolvoSensor(VolvoEntity, SensorEntity):
    """An entity using CoordinatorEntity.

    The CoordinatorEntity class provides:
      should_poll
      async_update
      async_added_to_hass
      available
    """

    def __init__(self, coordinator, idx, metaMapKey):
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(coordinator, idx, metaMapKey, Platform.SENSOR)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._attr_native_value = self.coordinator.data[self.idx].get(self.metaMapKey)
        self._attr_native_unit_of_measurement = metaMap[self.metaMapKey]["unit"]
        # Set state_class if defined in metaMap
        if "state_class" in metaMap[self.metaMapKey]:
            self._attr_state_class = metaMap[self.metaMapKey]["state_class"]
        # Set entity_category if defined in metaMap
        if "entity_category" in metaMap[self.metaMapKey]:
            self._attr_entity_category = metaMap[self.metaMapKey]["entity_category"]
        if self.metaMapKey in {
            "battery_charging_status",
            "charger_connection_status",
        }:
            vehicle = self.coordinator.data[self.idx]
            self._attr_extra_state_attributes = {
                "data_source": vehicle.charge_data_source,
                "charge_pile_name": vehicle.charge_pile_name,
                "charge_pile_address": vehicle.charge_pile_address,
                "plug_and_charge_enabled": vehicle.plug_and_charge_enabled,
                "last_charge_order": vehicle.last_charge_order,
            }
        self.async_write_ha_state()


class VolvoConnectionStatusSensor(VolvoEntity, SensorEntity):
    """Sensor for connection status with last update time as attribute."""

    def __init__(self, coordinator, idx, metaMapKey):
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(coordinator, idx, metaMapKey, Platform.SENSOR)
        # Set entity_category to diagnostic
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        vehicle = self.coordinator.data[self.idx]
        self._attr_native_value = vehicle.connection_status
        # Add last_update_time as an attribute
        self._attr_extra_state_attributes = {
            "last_update_time": vehicle.last_update_time.isoformat() if vehicle.last_update_time else None,
            "consecutive_failures": vehicle._consecutive_failures,
            "cache_info": vehicle.get_cache_info(),
        }
        self.async_write_ha_state()


class VolvoFullChargeRangeSensor(VolvoEntity, SensorEntity):
    """Publish the range captured at the start of each 100% charge session."""

    def __init__(self, coordinator, idx, metaMapKey):
        """Initialize the full-charge range sensor."""
        super().__init__(coordinator, idx, metaMapKey, Platform.SENSOR)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Publish the most recent persisted full-charge range sample."""
        store_data = self.coordinator.store_datas[self.idx]
        self._attr_native_value = store_data.get(
            "full_charge_electric_range"
        )
        self._attr_native_unit_of_measurement = metaMap[self.metaMapKey][
            "unit"
        ]
        self._attr_state_class = metaMap[self.metaMapKey]["state_class"]
        self._attr_extra_state_attributes = {
            "sampled_at": store_data.get("full_charge_sampled_at"),
            "sample_count": store_data.get("full_charge_sample_count") or 0,
            "data_source": store_data.get("full_charge_data_source"),
            "trigger_battery_level": 100,
        }
        self.async_write_ha_state()
