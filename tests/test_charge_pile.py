"""Home charge-pile tests replaying payloads captured from the Volvo app."""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.volvooncall_cn.switch import (
    VolvoHomeChargeSwitch,
    VolvoPlugAndChargeSwitch,
)
from custom_components.volvooncall_cn.volvooncall_base import VehicleBaseAPI
from custom_components.volvooncall_cn.volvooncall_cn import Vehicle

VIN = "TESTVIN0000000001"
SERIES_CODE = "238"
TRADE_NO = "TEST_TRADE"

# brandPile/getPileList entry while a session is running.
PILE_CHARGING = {
    "memberId": "TEST_MEMBER",
    "phone": "TEST_PHONE",
    "equipmentId": "TEST_EQUIPMENT",
    "equipmentName": "Test Home Charger",
    "connectorId": "TEST_CONNECTOR",
    "connectorStatus": 3,
    "connectorStatusName": "充电中",
    "orderNo": "TEST_ORDER",
    "tradeNo": TRADE_NO,
    "chargeUsePower": 0.0299,
    "chargeUseTime": "1",
    "address": "Test Garage",
    "plugAndChargeEnabled": 1,
}

# Same entry once the gun is plugged in but no order is running.
PILE_IDLE = {
    "memberId": "TEST_MEMBER",
    "equipmentId": "TEST_EQUIPMENT",
    "equipmentName": "Test Home Charger",
    "connectorId": "TEST_CONNECTOR",
    "connectorStatus": 2,
    "connectorStatusName": "已插枪",
    "address": "Test Garage",
    "plugAndChargeEnabled": 0,
}

# brandHomePile/status body for the running session above.
STATUS_CHARGING = {
    "startChargeSeq": TRADE_NO,
    "startChargeSeqStat": 2,
    "connectorID": "TEST_CONNECTOR",
    "connectorStatus": 3,
    "currentA": 15.84,
    "voltageA": 218.1,
    "totalPower": 0.0299,
    "estimatedChargingTime": "328",
    "batteryChargeLevelPercentage": "9.0",
    "estimatedDrivingKm": "4",
    "power": "3.5",
    "totalPowerStr": "0.03",
}


def _api(pile_list_response):
    api = VehicleBaseAPI(MagicMock(), "TEST_PHONE", "pw")
    api.digitalvolvo_get = AsyncMock(return_value=pile_list_response)
    api.digitalvolvo_post = AsyncMock()
    return api


def _pile_list(*piles):
    return {"success": True, "data": {"brandPileList": list(piles)}}


@pytest.mark.asyncio
async def test_get_pile_list_is_scoped_to_the_vin():
    """The app always passes the VIN; the order id is returned per vehicle."""
    api = _api(_pile_list(PILE_CHARGING))

    piles = await api.get_charge_piles(VIN, SERIES_CODE)

    assert piles == [PILE_CHARGING]
    url = api.digitalvolvo_get.await_args.args[0]
    assert "phone=TEST_PHONE" in url
    assert f"vin={VIN}" in url
    assert f"seriesCode={SERIES_CODE}" in url


@pytest.mark.asyncio
async def test_status_is_requested_with_the_live_trade_no():
    """status only answers for a running order, keyed by tradeNo + vinCode."""
    api = _api(_pile_list(PILE_CHARGING))
    api.digitalvolvo_post = AsyncMock(
        return_value={"success": True, "data": STATUS_CHARGING}
    )

    payload = await api.get_charge_pile_status(VIN, SERIES_CODE)

    assert api.digitalvolvo_post.await_args.args[2] == {
        "tradeNo": TRADE_NO,
        "vinCode": VIN,
    }
    assert payload["status"] == STATUS_CHARGING
    assert payload["pile"] == PILE_CHARGING


