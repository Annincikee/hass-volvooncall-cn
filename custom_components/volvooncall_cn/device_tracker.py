from __future__ import annotations

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.const import Platform

from . import VolvoCoordinator, VolvoEntity
from .volvooncall_cn import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Configure sensors from a config entry created in the integrations UI."""
    coordinator: VolvoCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    entities = []
    for idx, _ in enumerate(coordinator.data):
        entities.append(VolvoSensor(coordinator, idx, "position"))
        entities.append(VolvoSensor(coordinator, idx, "position_wgs84"))

    async_add_entities(entities)


class VolvoSensor(VolvoEntity, TrackerEntity):
    def __init__(self, coordinator, idx, metaMapKey):
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(coordinator, idx, metaMapKey, Platform.DEVICE_TRACKER)

    _attr_source_type = SourceType.GPS

    def _coordinate(self, axis):
        """Return one coordinate, or None when the vehicle has no fix."""
        vehicle = self.vehicle
        if vehicle is None:
            return None
        position = vehicle.get(self.metaMapKey) or {}
        return position.get(axis)

    @property
    def latitude(self):
        """Handle updated data from the coordinator."""
        return self._coordinate("latitude")

    @property
    def longitude(self):
        """Handle updated data from the coordinator."""
        return self._coordinate("longitude")
