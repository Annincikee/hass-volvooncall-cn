
import hashlib
import logging
import math
from typing import TypedDict, Unpack
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from .volvooncall_cn import DOMAIN

_LOGGER = logging.getLogger(__name__)
STORE_VERSION = 1
ACTIVE_FULL_CHARGE_STATUSES = {"charging", "smart_charging"}

# 100 disables the limit so upgrades keep today's charge-to-full behavior.
CHARGE_LIMIT_DISABLED = 100
CHARGE_LIMIT_MIN = 50


class StoreData(TypedDict, total=False):
    """Volvo Store Data"""
    engine_duration_number: int
    charge_limit: int
    full_charge_electric_range: int | float
    full_charge_sampled_at: str
    full_charge_sample_count: int
    full_charge_session_active: bool
    full_charge_data_source: str


def store_key_for(vin: str) -> str:
    """Return the .storage key for a VIN.

    Hashed, because the key becomes the filename on disk: the previous
    plaintext form wrote .storage/volvooncall_cn.LYV<...> and leaked the VIN
    into every backup, snapshot and support screenshot. The auth store
    already hashed its key the same way.
    """
    digest = hashlib.sha256(str(vin).encode("utf-8")).hexdigest()[:16]
    return f"{DOMAIN}.{digest}"


class VolvoStore(Store[StoreData]):
    def __init__(self, hass: HomeAssistant, vin: str):
        super().__init__(
            hass=hass, key=store_key_for(vin), version=STORE_VERSION
        )
        self.data: StoreData | None = None
        self.default_data = StoreData(engine_duration_number=5)
        self._legacy_store: Store[StoreData] = Store(
            hass=hass, key=f"{DOMAIN}.{vin}", version=STORE_VERSION
        )

    def get(self, key):
        assert self.data is not None
        return self.data.get(key)

    async def _async_migrate_legacy(self) -> StoreData | None:
        """Move data from the pre-hash, VIN-named store, then delete it."""
        try:
            legacy = await self._legacy_store.async_load()
        except Exception as err:  # pragma: no cover - defensive
            _LOGGER.warning("Could not read the legacy store: %s", err)
            return None
        if not legacy:
            return None

        _LOGGER.info(
            "Migrating vehicle settings to a hashed storage key so the VIN "
            "no longer appears in the .storage filename"
        )
        await self.async_save(legacy)
        try:
            await self._legacy_store.async_remove()
        except Exception as err:  # pragma: no cover - defensive
            _LOGGER.warning("Could not remove the legacy store: %s", err)
        return legacy

    async def load_create_data(self) -> StoreData:
        data = await self.async_load()
        if not data:
            data = await self._async_migrate_legacy()
        self.data = data or self.default_data
        return self.data

    async def update(self, **kwargs: Unpack[StoreData]):
        self.data = self.data or await self.load_create_data()
        for key, value in kwargs.items():
            if value is not None and key in StoreData.__annotations__:
                self.data[key] = value
        await self.async_save(self.data)

    def get_engine_duration_number(self):
        self.data = self.data or self.default_data
        return self.data.get("engine_duration_number")

    async def set_engine_duration_number(self, value):
        await self.update(engine_duration_number=int(value))

    def get_charge_limit(self) -> int:
        self.data = self.data or self.default_data
        value = self.data.get("charge_limit")
        try:
            limit = int(value)
        except (TypeError, ValueError):
            return CHARGE_LIMIT_DISABLED
        return min(max(limit, CHARGE_LIMIT_MIN), CHARGE_LIMIT_DISABLED)

    async def set_charge_limit(self, value):
        limit = min(
            max(int(value), CHARGE_LIMIT_MIN), CHARGE_LIMIT_DISABLED
        )
        await self.update(charge_limit=limit)

    async def async_capture_full_charge_range(
        self,
        battery_level: int | float | None,
        electric_range: int | float | None,
        sampled_at: str,
        data_source: str | None = None,
        charging_status: str | None = None,
        charging_power: int | float | str | None = None,
    ) -> bool:
        """Capture one range sample when a new 100% charge session starts."""
        self.data = self.data or await self.load_create_data()
        if battery_level is None:
            return False

        try:
            battery_level_value = float(battery_level)
        except (TypeError, ValueError):
            return False

        if not math.isfinite(battery_level_value):
            return False

        session_active = bool(
            self.data.get("full_charge_session_active", False)
        )
        if battery_level_value < 100:
            if session_active:
                await self.update(full_charge_session_active=False)
            return False

        if self._is_active_charging(charging_status, charging_power):
            return False

        if electric_range is None:
            return False

        try:
            electric_range_value = float(electric_range)
        except (TypeError, ValueError):
            return False

        if (
            not math.isfinite(electric_range_value)
            or electric_range_value <= 0
        ):
            return False

        captured_range: int | float
        if isinstance(electric_range, int):
            captured_range = electric_range
        else:
            captured_range = electric_range_value

        if session_active:
            previous_range = self.data.get("full_charge_electric_range")
            try:
                previous_range_value = float(previous_range)
            except (TypeError, ValueError):
                previous_range_value = 0

            if captured_range <= previous_range_value:
                return False

            sample_update = StoreData(
                full_charge_electric_range=captured_range,
                full_charge_sampled_at=sampled_at,
            )
            if data_source is not None:
                sample_update["full_charge_data_source"] = data_source
            await self.update(**sample_update)
            return True

        sample = StoreData(
            full_charge_electric_range=captured_range,
            full_charge_sampled_at=sampled_at,
            full_charge_sample_count=(
                self.data.get("full_charge_sample_count", 0) + 1
            ),
            full_charge_session_active=True,
        )
        if data_source is not None:
            sample["full_charge_data_source"] = data_source
        await self.update(**sample)
        return True

    def _is_active_charging(
        self,
        charging_status: str | None,
        charging_power: int | float | str | None,
    ) -> bool:
        if (
            charging_status
            and str(charging_status).lower() in ACTIVE_FULL_CHARGE_STATUSES
        ):
            return True

        if charging_power is None:
            return False
        try:
            charging_power_value = float(charging_power)
        except (TypeError, ValueError):
            return False
        return math.isfinite(charging_power_value) and charging_power_value > 0