@pytest.mark.asyncio
async def test_idle_pile_skips_status_but_still_reports_the_connector():
    """With no order there is nothing to poll, yet the pile is still known."""
    api = _api(_pile_list(PILE_IDLE))

    payload = await api.get_charge_pile_status(VIN, SERIES_CODE)

    api.digitalvolvo_post.assert_not_awaited()
    assert payload["pile"] == PILE_IDLE
    assert payload["status"] == {}


@pytest.mark.asyncio
async def test_pile_list_cache_is_dropped_after_start():
    """The new order id must be visible to the next poll."""
    api = _api(_pile_list(PILE_IDLE))
    api.digitalvolvo_post = AsyncMock(
        return_value={"success": True, "data": {"startChargeSeq": TRADE_NO}}
    )

    await api.get_charge_piles(VIN, SERIES_CODE)
    await api.start_charge_pile(VIN, SERIES_CODE)
    api.digitalvolvo_get.return_value = _pile_list(PILE_CHARGING)

    assert await api.get_active_trade_no(VIN, SERIES_CODE) == TRADE_NO
    assert api.digitalvolvo_get.await_count == 3
    assert api.digitalvolvo_post.await_args.kwargs["max_attempts"] == 1


@pytest.mark.asyncio
async def test_stop_control_is_sent_once_without_transport_replay():
    """A stop command must use the captured payload and a single attempt."""
    api = _api(_pile_list(PILE_CHARGING))
    api.digitalvolvo_post = AsyncMock(
        return_value={
            "success": True,
            "data": {"startChargeSeqStat": 3},
        }
    )

    await api.stop_charge_pile(TRADE_NO, VIN, SERIES_CODE)

    assert api.digitalvolvo_post.await_args.args[2] == {
        "startChargeSeq": TRADE_NO,
        "connectorID": "TEST_CONNECTOR",
        "versions": "1",
    }
    assert api.digitalvolvo_post.await_args.kwargs["max_attempts"] == 1


@pytest.mark.asyncio
async def test_charge_order_history_is_cached_between_poll_cycles():
    """The large order-history response must not be downloaded every poll."""
    api = _api(_pile_list(PILE_IDLE))
    orders = [{"orderNo": "TEST_ORDER"}]
    api.digitalvolvo_post = AsyncMock(
        return_value={"success": True, "data": orders}
    )

    first = await api.get_charge_order_list(
        vin=VIN, series_code=SERIES_CODE
    )
    second = await api.get_charge_order_list(
        vin=VIN, series_code=SERIES_CODE
    )

    assert first == orders
    assert second == orders
    api.digitalvolvo_post.assert_awaited_once()


@pytest.mark.asyncio
async def test_charging_session_populates_telemetry():
    """A running session drives the charging sensors."""
    api = MagicMock()
    api.get_battery_status = AsyncMock(side_effect=RuntimeError("UNIMPLEMENTED"))
    api.get_charge_pile_status = AsyncMock(
        return_value={"pile": PILE_CHARGING, "status": STATUS_CHARGING}
    )
    api.get_charge_order_list = AsyncMock(return_value=[])
    vehicle = Vehicle(VIN, api, True, series_code=SERIES_CODE)

    await vehicle._parse_battery()

    assert vehicle.battery_charging_status == "charging"
    assert vehicle.charger_connection_status == "charging"
    assert vehicle.charging_power == 3.5
    assert vehicle.charging_voltage == 218.1
    assert vehicle.charging_current == 15.84
    assert vehicle.charging_session_energy == 0.0299
    assert vehicle.estimated_charging_time == 328
    assert vehicle.charge_trade_no == TRADE_NO
    assert vehicle.home_charge_status == "charging"
    assert vehicle.has_home_charge_pile is True
    assert vehicle.plug_and_charge_enabled is True
    assert vehicle.charge_pile_name == "Test Home Charger"


