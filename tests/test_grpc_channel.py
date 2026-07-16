"""Regression tests for gRPC channel self-healing.

Commands (lock/unlock/honk/...) used to build their stub directly from
``self.channel``, which is only created during a successful data poll.
Any poll failure before channel creation therefore broke every control
with "'NoneType' object has no attribute 'unary_stream'". Commands must
now create the channel on demand.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.volvooncall_cn.volvooncall_cn import (
    DOMAIN,
    Vehicle,
    VehicleAPI,
)
from custom_components.volvooncall_cn.volvooncall_base import VehicleBaseAPI
from custom_components.volvooncall_cn.proto.engineremotestart_pb2 import (
    EngineRunningStatus,
    GetEngineRemoteStartResp,
)

VIN = "LVYTESTVIN0000001"

# Unroutable local endpoint: RPCs fail fast without leaving the machine.
DEAD_GRPC_TARGET = "127.0.0.1:1"

LOGIN_RESP = {
    "success": True,
    "data": {
        "refreshToken": "rt",
        "globalAccessToken": "gat",
        "accessToken": "at",
        "jwtToken": "jwt",
        "expiresIn": "7200",
    },
}

LIST_BIND_CAR_RESP = {
    "success": True,
    "data": [
        {
            "vinCode": VIN,
            "seriesName": "XC60",
            "modelName": "T8",
            "modelYear": "2023",
            "seriesCode": "246",
        }
    ],
}

PILE_LIST_RESP = {"success": True, "data": {"brandPileList": []}}


async def fake_rest(self, method, url, headers, **kwargs):
    if "/auth" in url:
        return LOGIN_RESP
    if "listBindCar" in url:
        return LIST_BIND_CAR_RESP
    if "getPileList" in url:
        return PILE_LIST_RESP
    return {"success": True, "data": {}}


@pytest.mark.asyncio
async def test_command_creates_channel_on_demand():
    """door_lock must build its own channel instead of requiring a prior
    successful data poll to have created it."""
    api = VehicleAPI(session=MagicMock(), username="TEST_USERNAME", password="pw")
    assert api.channel is None

    with patch(
        "custom_components.volvooncall_cn.volvooncall_cn.GRPC_DIGITALVOLVO_HOST",
        DEAD_GRPC_TARGET,
    ):
        try:
            await api.door_lock(VIN)
        except AttributeError as err:
            pytest.fail(f"stub built from a None channel: {err}")
        except Exception:
            pass  # the RPC itself fails against the dead endpoint

    assert api.channel is not None


@pytest.mark.asyncio
async def test_lock_entity_survives_failed_first_poll(hass: HomeAssistant):
    """Even if every data poll failed before channel creation, pressing the
    lock must not raise the NoneType unary_stream error."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_USERNAME: "TEST_USERNAME",
            CONF_PASSWORD: "pw",
            CONF_SCAN_INTERVAL: 30,
        },
        unique_id="TEST_USERNAME",
        version=3,
    )
    entry.add_to_hass(hass)

    async def boom(api_self):
        raise Exception("simulated failure before channel creation")

    with patch.object(VehicleBaseAPI, "_request_digitalvolvo", fake_rest):
        # First poll: channel creation fails, entities are still created.
        with patch.object(VehicleAPI, "get_channel", boom):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

        coordinator = hass.data[DOMAIN][entry.entry_id]
        vehicle = coordinator.data[0]
        assert vehicle._api.channel is None
        assert hass.states.get(f"lock.{VIN.lower()}_lock") is not None

        # The command must now self-heal the channel; only the RPC itself
        # may fail (dead endpoint), never stub construction.
        with patch(
            "custom_components.volvooncall_cn.volvooncall_cn.GRPC_DIGITALVOLVO_HOST",
            DEAD_GRPC_TARGET,
        ):
            try:
                await vehicle.lock_vehicle()
            except AttributeError as err:
                pytest.fail(f"stub built from a None channel: {err}")
            except Exception:
                pass

        assert vehicle._api.channel is not None


@pytest.mark.asyncio
async def test_parse_engine_status_calls_existing_api_method():
    """_parse_engine_status must call a method that exists on VehicleAPI
    (it used to call the non-existent get_engine_remote_start_status) and
    read real proto fields (it used to reference EngineRunningStatus.STARTED
    and engineStartTimestamp/engineStopTimestamp, none of which exist)."""
    resp = GetEngineRemoteStartResp(vin=VIN)
    resp.data.engineRunningStatus = EngineRunningStatus.Running
    resp.data.engineStartTime.seconds = 1720000000
    resp.data.engineEndTime.seconds = 1720000900

    api = AsyncMock(spec=VehicleAPI)
    api.get_engine_status.return_value = resp

    vehicle = Vehicle(VIN, api, isAaos=True)
    await vehicle._parse_engine_status()

    assert vehicle._data_source_status["engine_status"] is True
    api.get_engine_status.assert_awaited_once_with(VIN)
    assert vehicle.engine_remote_running is True
    assert vehicle.engine_remote_start_time == 1720000000
    assert vehicle.engine_remote_end_time == 1720000900
