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

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Configure sensors from a config entry created in the integrations UI."""
    coordinator: VolvoCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    entities = []
    for idx, _ in enumerate(coordinator.data):
        entities.append(VolvoSensor(coordinator, idx, "distance_to_empty"))
        entities.append(VolvoSensor(coordinator, idx, "odo_meter"))
        entities.append(VolvoSensor(coordinator, idx, "fuel_amount"))
        entities.append(VolvoSensor(coordinator, idx, "fuel_average_consumption_liters_per_100_km"))
        entities.append(VolvoSensor(coordinator, idx, "service_warning_msg"))
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
