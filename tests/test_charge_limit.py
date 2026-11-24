"""Tests for the persisted charge limit and its home-charge auto-stop."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.volvooncall_cn import VolvoCoordinator
from custom_components.volvooncall_cn.number import VolvoChargeLimitNumber
from custom_components.volvooncall_cn.store import (
    CHARGE_LIMIT_DISABLED,
    CHARGE_LIMIT_MIN,
    VolvoStore,
)

TEST_SCAN_INTERVAL = 300


def _mock_vehicle(battery_level, home_charge_status):
    vehicle = MagicMock()
    vehicle.vin = "CHARGE_LIMIT_TEST"
    vehicle.battery_charge_level_percentage = battery_level
    vehicle.home_charge_status = home_charge_status
    vehicle.stop_home_charge = AsyncMock()
    return vehicle


async def _store_with_limit(hass, limit=None):
    store = VolvoStore(hass, "CHARGE_LIMIT_TEST")
    await store.load_create_data()
    if limit is not None:
        await store.set_charge_limit(limit)
    return store


@pytest.mark.asyncio
async def test_charge_limit_defaults_to_disabled(hass):
    """Fresh installs must keep the charge-to-full behavior."""
    store = await _store_with_limit(hass)
    assert store.get_charge_limit() == CHARGE_LIMIT_DISABLED


@pytest.mark.asyncio
async def test_charge_limit_is_persisted_and_clamped(hass):
    store = await _store_with_limit(hass, 80)
    assert store.get_charge_limit() == 80

    reloaded = VolvoStore(hass, "CHARGE_LIMIT_TEST")
    await reloaded.load_create_data()
    assert reloaded.get_charge_limit() == 80

    await store.set_charge_limit(30)
    assert store.get_charge_limit() == CHARGE_LIMIT_MIN
    await store.set_charge_limit(120)
    assert store.get_charge_limit() == CHARGE_LIMIT_DISABLED


@pytest.mark.asyncio
async def test_enforce_stops_home_charge_at_limit(hass):
    coordinator = VolvoCoordinator(hass, MagicMock(), TEST_SCAN_INTERVAL)
    store = await _store_with_limit(hass, 80)
    vehicle = _mock_vehicle(80.0, "charging")

    await coordinator._async_enforce_charge_limit(vehicle, store)

    vehicle.stop_home_charge.assert_awaited_once()


@pytest.mark.asyncio
async def test_enforce_skips_below_limit(hass):
    coordinator = VolvoCoordinator(hass, MagicMock(), TEST_SCAN_INTERVAL)
    store = await _store_with_limit(hass, 80)
    vehicle = _mock_vehicle(79.4, "charging")

    await coordinator._async_enforce_charge_limit(vehicle, store)

    vehicle.stop_home_charge.assert_not_awaited()


@pytest.mark.asyncio
async def test_enforce_skips_when_limit_disabled(hass):
    coordinator = VolvoCoordinator(hass, MagicMock(), TEST_SCAN_INTERVAL)
    store = await _store_with_limit(hass, CHARGE_LIMIT_DISABLED)
    vehicle = _mock_vehicle(100.0, "charging")

    await coordinator._async_enforce_charge_limit(vehicle, store)

    vehicle.stop_home_charge.assert_not_awaited()


@pytest.mark.asyncio
async def test_enforce_skips_when_not_home_charging(hass):
    """Public/AC sessions cannot be stopped remotely; only home piles can."""
    coordinator = VolvoCoordinator(hass, MagicMock(), TEST_SCAN_INTERVAL)
    store = await _store_with_limit(hass, 80)

    for status in (None, "idle", "stopping", "done", "unknown"):
        vehicle = _mock_vehicle(95.0, status)
        await coordinator._async_enforce_charge_limit(vehicle, store)
        vehicle.stop_home_charge.assert_not_awaited()


@pytest.mark.asyncio
async def test_enforce_skips_without_battery_reading(hass):
    coordinator = VolvoCoordinator(hass, MagicMock(), TEST_SCAN_INTERVAL)
    store = await _store_with_limit(hass, 80)
    vehicle = _mock_vehicle(None, "charging")

    await coordinator._async_enforce_charge_limit(vehicle, store)

    vehicle.stop_home_charge.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "battery_level",
    [float("nan"), float("inf"), float("-inf"), -1, 101],
)
async def test_enforce_rejects_non_finite_or_out_of_range_soc(
    hass, battery_level
):
    """Malformed telemetry must never stop a real charging session."""
    coordinator = VolvoCoordinator(hass, MagicMock(), TEST_SCAN_INTERVAL)
    store = await _store_with_limit(hass, 80)
    vehicle = _mock_vehicle(battery_level, "charging")

    await coordinator._async_enforce_charge_limit(vehicle, store)

    vehicle.stop_home_charge.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("previous_limit", "new_limit"),
    [(80, 90), (80, 100), (80, 80)],
)
async def test_charge_limit_non_decrease_does_not_force_poll(
    previous_limit, new_limit
):
    """Raising, disabling, or re-saving the limit is a local-only change."""
    store = MagicMock()
    store.get_charge_limit.side_effect = [previous_limit, new_limit]
    store.set_charge_limit = AsyncMock()
    entity = MagicMock()
    entity._get_store.return_value = store

    with patch(
        "custom_components.volvooncall_cn.number.async_refresh_after_control",
        new=AsyncMock(),
    ) as refresh:
        await VolvoChargeLimitNumber.async_set_native_value(entity, new_limit)

    refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_lowered_charge_limit_forces_one_poll():
    store = MagicMock()
    store.get_charge_limit.side_effect = [90, 80]
    store.set_charge_limit = AsyncMock()
    entity = MagicMock()
    entity._get_store.return_value = store

    with patch(
        "custom_components.volvooncall_cn.number.async_refresh_after_control",
        new=AsyncMock(),
    ) as refresh:
        await VolvoChargeLimitNumber.async_set_native_value(entity, 80)

    refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_enforce_swallows_stop_failures(hass):
    """A failed stop command must not break the coordinator update."""
    coordinator = VolvoCoordinator(hass, MagicMock(), TEST_SCAN_INTERVAL)
    store = await _store_with_limit(hass, 80)
    vehicle = _mock_vehicle(90.0, "charging")
    vehicle.stop_home_charge.side_effect = RuntimeError("boom")

    await coordinator._async_enforce_charge_limit(vehicle, store)

    vehicle.stop_home_charge.assert_awaited_once()
