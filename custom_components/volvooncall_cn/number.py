import logging
from propcache import cached_property
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import Platform
from . import VolvoCoordinator, VolvoEntity
from .store import CHARGE_LIMIT_DISABLED, CHARGE_LIMIT_MIN
from .volvooncall_cn import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up button."""
    coordinator: VolvoCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    numbers = []
    for idx, _ in enumerate(coordinator.data):
        numbers.append(VolovEngineDurationNumInput(coordinator, idx, "engine_duration_number"))
        if coordinator.supports_electric:
            numbers.append(
                VolvoChargeLimitNumber(coordinator, idx, "charge_limit_number")
            )

    async_add_entities(numbers)


class VolovEngineDurationNumInput(VolvoEntity, NumberEntity):
    def __init__(self, coordinator, idx, metaMapKey):
        super().__init__(coordinator, idx, metaMapKey, Platform.NUMBER)
        self.max_duration = 15
        self.min_duration = 1

    @cached_property
    def native_max_value(self) -> float:
        return self.max_duration

    @cached_property
    def native_min_value(self) -> float:
        return self.min_duration

    @cached_property
    def native_step(self) -> float:
        return 1

    @cached_property
    def native_value(self):
        store_data = self._get_store()
        return store_data.get_engine_duration_number() if store_data else None

    @property
    def state(self):
        store_data = self._get_store()
        return store_data.get_engine_duration_number() if store_data else None

    async def async_set_native_value(self, value):
        store_data = self._get_store()
        if store_data is None:
            return
        await store_data.set_engine_duration_number(value)
        await self.coordinator.async_refresh()


class VolvoChargeLimitNumber(VolvoEntity, NumberEntity):
    """Persisted SoC ceiling; 100% disables auto-stop of home charging."""

    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = CHARGE_LIMIT_MIN
    _attr_native_max_value = CHARGE_LIMIT_DISABLED
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "%"

    def __init__(self, coordinator, idx, metaMapKey):
        super().__init__(coordinator, idx, metaMapKey, Platform.NUMBER)

    @property
    def native_value(self):
        store_data = self._get_store()
        return store_data.get_charge_limit() if store_data else None

    async def async_set_native_value(self, value):
        store_data = self._get_store()
        if store_data is None:
            return
        await store_data.set_charge_limit(value)
        self.async_write_ha_state()
        # A lowered limit may already be reached; re-poll so the coordinator
        # can stop an active home-charge session right away.
        await self.coordinator.async_force_refresh()
