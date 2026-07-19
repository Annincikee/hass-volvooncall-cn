import logging
import asyncio
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import Platform
from homeassistant.exceptions import HomeAssistantError

from . import VolvoCoordinator, VolvoEntity
from .volvooncall_cn import DOMAIN
from .volvooncall_base import MAX_RETRIES

_LOGGER = logging.getLogger(__name__)

CLIMATIZATION_DEFAULT_RUNTIME = 3 * 60
CLIMATIZATION_CHARGING_RUNTIME = 30 * 60
CLIMATIZATION_CONNECTED_STATES = {
    "connected_ac",
    "connected_dc",
    "plugged_in",
    "charging",
}
CLIMATIZATION_CHARGING_STATES = {
    "charging",
    "scheduled",
    "smart_charging",
}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up button."""
    coordinator: VolvoCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    switchs = []
    for idx, ent in enumerate(coordinator.data):
        switchs.append(VolvoEngineSwitch(coordinator, idx, "engine_switch"))
        switchs.append(
            VolvoClimatizationSwitch(coordinator, idx, "climatization_switch")
        )
        if ent.get("isAaos"):
            switchs.append(VolvoTailgateSwitch(coordinator, idx, "tail_gate_switch"))
            switchs.append(VolvoSunroofSwitch(coordinator, idx, "sunroof_switch"))
        if coordinator.supports_electric:
            switchs.append(
                VolvoHomeChargeSwitch(coordinator, idx, "home_charge_switch")
            )
            switchs.append(
                VolvoPlugAndChargeSwitch(coordinator, idx, "plug_and_charge_switch")
            )

    async_add_entities(switchs)


class VolvoSwitchEntity(VolvoEntity, SwitchEntity):
    def __init__(self, coordinator, idx, metaKey, checkMetaKeys):
        super().__init__(coordinator, idx, metaKey, Platform.SWITCH)
        self.checkMetaKeys = checkMetaKeys

    async def _update_status(self, is_on):
        for _ in range(MAX_RETRIES):
            await asyncio.sleep(2)
            await self.coordinator.async_refresh()
            if self.is_on == is_on:
                break

    @property
    def is_on(self):
        vehicle = self.vehicle
        if vehicle is None:
            return None
        for key in self.checkMetaKeys:
            if vehicle.get(key):
                return True
        return False


class VolvoEngineSwitch(VolvoSwitchEntity):
    def __init__(self, coordinator, idx, metaKey):
        check_meta_keys = ["engine_running", "engine_remote_running"]
        super().__init__(coordinator, idx, metaKey, check_meta_keys)

    async def async_turn_on(self) -> None:
        store_data = self._get_store()
        duration = store_data.get_engine_duration_number() if store_data else 5
        await self.vehicle.engine_start(duration)
        await self._update_status(True)

    async def async_turn_off(self) -> None:
        await self.vehicle.engine_stop()
        await self._update_status(False)

    @property
    def extra_state_attributes(self):
        data = self.vehicle
        if data is None:
            return {}
        return {
            "remote_start_at": data.get("engine_remote_start_time"),
            "remote_end_at": data.get("engine_remote_end_time")
        }


class VolvoClimatizationSwitch(VolvoEntity, SwitchEntity):
    """Optimistic parked-climatization control with local auto-off."""

    _attr_assumed_state = True

    def __init__(self, coordinator, idx, metaKey):
        super().__init__(coordinator, idx, metaKey, Platform.SWITCH)
        self._attr_is_on = False
        self._cancel_auto_off = None

    def _cancel_auto_off_timer(self):
        if self._cancel_auto_off is None:
            return
        self._cancel_auto_off()
        self._cancel_auto_off = None

    def _is_charger_connected(self):
        vehicle = self.vehicle
        if vehicle is None:
            return False
        connection_status = str(
            vehicle.get("charger_connection_status") or ""
        ).lower()
        charging_status = str(
            vehicle.get("battery_charging_status") or ""
        ).lower()
        charging_power = vehicle.get("charging_power")

        if connection_status in CLIMATIZATION_CONNECTED_STATES:
            return True
        if charging_status in CLIMATIZATION_CHARGING_STATES:
            return True
        if isinstance(charging_power, (int, float)) and charging_power > 0:
            return True
        return False

    def _auto_off_delay(self):
        if self._is_charger_connected():
            return CLIMATIZATION_CHARGING_RUNTIME
        return CLIMATIZATION_DEFAULT_RUNTIME

    def _schedule_auto_off(self):
        self._cancel_auto_off_timer()

        def _turn_off_state(_now):
            self._cancel_auto_off = None
            self._attr_is_on = False
            self.async_write_ha_state()

        self._cancel_auto_off = async_call_later(
            self.hass,
            self._auto_off_delay(),
            _turn_off_state,
        )

    async def async_turn_on(self) -> None:
        await self.vehicle.climatization_start()
        self._attr_is_on = True
        self._schedule_auto_off()
        self.async_write_ha_state()

    async def async_turn_off(self) -> None:
        await self.vehicle.climatization_stop()
        self._cancel_auto_off_timer()
        self._attr_is_on = False
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        self._cancel_auto_off_timer()


class VolvoTailgateSwitch(VolvoSwitchEntity):
    def __init__(self, coordinator, idx, metaKey):
        check_keys = ["tail_gate_open"]
        super().__init__(coordinator, idx, metaKey, check_keys)

    async def async_turn_on(self) -> None:
        vehicle = self.vehicle
        await vehicle.unlock_vehicle_trunk_only()
        await vehicle.tail_gate_control_open()
        await self._update_status(True)

    async def async_turn_off(self) -> None:
        await self.vehicle.tail_gate_control_close()
        await self._update_status(False)


class VolvoSunroofSwitch(VolvoSwitchEntity):
    def __init__(self, coordinator, idx, metaKey):
        check_keys = ["sunroof_open"]
        super().__init__(coordinator, idx, metaKey, check_keys)

    async def async_turn_on(self) -> None:
        await self.vehicle.sunroof_control_open()
        await self._update_status(True)

    async def async_turn_off(self) -> None:
        await self.vehicle.sunroof_control_close()
        await self._update_status(False)


class VolvoHomeChargeSwitch(VolvoEntity, SwitchEntity):
    """Start/stop charging on the linked home charge pile."""

    def __init__(self, coordinator, idx, metaKey):
        super().__init__(coordinator, idx, metaKey, Platform.SWITCH)

    @property
    def is_on(self):
        vehicle = self.vehicle
        if vehicle is None:
            return None
        status = str(vehicle.get("home_charge_status") or "").lower()
        return status in ("starting", "charging")

    @property
    def available(self):
        vehicle = self.vehicle
        if vehicle is None:
            return False
        return super().available and bool(
            vehicle.get("has_home_charge_pile")
        )

    @property
    def extra_state_attributes(self):
        vehicle = self.vehicle
        if vehicle is None:
            return {}
        return {
            "home_charge_status": vehicle.get("home_charge_status"),
            "session_active": bool(vehicle.get("charge_trade_no")),
        }

    async def async_turn_on(self) -> None:
        await self.vehicle.start_home_charge()
        await self._update_status(True)

    async def async_turn_off(self) -> None:
        await self.vehicle.stop_home_charge()
        await self._update_status(False)

    async def _update_status(self, is_on):
        for _ in range(MAX_RETRIES):
            await asyncio.sleep(2)
            await self.coordinator.async_force_refresh()
            if self.is_on == is_on:
                return
        raise HomeAssistantError(
            "Home charge command was sent but the requested state "
            "could not be confirmed"
        )


class VolvoPlugAndChargeSwitch(VolvoEntity, SwitchEntity):
    """Toggle auto-start charging when the connector is plugged in."""

    def __init__(self, coordinator, idx, metaKey):
        super().__init__(coordinator, idx, metaKey, Platform.SWITCH)

    @property
    def is_on(self):
        vehicle = self.vehicle
        if vehicle is None:
            return None
        return bool(vehicle.get("plug_and_charge_enabled"))

    @property
    def available(self):
        vehicle = self.vehicle
        if vehicle is None:
            return False
        return super().available and bool(
            vehicle.get("has_home_charge_pile")
        )

    async def async_turn_on(self) -> None:
        await self.vehicle.set_plug_and_charge(True)
        await self._update_status(True)

    async def async_turn_off(self) -> None:
        await self.vehicle.set_plug_and_charge(False)
        await self._update_status(False)

    async def _update_status(self, is_on):
        for _ in range(MAX_RETRIES):
            await asyncio.sleep(2)
            await self.coordinator.async_force_refresh()
            if self.is_on == is_on:
                return
        raise HomeAssistantError(
            "Plug-and-charge command was sent but the requested state "
            "could not be confirmed"
        )
