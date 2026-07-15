"""Tests for persisted T8 full-charge range samples."""

import pytest

from custom_components.volvooncall_cn.store import VolvoStore


@pytest.mark.asyncio
async def test_captures_once_per_full_charge_session(hass):
    """A full-charge session should create one durable range sample."""
    store = VolvoStore(hass, "FULL_CHARGE_TEST")
    await store.load_create_data()

    assert (
        await store.async_capture_full_charge_range(
            99.9, 56.875, "2026-07-05T01:00:00+00:00", "grpc_battery"
        )
        is False
    )
    assert store.get("full_charge_electric_range") is None

    assert (
        await store.async_capture_full_charge_range(
            100.0,
            55.875,
            "2026-07-05T02:00:00+00:00",
            "grpc_battery",
        )
        is True
    )
    assert store.get("full_charge_electric_range") == 55.875
    assert store.get("full_charge_sample_count") == 1
    assert store.get("full_charge_session_active") is True
    assert store.get("full_charge_data_source") == "grpc_battery"

    reloaded_store = VolvoStore(hass, "FULL_CHARGE_TEST")
    await reloaded_store.load_create_data()
    assert reloaded_store.get("full_charge_electric_range") == 55.875
    assert reloaded_store.get("full_charge_sample_count") == 1
    assert reloaded_store.get("full_charge_session_active") is True

    assert (
        await store.async_capture_full_charge_range(
            100.0,
            54.625,
            "2026-07-05T02:05:00+00:00",
            "grpc_battery",
        )
        is False
    )
    assert store.get("full_charge_electric_range") == 55.875
    assert store.get("full_charge_sample_count") == 1

    assert (
        await store.async_capture_full_charge_range(
            100.0,
            56.875,
            "2026-07-05T02:07:00+00:00",
            "grpc_battery",
        )
        is True
    )
    assert store.get("full_charge_electric_range") == 56.875
    assert store.get("full_charge_sample_count") == 1
    assert store.get("full_charge_sampled_at") == "2026-07-05T02:07:00+00:00"


@pytest.mark.asyncio
async def test_below_full_resets_session_even_without_range(hass):
    """The next 100% reading should be sampled after SOC drops below full."""
    store = VolvoStore(hass, "FULL_CHARGE_RESET_TEST")
    await store.load_create_data()
    await store.async_capture_full_charge_range(
        100, 56, "2026-07-05T01:00:00+00:00", "grpc_battery"
    )

    assert (
        await store.async_capture_full_charge_range(
            80, None, "2026-07-06T01:00:00+00:00", "grpc_battery"
        )
        is False
    )
    assert store.get("full_charge_session_active") is False

    assert (
        await store.async_capture_full_charge_range(
            100, 53, "2026-07-07T01:00:00+00:00", "grpc_battery"
        )
        is True
    )
    assert store.get("full_charge_electric_range") == 53
    assert store.get("full_charge_sample_count") == 2


@pytest.mark.asyncio
async def test_waits_until_full_charge_is_no_longer_actively_charging(hass):
    """A 100% reading can still gain range while charging tapers."""
    store = VolvoStore(hass, "FULL_CHARGE_ACTIVE_CHARGING_TEST")
    await store.load_create_data()

    assert (
        await store.async_capture_full_charge_range(
            100,
            55,
            "2026-07-05T01:00:00+00:00",
            "grpc_battery",
            "charging",
            0.3,
        )
        is False
    )
    assert store.get("full_charge_electric_range") is None

    assert (
        await store.async_capture_full_charge_range(
            100,
            56,
            "2026-07-05T01:03:00+00:00",
            "grpc_battery",
            "completed",
            0,
        )
        is True
    )
    assert store.get("full_charge_electric_range") == 56
    assert store.get("full_charge_sample_count") == 1


@pytest.mark.asyncio
async def test_rejects_invalid_full_charge_samples(hass):
    """Missing, non-finite, and non-positive ranges must not be stored."""
    store = VolvoStore(hass, "FULL_CHARGE_INVALID_TEST")
    await store.load_create_data()

    for value in (None, 0, -1, float("nan"), float("inf")):
        assert (
            await store.async_capture_full_charge_range(
                100, value, "2026-07-05T01:00:00+00:00"
            )
            is False
        )

    assert store.get("full_charge_electric_range") is None
