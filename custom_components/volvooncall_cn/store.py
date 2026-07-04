import logging
import math
from typing import TypedDict, Unpack

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .volvooncall_cn import DOMAIN

_LOGGER = logging.getLogger(__name__)
STORE_VERSION = 1


class StoreData(TypedDict, total=False):
    """Volvo Store Data"""

    engine_duration_number: int
    full_charge_electric_range: int | float
    full_charge_sampled_at: str
    full_charge_sample_count: int
    full_charge_session_active: bool
    full_charge_data_source: str


class VolvoStore(Store[StoreData]):
    def __init__(self, hass: HomeAssistant, vin: str):
        super().__init__(hass=hass, key=f"{DOMAIN}.{vin}", version=STORE_VERSION)
        self.data: StoreData | None = None
        self.default_data = StoreData(engine_duration_number=5)

    def get(self, key):
        assert self.data is not None
        return self.data.get(key)

    async def load_create_data(self) -> StoreData:
        self.data = await self.async_load() or self.default_data
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

    async def async_capture_full_charge_range(
        self,
        battery_level: int | float | None,
        electric_range: int | float | None,
        sampled_at: str,
        data_source: str | None = None,
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

        if session_active:
            return False

        if electric_range is None:
            return False
        try:
            electric_range_value = float(electric_range)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(electric_range_value) or electric_range_value <= 0:
            return False

        captured_range: int | float
        if isinstance(electric_range, int):
            captured_range = electric_range
        else:
            captured_range = electric_range_value

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