@pytest.mark.asyncio
async def test_idle_pile_reports_state_without_a_session():
    """A plugged-in but idle pile still reports connector and settings."""
    api = MagicMock()
    api.get_battery_status = AsyncMock(side_effect=RuntimeError("UNIMPLEMENTED"))
    api.get_charge_pile_status = AsyncMock(
        return_value={"pile": PILE_IDLE, "status": {}}
    )
    api.get_charge_order_list = AsyncMock(return_value=[])
    vehicle = Vehicle(VIN, api, True, series_code=SERIES_CODE)

    await vehicle._parse_battery()

    assert vehicle.battery_charging_status == "idle"
    assert vehicle.charger_connection_status == "plugged_in"
    assert vehicle.charge_trade_no is None
    assert vehicle.home_charge_status == "idle"
    assert vehicle.plug_and_charge_enabled is False
    assert vehicle.charge_pile_name == "Test Home Charger"


@pytest.mark.asyncio
async def test_idle_pile_does_not_clobber_battery_service_charging():
    """Charging on a public charger must survive an idle home pile."""
    from custom_components.volvooncall_cn.proto.battery_pb2 import (
        Battery,
        GetBatteryResponse,
    )

    api = MagicMock()
    api.get_battery_status = AsyncMock(
        return_value=GetBatteryResponse(
            vin=VIN,
            battery=Battery(
                batteryChargeLevelPercentage=76.5,
                estimatedChargingTimeToFullMinutes=42,
                chargerConnectionStatus=1,
                chargingStatus=1,
                chargingPowerWatts=3680,
            ),
        )
    )
    api.get_charge_pile_status = AsyncMock(
        return_value={"pile": PILE_IDLE, "status": {}}
    )
    api.get_charge_order_list = AsyncMock(return_value=[])
    vehicle = Vehicle(VIN, api, True, series_code=SERIES_CODE)

    await vehicle._parse_battery()

    assert vehicle.battery_charging_status == "charging"
    assert vehicle.charger_connection_status == "connected_ac"
    assert vehicle.charging_power == 3.68
    assert vehicle.estimated_charging_time == 42
    assert vehicle.charge_pile_name == "Test Home Charger"


@pytest.mark.asyncio
async def test_idle_session_clears_previous_home_pile_telemetry():
    """Ended sessions must not leave stale electrical values or order ids."""
    api = MagicMock()
    api.get_battery_status = AsyncMock(
        side_effect=RuntimeError("UNIMPLEMENTED")
    )
    api.get_charge_pile_status = AsyncMock(
        return_value={"pile": PILE_CHARGING, "status": STATUS_CHARGING}
    )
    api.get_charge_order_list = AsyncMock(return_value=[])
    vehicle = Vehicle(VIN, api, True, series_code=SERIES_CODE)

    await vehicle._parse_battery()
    api.get_charge_pile_status.return_value = {
        "pile": PILE_IDLE,
        "status": {},
    }
    await vehicle._parse_battery()

    assert vehicle.home_charge_status == "idle"
    assert vehicle.charge_trade_no is None
    assert vehicle.charging_voltage is None
    assert vehicle.charging_current is None
    assert vehicle.charging_session_energy is None


@pytest.mark.asyncio
async def test_stop_uses_the_pile_order_when_ha_did_not_start_it():
    """Sessions started from the app or by plug-and-charge are stoppable."""
    api = MagicMock()
    api.get_active_trade_no = AsyncMock(return_value=TRADE_NO)
    api.stop_charge_pile = AsyncMock(return_value={"startChargeSeqStat": 3})
    vehicle = Vehicle(VIN, api, True, series_code=SERIES_CODE)
    assert vehicle.charge_trade_no is None

    await vehicle.stop_home_charge()

    api.get_active_trade_no.assert_awaited_once_with(
        VIN, SERIES_CODE, force_refresh=True
    )
    api.stop_charge_pile.assert_awaited_once_with(
        TRADE_NO, VIN, SERIES_CODE
    )


