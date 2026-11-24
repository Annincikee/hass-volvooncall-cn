"""Focused regression tests for per-source vehicle stability."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.volvooncall_cn.proto.exterior_pb2 import GetExteriorResp
from custom_components.volvooncall_cn.volvooncall_base import VolvoAPIError
from custom_components.volvooncall_cn.volvooncall_cn import (
    SOURCE_FAILURE_BACKOFF_BASE_SECONDS,
    Vehicle,
    _first_stream_response,
)


POLL_SOURCES = (
    "_parse_exterior",
    "_parse_odometer",
    "_parse_fuel",
    "_parse_availability",
    "_parse_location",
    "_parse_health",
    "_parse_car_preference",
)


def _mock_poll_parsers(vehicle):
    for method_name in POLL_SOURCES:
        setattr(vehicle, method_name, AsyncMock())


@pytest.mark.asyncio
async def test_update_raises_when_no_attempted_source_advances_generation():
    """A fully swallowed source outage must reach the coordinator as failure."""
    api = MagicMock()
    api.get_channel = AsyncMock()
    vehicle = Vehicle("TEST_VIN", api, isAaos=False, supports_electric=False)
    _mock_poll_parsers(vehicle)

    with patch(
        "custom_components.volvooncall_cn.volvooncall_cn.time.monotonic",
        return_value=100.0,
    ):
        with pytest.raises(
            VolvoAPIError,
            match="No attempted vehicle data source refreshed successfully",
        ):
            await vehicle.update()

    for source in vehicle._data_source_status:
        if source == "engine_status":
            continue
        assert vehicle._data_source_status[source] is False
        assert vehicle._source_consecutive_failures[source] == 1
        assert vehicle._source_retry_after[source] == (100.0 + SOURCE_FAILURE_BACKOFF_BASE_SECONDS)


@pytest.mark.asyncio
async def test_partial_source_success_keeps_update_available():
    """One real source generation is enough to serve a degraded snapshot."""
    api = MagicMock()
    api.get_channel = AsyncMock()
    vehicle = Vehicle("TEST_VIN", api, isAaos=False, supports_electric=False)
    _mock_poll_parsers(vehicle)

    async def save_exterior():
        vehicle._save_to_cache("exterior", {"car_locked": True})

    vehicle._parse_exterior = AsyncMock(side_effect=save_exterior)

    await vehicle.update()

    assert vehicle._data_source_status["exterior"] is True
    assert vehicle._data_source_generation["exterior"] == 1
    assert vehicle._cache["exterior"] == {"car_locked": True}
    assert vehicle._data_source_status["fuel"] is False
    assert vehicle._source_consecutive_failures["fuel"] == 1


@pytest.mark.asyncio
async def test_expired_source_cache_is_removed_and_owned_values_are_cleared():
    """A source outage must not expose the same cached value forever."""
    api = MagicMock()
    api.get_exterior = AsyncMock(side_effect=RuntimeError("UNAVAILABLE"))
    vehicle = Vehicle("TEST_VIN", api, isAaos=False, supports_electric=False)
    vehicle.car_locked = True
    vehicle._cache["exterior"] = {"car_locked": True}
    vehicle._cache_timestamp["exterior"] = datetime.now(timezone.utc) - timedelta(hours=2)

    await vehicle._parse_exterior()

    assert vehicle.car_locked is None
    assert "exterior" not in vehicle._cache
    assert "exterior" not in vehicle._cache_timestamp
    assert vehicle._data_source_status["exterior"] is False


@pytest.mark.asyncio
async def test_missing_protobuf_payload_preserves_recent_cache():
    """An absent message field is failure, not a healthy all-zero reading."""
    api = MagicMock()
    api.get_exterior = AsyncMock(return_value=GetExteriorResp())
    vehicle = Vehicle("TEST_VIN", api, isAaos=False, supports_electric=False)
    vehicle.car_locked = True
    vehicle._save_to_cache("exterior", {"car_locked": True})
    generation = vehicle._data_source_generation["exterior"]

    await vehicle._parse_exterior()

    assert vehicle.car_locked is True
    assert vehicle._data_source_status["exterior"] is False
    assert vehicle._data_source_generation["exterior"] == generation


@pytest.mark.asyncio
async def test_empty_grpc_stream_is_failure_and_is_cancelled():
    """No response cannot be accepted as a successful zero-valued message."""

    class EmptyCall:
        cancelled = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        def cancel(self):
            self.cancelled = True

    call = EmptyCall()

    with pytest.raises(VolvoAPIError, match="without a response"):
        await _first_stream_response(call)

    assert call.cancelled is True


def test_source_backoff_is_monotonic_exponential_and_success_resets_it():
    """Repeated failures back off per source without wall-clock dependence."""
    vehicle = Vehicle("TEST_VIN", MagicMock(), isAaos=False, supports_electric=False)

    with patch(
        "custom_components.volvooncall_cn.volvooncall_cn.time.monotonic",
        return_value=100.0,
    ):
        vehicle._record_source_failure("exterior")
        assert vehicle._source_retry_after["exterior"] == (
            100.0 + SOURCE_FAILURE_BACKOFF_BASE_SECONDS
        )
        assert vehicle._should_refresh("exterior") is False

        vehicle._record_source_failure("exterior")
        assert vehicle._source_retry_after["exterior"] == (
            100.0 + SOURCE_FAILURE_BACKOFF_BASE_SECONDS * 2
        )

    vehicle._record_source_success("exterior")

    assert vehicle._source_consecutive_failures["exterior"] == 0
    assert "exterior" not in vehicle._source_retry_after