@pytest.mark.asyncio
async def test_stop_without_any_session_raises():
    api = MagicMock()
    api.get_active_trade_no = AsyncMock(return_value=None)
    api.stop_charge_pile = AsyncMock()
    vehicle = Vehicle(VIN, api, True, series_code=SERIES_CODE)

    with pytest.raises(Exception, match="No active home-charge session"):
        await vehicle.stop_home_charge()

    api.stop_charge_pile.assert_not_awaited()


def test_home_switch_ignores_public_charging_state():
    """Public charging must never make the home-pile switch appear on."""
    vehicle = Vehicle(VIN, MagicMock(), True)
    vehicle.has_home_charge_pile = True
    vehicle.home_charge_status = "idle"
    vehicle.battery_charging_status = "charging"
    coordinator = MagicMock()
    coordinator.data = [vehicle]

    entity = VolvoHomeChargeSwitch(
        coordinator, 0, "home_charge_switch"
    )

    assert entity.is_on is False


def test_charge_switches_are_unavailable_without_a_linked_pile():
    """Electric vehicles without home hardware must not expose controls."""
    vehicle = Vehicle(VIN, MagicMock(), True)
    vehicle.has_home_charge_pile = False
    coordinator = MagicMock()
    coordinator.data = [vehicle]
    coordinator.last_update_success = True

    home = VolvoHomeChargeSwitch(
        coordinator, 0, "home_charge_switch"
    )
    plug = VolvoPlugAndChargeSwitch(
        coordinator, 0, "plug_and_charge_switch"
    )

    assert home.available is False
    assert plug.available is False


@pytest.mark.asyncio
async def test_home_switch_uses_forced_refresh_for_confirmation(monkeypatch):
    """Command confirmation must bypass the normal scan interval."""
    vehicle = Vehicle(VIN, MagicMock(), True)
    vehicle.has_home_charge_pile = True
    vehicle.home_charge_status = "starting"
    coordinator = MagicMock()
    coordinator.data = [vehicle]
    coordinator.async_force_refresh = AsyncMock()
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    entity = VolvoHomeChargeSwitch(
        coordinator, 0, "home_charge_switch"
    )

    await entity._update_status(True)

    coordinator.async_force_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_home_switch_raises_when_confirmation_never_arrives(
    monkeypatch,
):
    """A sent command must not be reported successful without confirmation."""
    vehicle = Vehicle(VIN, MagicMock(), True)
    vehicle.has_home_charge_pile = True
    vehicle.home_charge_status = "idle"
    coordinator = MagicMock()
    coordinator.data = [vehicle]
    coordinator.async_force_refresh = AsyncMock()
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    entity = VolvoHomeChargeSwitch(
        coordinator, 0, "home_charge_switch"
    )

    with pytest.raises(HomeAssistantError, match="could not be confirmed"):
        await entity._update_status(True)


@pytest.mark.asyncio
async def test_stop_evicts_the_cached_order_history():
    """A just-ended session must not read a stale 30-minute order cache."""
    api = _api(_pile_list(PILE_CHARGING))
    api.digitalvolvo_post = AsyncMock(
        return_value={"success": True, "data": {"startChargeSeqStat": 3}}
    )
    connector_id = PILE_CHARGING["connectorId"]
    api._charge_order_cache[connector_id] = (["stale_order"], time.time() + 999)

    await api.stop_charge_pile(TRADE_NO, VIN, SERIES_CODE)

    assert connector_id not in api._charge_order_cache


@pytest.mark.asyncio
async def test_start_evicts_the_cached_order_history():
    """A newly started session must invalidate the previous order cache."""
    api = _api(_pile_list(PILE_IDLE))
    api.digitalvolvo_post = AsyncMock(
        return_value={"success": True, "data": {"startChargeSeq": TRADE_NO}}
    )
    connector_id = PILE_IDLE["connectorId"]
    api._charge_order_cache[connector_id] = (["stale_order"], time.time() + 999)

    await api.start_charge_pile(VIN, SERIES_CODE)

    assert connector_id not in api._charge_order_cache
