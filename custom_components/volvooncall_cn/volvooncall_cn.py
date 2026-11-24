import logging
import datetime
import grpc
from grpc import aio as grpc_aio
import asyncio
import math
import time
from datetime import datetime as dt, timedelta, timezone
from typing import Dict, Any
from homeassistant.exceptions import HomeAssistantError
from .volvooncall_base import (
    VehicleBaseAPI,
    VolvoAPIError,
    gcj02towgs84,
    redact_sensitive,
    vehicle_log_ref,
)
from .proto.exterior_pb2_grpc import ExteriorServiceStub
from .proto.exterior_pb2 import GetExteriorReq, GetExteriorResp, ExteriorStatus
from .proto.exterior_pb2 import LockStatus, OpenStatus
from .proto.health_pb2_grpc import HealthServiceStub
from .proto.health_pb2 import GetHealthReq, GetHealthResp, HealthStatus
from .proto.fuel_pb2_grpc import FuelServiceStub
from .proto.fuel_pb2 import GetFuelReq, GetFuelResp
from .proto.invocation_pb2_grpc import InvocationServiceStub
from .proto.invocation_pb2 import invocationHead, invocationStatus, invocationControlType
from .proto.invocation_pb2 import windowControlReq
from .proto.invocation_pb2 import EngineStartReq
from .proto.invocation_pb2 import ClimatizationStartReq, ClimatizationStopReq
from .proto.invocation_pb2 import HonkFlashReq, HonkFlashType
from .proto.invocation_pb2 import LockReq, LockType
from .proto.invocation_pb2 import UnlockReq, UnlockType
from .proto.invocation_pb2 import TailgateControlReq
from .proto.invocation_pb2 import SunroofControlReq
from .proto.invocation_pb2 import UpdateStatusReq
from .proto.odometer_pb2_grpc import OdometerServiceStub
from .proto.odometer_pb2 import GetOdometerReq, GetOdometerResp
from .proto.availability_pb2_grpc import AvailabilityServiceStub
from .proto.availability_pb2 import GetAvailabilityReq, GetAvailabilityResp, AvailabilityStatus, AvailabilityReason
from .proto.dtlinternet_pb2_grpc import DtlInternetServiceStub
from .proto.dtlinternet_pb2 import StreamLastKnownLocationsReq, StreamLastKnownLocationsResp
from .proto.engineremotestart_pb2_grpc import EngineRemoteStartServiceStub
from .proto.engineremotestart_pb2 import GetEngineRemoteStartReq, GetEngineRemoteStartResp, EngineRunningStatus
from .proto.car_preferences_pb2_grpc import CarPreferencesStub
from .proto.car_preferences_pb2 import GetPreferencesReq, GetPreferencesResp
from .proto.car_preferences_pb2 import UpdatePreferencesReq, Preference
from .proto.battery_pb2_grpc import BatteryServiceStub
from .proto.battery_pb2 import GetBatteryRequest, GetBatteryResponse


_LOGGER = logging.getLogger(__name__)

GRPC_DIGITALVOLVO_HOST = "cepmobtoken.prod.c3.volvocars.com.cn:443"
GRPC_LBS_VOLVO_HOST = "cepmobtoken.lbs.prod.c3.volvocars.com.cn:443"
USER_AGENT = "vca-android/5.53.1 grpc-java-okhttp/1.68.0"
TIMEOUT = datetime.timedelta(seconds=10)
TIMEOUT_SECONDS = int(TIMEOUT.total_seconds())
DOMAIN = "volvooncall_cn"

# Keepalive matters here: the integration runs for months behind consumer
# NAT, and an idle HTTP/2 connection that a middlebox silently dropped is
# only discovered when the next RPC times out — costing a whole poll cycle
# for every data source on that channel.
GRPC_CHANNEL_OPTIONS = (
    ("grpc.primary_user_agent", USER_AGENT),
    ("grpc.accept_encoding", "gzip"),
    ("grpc.keepalive_time_ms", 60_000),
    ("grpc.keepalive_timeout_ms", 10_000),
    ("grpc.keepalive_permit_without_calls", 1),
    ("grpc.http2.max_pings_without_data", 0),
)

# How often each data source is refreshed, in poll cycles. Vehicle state that
# can change at any moment is read every cycle; the rest is throttled. For a
# hybrid on the default 30s interval this takes a steady-state cycle from ~12
# requests to ~9.3 on average — roughly 7,700 fewer requests per day per car.
SOURCE_REFRESH_EVERY_CYCLES = {
    "health": 20,       # fluid/tyre warnings: ~10 min at a 30s interval
    "preference": 240,  # vehicle nickname: ~2 h
    "odometer": 4,      # ~2 min; only moves while driving
}
# The cycle counts above assume the minimum 30s poll. With a longer interval
# (dynamic polling at night runs every 30 minutes) counting cycles alone
# would stretch "health every 20 cycles" to ten hours, so a source is also
# due once this much wall time has passed since it last refreshed. The 1.5x
# margin keeps the cycle count in charge at the nominal interval.
SOURCE_REFRESH_NOMINAL_CYCLE_SECONDS = 30
SOURCE_REFRESH_TIME_CEILING_FACTOR = 1.5

# A failed source is retried independently from healthy sources. Start at the
# minimum poll interval, then back off exponentially so a persistent endpoint
# outage cannot turn every coordinator callback into a request/log storm.
SOURCE_FAILURE_BACKOFF_BASE_SECONDS = 30
SOURCE_FAILURE_BACKOFF_MAX_SECONDS = 5 * 60

# How long a failed source may keep serving its last good snapshot. Lock and
# door state is security relevant, so it goes "unknown" much sooner than the
# slow-moving sources: an hour-old "locked" is worse than no answer.
SOURCE_CACHE_MAX_AGE = timedelta(hours=1)
SOURCE_CACHE_MAX_AGE_OVERRIDES = {
    "exterior": timedelta(minutes=10),
}

# Honk/flash and parked climatization have no state to confirm afterwards, so
# their only feedback is the invocation stream. The first message is normally
# just SENT; keep reading this long for a terminal status (CAR_OFFLINE, a
# timeout, a refusal) before settling for the acknowledgement.
INVOCATION_FOLLOW_UP_SECONDS = 5

CHARGE_PILE_EXCLUSIVE_FIELDS = (
    "charge_trade_no",
    "home_charge_status",
    "has_home_charge_pile",
    "plug_and_charge_enabled",
    "last_charge_order",
    "charge_pile_name",
    "charge_pile_address",
    "charging_voltage",
    "charging_current",
    "charging_session_energy",
)


_NO_STREAM_DEFAULT = object()


async def _first_stream_response(call, default=_NO_STREAM_DEFAULT):
    """Await the first message of a server-streaming RPC, then cancel it.

    All of Volvo's RPCs are unary-stream; the app only ever consumes the
    first message. Cancelling releases the stream without waiting for the
    server to close it.
    """
    try:
        async for res in call:
            return res
    finally:
        call.cancel()
    if default is not _NO_STREAM_DEFAULT:
        return default
    raise VolvoAPIError("gRPC stream completed without a response")


def _require_message_field(response, field: str, operation: str):
    """Reject a protobuf response whose required message payload is absent."""
    if not response.HasField(field):
        raise VolvoAPIError(f"{operation} returned no {field} payload")
    return response

BATTERY_CHARGING_STATUS = {
    0: "unknown",
    1: "charging",
    2: "idle",
    3: "completed",
    4: "fault",
    5: "scheduled",
    6: "smart_charging",
    7: "discharging",
}
BATTERY_CONNECTION_STATUS = {
    0: "unknown",
    # BatteryService reports 1 while the vehicle is connected to AC power and
    # 2 after the connector is removed.  Do not confuse this with the home
    # pile's connectorStatus enum below, which uses different values.
    1: "connected_ac",
    2: "disconnected",
    3: "connected_dc",
}
PILE_CONNECTION_STATUS = {
    0: "offline",
    1: "idle",
    2: "plugged_in",
    3: "charging",
    4: "reserved",
    255: "fault",
}
# startChargeSeqStat as reported by brandHomePile/status.
PILE_CHARGING_STATUS = {
    0: "not_started",
    1: "starting",
    2: "charging",
    3: "stopping",
    4: "completed",
}


def isWindowOpen(status) -> bool:
    return status == OpenStatus.OPEN_STATUS_OPEN or status == OpenStatus.OPEN_STATUS_AJAR


class VehicleAPI(VehicleBaseAPI):
    def __init__(self, session, username, password):
        super(VehicleAPI, self).__init__(session, username, password)
        self.channel = None
        self.lbs_channel = None
        self._channel_lock = asyncio.Lock()
        self._lbs_channel_lock = asyncio.Lock()

    def _metadata_callback(self, context, callback):
        token = self._vocapi_access_token.strip()
        metadata = [('authorization', f'Bearer {token}')]
        callback(metadata, None)

    async def gen_channel(self, target):
        # grpc.aio keeps every RPC on the event loop; the previous sync
        # channel blocked Home Assistant's loop for up to the full RPC
        # timeout on every call.
        callCreds = grpc.metadata_call_credentials(self._metadata_callback)
        sslCreds = grpc.ssl_channel_credentials()
        creds = grpc.composite_channel_credentials(sslCreds, callCreds)
        channel = grpc_aio.secure_channel(
            target, creds, options=GRPC_CHANNEL_OPTIONS
        )
        return channel

    async def get_channel(self):
        if self.channel:
            return self.channel

        async with self._channel_lock:
            if not self.channel:
                self.channel = await self.gen_channel(GRPC_DIGITALVOLVO_HOST)
        return self.channel

    async def get_lbs_channel(self):
        if self.lbs_channel:
            return self.lbs_channel

        async with self._lbs_channel_lock:
            if not self.lbs_channel:
                self.lbs_channel = await self.gen_channel(GRPC_LBS_VOLVO_HOST)
        return self.lbs_channel

    async def close(self):
        """Release both gRPC channels (entry unload or API replacement)."""
        channel, self.channel = self.channel, None
        lbs_channel, self.lbs_channel = self.lbs_channel, None
        for open_channel in (channel, lbs_channel):
            if open_channel is None:
                continue
            try:
                await open_channel.close()
            except Exception as err:
                _LOGGER.debug("Error closing gRPC channel: %s", err)

    def raise_invocation_fail(self, status):
        # HomeAssistantError, not a bare Exception: Home Assistant renders it
        # as a clean message on the service call instead of dumping a stack
        # trace into the log for what is an expected vehicle-side refusal.
        if status in [invocationStatus.SUCCESS, invocationStatus.SENT, invocationStatus.DELIVERED]:
            return
        if status == invocationStatus.CAR_OFFLINE:
            raise HomeAssistantError("车辆离线或无网络")
        elif status in [invocationStatus.DELIVERY_TIMEOUT, invocationStatus.RESPONSE_TIMEOUT]:
            raise HomeAssistantError("请求超时")
        elif status == invocationStatus.UNKNOWN_CAR_ERROR:
            raise HomeAssistantError("车辆未知错误")
        elif status == invocationStatus.NOT_ALLOWED_PRIVACY_ENABLED:
            raise HomeAssistantError("车辆隐私协议未同意")
        elif status == invocationStatus.NOT_ALLOWED_WRONG_USAGE_MODE:
            raise HomeAssistantError("请求模式错误")
        elif status == invocationStatus.NOT_ALLOWED_CONFLICTING_INVOCATION:
            raise HomeAssistantError("请求操作存在冲突")
        else:
            raise HomeAssistantError("未知错误")

    def _validate_invocation_response(self, response, operation: str) -> None:
        """Require a server acknowledgement before accepting a control."""
        _require_message_field(response, "data", operation)
        self.raise_invocation_fail(response.data.status)

    def _note_grpc_error(self, err) -> None:
        """Invalidate the session when gRPC says the token is not accepted."""
        try:
            unauthenticated = err.code() == grpc.StatusCode.UNAUTHENTICATED
        except Exception:  # pragma: no cover - defensive
            return
        if unauthenticated and self.note_grpc_unauthenticated():
            _LOGGER.warning(
                "gRPC rejected the session token; re-authenticating on the "
                "next poll"
            )

    async def _first_response(self, call, default=_NO_STREAM_DEFAULT):
        """_first_stream_response plus gRPC authentication-failure handling."""
        try:
            return await _first_stream_response(call, default)
        except grpc_aio.AioRpcError as err:
            self._note_grpc_error(err)
            raise

    async def _run_invocation(
        self, call, operation: str, *, follow_up_seconds: float = 0
    ):
        """Send a control and validate the server's verdict on it.

        The first stream message must be an accepted status. With
        follow_up_seconds, later messages are read for that long as well so a
        terminal failure reported after the initial SENT is not mistaken for
        success. Running out of time, or the stream ending, leaves the
        acknowledgement standing.
        """
        try:
            stream = call.__aiter__()
            try:
                response = await stream.__anext__()
            except StopAsyncIteration:
                raise VolvoAPIError(
                    "gRPC stream completed without a response"
                ) from None
            except grpc_aio.AioRpcError as err:
                self._note_grpc_error(err)
                raise
            _LOGGER.debug("%s: %s", operation, redact_sensitive(response))
            self._validate_invocation_response(response, operation)
            if (
                follow_up_seconds <= 0
                or response.data.status == invocationStatus.SUCCESS
            ):
                return response

            try:
                async with asyncio.timeout(follow_up_seconds):
                    while True:
                        try:
                            follow_up = await stream.__anext__()
                        except StopAsyncIteration:
                            break
                        if not follow_up.HasField("data"):
                            continue
                        _LOGGER.debug(
                            "%s follow-up: %s",
                            operation,
                            redact_sensitive(follow_up),
                        )
                        self.raise_invocation_fail(follow_up.data.status)
                        response = follow_up
                        if follow_up.data.status == invocationStatus.SUCCESS:
                            break
            except TimeoutError:
                pass
            except grpc_aio.AioRpcError as err:
                # The command was already acknowledged; a stream that breaks
                # afterwards (deadline, reset) does not undo that.
                _LOGGER.debug(
                    "%s stream ended after the acknowledgement: %s",
                    operation,
                    redact_sensitive(err),
                )
            return response
        finally:
            call.cancel()

    async def get_fuel_status(self, vin) -> GetFuelResp:
        stub = FuelServiceStub(await self.get_channel())
        req = GetFuelReq(vin=vin)
        metadata: list = [("vin", vin)]
        res = await self._first_response(
            stub.GetFuel(req, metadata=metadata, timeout=TIMEOUT_SECONDS)
        )
        return _require_message_field(res, "data", "FuelService.GetFuel")

    async def get_exterior(self, vin) -> GetExteriorResp:
        stub = ExteriorServiceStub(await self.get_channel())
        req = GetExteriorReq(vin=vin)
        metadata: list = [("vin", vin)]
        res = await self._first_response(
            stub.GetExterior(req, metadata=metadata, timeout=TIMEOUT_SECONDS)
        )
        return _require_message_field(
            res, "data", "ExteriorService.GetExterior"
        )

    async def get_health(self, vin) -> GetHealthResp:
        stub = HealthServiceStub(await self.get_channel())
        req = GetHealthReq(vin=vin)
        metadata: list = [("vin", vin)]
        res = await self._first_response(
            stub.GetHealth(req, metadata=metadata, timeout=TIMEOUT_SECONDS)
        )
        _require_message_field(res, "data", "HealthService.GetHealth")
        _LOGGER.debug("get_health resp: %s", redact_sensitive(res))
        return res

    async def get_odometer(self, vin) -> GetOdometerResp:
        stub = OdometerServiceStub(await self.get_channel())
        req = GetOdometerReq(vin=vin)
        metadata: list = [("vin", vin)]
        res = await self._first_response(
            stub.GetOdometer(req, metadata=metadata, timeout=TIMEOUT_SECONDS)
        )
        return _require_message_field(
            res, "data", "OdometerService.GetOdometer"
        )

    async def get_availability(self, vin) -> GetAvailabilityResp:
        stub = AvailabilityServiceStub(await self.get_channel())
        req = GetAvailabilityReq(vin=vin)
        metadata: list = [("vin", vin)]
        res = await self._first_response(
            stub.GetAvailability(req, metadata=metadata, timeout=TIMEOUT_SECONDS)
        )
        return _require_message_field(
            res, "data", "AvailabilityService.GetAvailability"
        )

    async def window_control(self, vin, opentype):
        stub = InvocationServiceStub(await self.get_channel())
        req_header = invocationHead(vin=vin)
        req = windowControlReq(head=req_header, openType=opentype)
        metadata: list = [("vin", vin)]
        res = await self._first_response(
            stub.WindowControl(req, metadata=metadata, timeout=TIMEOUT_SECONDS)
        )
        _LOGGER.debug("%s", redact_sensitive(res))
        self._validate_invocation_response(res, "InvocationService.WindowControl")
        return

    async def get_location(self, vin) -> StreamLastKnownLocationsResp:
        stub = DtlInternetServiceStub(await self.get_lbs_channel())
        req = StreamLastKnownLocationsReq(vin=vin)
        metadata: list = [("vin", vin)]
        res = await self._first_response(
            stub.StreamLastKnownLocations(req, metadata=metadata, timeout=TIMEOUT_SECONDS)
        )
        return res

    async def engine_control(self, vin, isStart: bool, duration: int):
        stub = InvocationServiceStub(await self.get_channel())
        req_header = invocationHead(vin=vin)
        req = EngineStartReq()
        if isStart:
            req = EngineStartReq(head=req_header, isStart=isStart, startDurationMin=duration)
        else:
            req = EngineStartReq(head=req_header, isStart=isStart)
        metadata: list = [("vin", vin)]
        res = await self._first_response(
            stub.EngineStart(req, metadata=metadata, timeout=TIMEOUT_SECONDS)
        )
        _LOGGER.debug("%s", redact_sensitive(res))
        self._validate_invocation_response(res, "InvocationService.EngineStart")
        return

    async def climatization_control(self, vin: str, is_start: bool):
        """Start or stop parked climatization without starting the engine."""
        stub = InvocationServiceStub(await self.get_channel())
        req_header = invocationHead(vin=vin)
        metadata: list = [("vin", vin)]

        if is_start:
            req = ClimatizationStartReq(
                head=req_header,
                start=True,
                compartmentTemperatureCelsius=0,
            )
            responses = stub.ClimatizationStart(
                req, metadata=metadata, timeout=TIMEOUT_SECONDS
            )
        else:
            req = ClimatizationStopReq(head=req_header)
            responses = stub.ClimatizationStop(
                req, metadata=metadata, timeout=TIMEOUT_SECONDS
            )

        await self._run_invocation(
            responses,
            (
                "InvocationService.ClimatizationStart"
                if is_start
                else "InvocationService.ClimatizationStop"
            ),
            follow_up_seconds=INVOCATION_FOLLOW_UP_SECONDS,
        )

    async def honk_flash_control(self, vin, honk_flash_type: HonkFlashType):
        stub = InvocationServiceStub(await self.get_channel())
        req_header = invocationHead(vin=vin)
        req = HonkFlashReq(head=req_header, honkFlashType=honk_flash_type)
        metadata: list = [("vin", vin)]
        await self._run_invocation(
            stub.HonkFlash(req, metadata=metadata, timeout=TIMEOUT_SECONDS),
            "InvocationService.HonkFlash",
            follow_up_seconds=INVOCATION_FOLLOW_UP_SECONDS,
        )

    async def door_lock(self, vin):
        stub = InvocationServiceStub(await self.get_channel())
        req_header = invocationHead(vin=vin)
        req = LockReq(head=req_header, lockType=LockType.LOCK_REDUCED_GUARD)
        metadata: list = [("vin", vin)]
        res = await self._first_response(
            stub.Lock(req, metadata=metadata, timeout=TIMEOUT_SECONDS)
        )
        _LOGGER.debug("%s", redact_sensitive(res))
        self._validate_invocation_response(res, "InvocationService.Lock")
        return

    async def door_unlock(self, vin, unlockType):
        stub = InvocationServiceStub(await self.get_channel())
        req_header = invocationHead(vin=vin)
        req = UnlockReq(head=req_header)
        if unlockType != UnlockType.UNLOCK_UNSPECIFIED:
            req = UnlockReq(head=req_header, unlockType=unlockType)
        metadata: list = [("vin", vin)]
        res = await self._first_response(
            stub.Unlock(req, metadata=metadata, timeout=TIMEOUT_SECONDS)
        )
        _LOGGER.debug("%s", redact_sensitive(res))
        self._validate_invocation_response(res, "InvocationService.Unlock")
        return

    async def get_engine_status(self, vin):
        stub = EngineRemoteStartServiceStub(await self.get_channel())
        req = GetEngineRemoteStartReq(vin=vin)
        metadata: list = [("vin", vin)]
        res = await self._first_response(
            stub.GetEngineRemoteStart(req, metadata=metadata, timeout=TIMEOUT_SECONDS)
        )
        _require_message_field(
            res, "data", "EngineRemoteStartService.GetEngineRemoteStart"
        )
        _LOGGER.debug("%s", redact_sensitive(res))
        return res

    async def sunroof_contorl(self, vin: str, controlType: invocationControlType):
        stub = InvocationServiceStub(await self.get_channel())
        req_header = invocationHead(vin=vin)
        req = SunroofControlReq(head=req_header, type=controlType)
        metadata: list = [("vin", vin)]
        res = await self._first_response(
            stub.SunroofControl(req, metadata=metadata, timeout=TIMEOUT_SECONDS)
        )
        _LOGGER.debug("%s", redact_sensitive(res))
        self._validate_invocation_response(res, "InvocationService.SunroofControl")
        return

    async def tailgate_contorl(self, vin: str, controlType: invocationControlType):
        stub = InvocationServiceStub(await self.get_channel())
        req_header = invocationHead(vin=vin)
        req = TailgateControlReq(head=req_header, type=controlType)
        metadata: list = [("vin", vin)]
        res = await self._first_response(
            stub.TailgateControl(req, metadata=metadata, timeout=TIMEOUT_SECONDS)
        )
        _LOGGER.debug("%s", redact_sensitive(res))
        self._validate_invocation_response(res, "InvocationService.TailgateControl")
        return

    async def update_status(self, vin: str):
        stub = InvocationServiceStub(await self.get_channel())
        req_header = invocationHead(vin=vin)
        req = UpdateStatusReq(head=req_header)
        metadata: list = [("vin", vin)]
        res = await self._first_response(
            stub.UpdateStatus(req, metadata=metadata, timeout=TIMEOUT_SECONDS)
        )
        _LOGGER.debug("update_status resp: %s", redact_sensitive(res))
        self._validate_invocation_response(res, "InvocationService.UpdateStatus")
        return

    async def get_car_preferences(self, vin: str):
        stub = CarPreferencesStub(await self.get_channel())
        req = GetPreferencesReq(vin=vin)
        metadata: list = [("vin", vin)]
        res = await self._first_response(
            stub.GetPreferences(req, metadata=metadata, timeout=TIMEOUT_SECONDS)
        )
        _require_message_field(
            res, "preference", "CarPreferences.GetPreferences"
        )
        _LOGGER.debug("%s", redact_sensitive(res))
        return res

    async def get_battery_status(self, vin: str) -> GetBatteryResponse:
        """Fetch PHEV/BEV battery and charging status."""
        stub = BatteryServiceStub(await self.get_channel())
        req = GetBatteryRequest(vin=vin)
        metadata: list = [("vin", vin)]
        res = await self._first_response(
            stub.GetLatestBattery(req, metadata=metadata, timeout=TIMEOUT_SECONDS)
        )
        return _require_message_field(
            res, "battery", "BatteryService.GetLatestBattery"
        )

    async def update_car_preference(self, vin: str, nickname: str):
        stub = CarPreferencesStub(await self.get_channel())
        preference = Preference(nickName=nickname)
        req = UpdatePreferencesReq(vin=vin, preference=preference)
        metadata: list = [("vin", vin)]
        res = await self._first_response(
            stub.UpdatePreferences(req, metadata=metadata, timeout=TIMEOUT_SECONDS)
        )
        _LOGGER.debug("%s", redact_sensitive(res))
        return res


class Vehicle(object):
    def __init__(
        self,
        vin,
        api,
        isAaos,
        supports_electric=True,
        series_code=None,
    ):
        self.vin = vin
        self._api = api
        self.isAaos = isAaos
        self.supports_electric = supports_electric
        self.series_code = series_code

        self.series_name = ""
        self.model_name = ""
        # Everything a data source owns starts as None ("unknown"), never
        # as a plausible value. With False/0 defaults a first poll in which
        # one source failed published "unlocked", "doors closed", 0 L of
        # fuel and a 0 km odometer as if the car had reported them — which
        # fires unlock/low-fuel automations after a restart and resets
        # total_increasing statistics.
        self.car_locked = None
        self.distance_to_empty = None  # 续航公里
        self.tail_gate_open = None
        self.rear_right_door_open = None
        self.rear_left_door_open = None
        self.front_right_door_open = None
        self.front_left_door_open = None
        self.hood_open = None
        self.sunroof_open = None
        self.engine_running = None
        self.engine_remote_running = None
        self.odo_meter = None
        self.front_left_window_open = None
        self.front_right_window_open = None
        self.rear_left_window_open = None
        self.rear_right_window_open = None
        self.front_left_window_open_ajar = None
        self.front_right_window_open_ajar = None
        self.rear_left_window_open_ajar = None
        self.rear_right_window_open_ajar = None
        self.fuel_amount = None
        self.fuel_average_consumption_liters_per_100_km = None
        self.tm_distance = None
        self.tm_fuel_consumption = None
        self.tm_energy_consumption = None
        self.tm_average_speed = None
        self.ta_distance = None
        self.ta_fuel_consumption = None
        self.ta_average_speed = None
        self.battery_charge_level_percentage = None
        self.electric_range = None
        self.battery_charging_status = None
        self.charger_connection_status = None
        self.estimated_charging_time = None
        self.charging_power = None
        self.charging_voltage = None
        self.charging_current = None
        self.charging_session_energy = None
        self.charge_trade_no = None
        self.home_charge_status = None
        self.has_home_charge_pile = False
        self.plug_and_charge_enabled = None
        self.last_charge_order = None
        self.charge_data_source = None
        self.charge_pile_name = None
        self.charge_pile_address = None
        self.tank_lid_open = None
        self.availability_status = None
        self.unavailable_reason = None
        self.engine_remote_start_time = None
        self.engine_remote_end_time = None
        # self.fuel_amount_level = 0
        # None, not (0, 0): a zeroed fix is a real coordinate in the Gulf of
        # Guinea, so defaulting to it parked every not-yet-located car there
        # on the map instead of reporting an unknown position.
        self.position = {
            "longitude": None,
            "latitude": None,
        }
        self.position_wgs84 = {
            "longitude": None,
            "latitude": None,
        }
        # Set by the coordinator when the car stops appearing in the account
        # listing; entities consult it to go unavailable.
        self.is_unbound = False
        self.service_warning_msg = None
        self.service_warning = None
        self.brake_fluid_level_warning = None
        self.engine_coolant_level_warning = None
        self.oil_level_warning = None
        self.washer_fluid_level_warning = None
        self.front_left_tyre_pressure_warning = None
        self.front_right_tyre_pressure_warning = None
        self.rear_left_tyre_pressure_warning = None
        self.rear_right_tyre_pressure_warning = None
        self.nickname = ""

        # Caching infrastructure for resilience
        self._poll_cycle = 0  # drives SOURCE_REFRESH_EVERY_CYCLES throttling
        # Monotonic time of each source's last successful refresh.
        self._source_refreshed_monotonic: Dict[str, float] = {}
        # Extra cache lifetime granted by the coordinator when it polls at a
        # long (dynamic) interval; zero for fixed-interval polling.
        self.cache_age_allowance = timedelta(0)
        self._cache: Dict[str, Any] = {}  # Stores last known good values
        self._cache_timestamp: Dict[str, dt] = {}  # Timestamps for each data source
        self._last_successful_update = dt.now(timezone.utc)
        self._consecutive_failures = 0
        self._data_source_status: Dict[str, bool] = {
            "exterior": True,
            "fuel": True,
            "odometer": True,
            "health": True,
            "location": True,
            "availability": True,
            "engine_status": True,
            "preference": True,
        }
        if self.supports_electric:
            self._data_source_status["battery"] = True
            self._data_source_status["charge_pile"] = True
        # A True status can be historical. Controls also compare this
        # generation before/after their forced poll so a cached or temporarily
        # absent vehicle cannot confirm an optimistic local state.
        self._data_source_generation: Dict[str, int] = {
            source: -1 for source in self._data_source_status
        }
        self._source_consecutive_failures: Dict[str, int] = {
            source: 0 for source in self._data_source_status
        }
        self._source_retry_after: Dict[str, float] = {}


    def _save_to_cache(self, source: str, data_dict: Dict[str, Any]):
        """Save successful data to cache."""
        # Shallow copy is enough: every value in these snapshots is a scalar
        # or a freshly built dict that nothing else holds a reference to.
        self._cache[source] = dict(data_dict)
        self._cache_timestamp[source] = dt.now(timezone.utc)
        self._source_refreshed_monotonic[source] = time.monotonic()
        self._last_successful_update = dt.now(timezone.utc)
        self._data_source_status[source] = True
        self._data_source_generation[source] = max(
            self._data_source_generation.get(source, -1) + 1,
            self._poll_cycle + 1,
        )
        self._source_consecutive_failures[source] = 0
        self._source_retry_after.pop(source, None)
        self._consecutive_failures = 0
        _LOGGER.debug("Cached %s data for %s", source, vehicle_log_ref(self.vin))

    def _clear_stale_source_values(
        self, source: str, keys: set[str] | None = None
    ) -> None:
        """Drop an expired snapshot and clear only the values it owned."""
        cached_data = self._cache.pop(source, {})
        self._cache_timestamp.pop(source, None)
        for key in cached_data:
            if keys is not None and key not in keys:
                continue
            if hasattr(self, key):
                setattr(self, key, None)

    def _restore_from_cache(
        self, source: str, *, keys: set[str] | None = None
    ) -> bool:
        """Restore data from cache if available and not too old."""
        if source not in self._cache:
            return False

        # Check if cache is not too old (1 hour unless the source overrides)
        cache_age = dt.now(timezone.utc) - self._cache_timestamp.get(source, dt.min.replace(tzinfo=timezone.utc))
        max_age = (
            SOURCE_CACHE_MAX_AGE_OVERRIDES.get(source, SOURCE_CACHE_MAX_AGE)
            + self.cache_age_allowance
        )
        if cache_age > max_age:
            _LOGGER.warning(
                "Cache for %s is too old (%s), clearing stale values",
                source,
                cache_age,
            )
            self._clear_stale_source_values(source, keys)
            return False

        # Restore cached values
        cached_data = self._cache[source]
        for key, value in cached_data.items():
            if keys is not None and key not in keys:
                continue
            if hasattr(self, key):
                setattr(self, key, value)

        _LOGGER.info(
            "Restored %s from cache (age: %s) for %s",
            source,
            cache_age,
            vehicle_log_ref(self.vin),
        )
        return True

    def _record_source_failure(self, source: str) -> None:
        """Back off one failing source using a monotonic exponential timer."""
        self._data_source_status[source] = False
        failures = self._source_consecutive_failures.get(source, 0) + 1
        self._source_consecutive_failures[source] = failures
        delay = min(
            SOURCE_FAILURE_BACKOFF_BASE_SECONDS
            * (2 ** min(failures - 1, 16)),
            SOURCE_FAILURE_BACKOFF_MAX_SECONDS,
        )
        self._source_retry_after[source] = time.monotonic() + delay
        _LOGGER.debug(
            "Backing off %s for %ss after source failure #%s on %s",
            source,
            delay,
            failures,
            vehicle_log_ref(self.vin),
        )

    def _record_source_success(self, source: str) -> None:
        """Clear source-specific failure and backoff state."""
        self._data_source_status[source] = True
        self._source_consecutive_failures[source] = 0
        self._source_retry_after.pop(source, None)
    
    def get_cache_info(self) -> Dict[str, Any]:
        """Get cache status for diagnostics."""
        return {
            "last_update": self._last_successful_update,
            "consecutive_failures": self._consecutive_failures,
            "data_sources": self._data_source_status.copy(),
            "cached_sources": list(self._cache.keys()),
        }
    
    @property
    def connection_status(self) -> str:
        """Return connection status for diagnostic sensor."""
        failed_sources = [
            key for key, available in self._data_source_status.items()
            if not available
        ]
        if failed_sources:
            return f"Degraded ({len(failed_sources)} sources failed)"
        if self._consecutive_failures == 0:
            return "Connected"
        elif self._consecutive_failures < 3:
            return "Degraded"
        else:
            return f"Disconnected ({self._consecutive_failures} failures)"
    
    @property
    def last_update_time(self) -> dt:
        """Return last successful update time for diagnostic sensor."""
        return self._last_successful_update

    async def _parse_exterior(self):
        try:
            exterior_resp: GetExteriorResp = await self._api.get_exterior(self.vin)
            _require_message_field(
                exterior_resp, "data", "ExteriorService.GetExterior"
            )
            exterior_status: ExteriorStatus = exterior_resp.data
            
            # Build data dict before setting attributes
            data = {
                "car_locked": exterior_status.central_lock == LockStatus.LOCK_STATUS_LOCKED,
                "front_left_door_open": isWindowOpen(exterior_status.front_left_door),
                "front_right_door_open": isWindowOpen(exterior_status.front_right_door),
                "rear_left_door_open": isWindowOpen(exterior_status.rear_left_door),
                "rear_right_door_open": isWindowOpen(exterior_status.rear_right_door),
                "sunroof_open": isWindowOpen(exterior_status.sunroof),
                "tail_gate_open": isWindowOpen(exterior_status.tailgate),
                "hood_open": isWindowOpen(exterior_status.hood),
                "tank_lid_open": isWindowOpen(exterior_status.tank_lid),
            }
            
            # Handle window sensors
            window_sensors = ["front_left_window", "front_right_window", "rear_left_window", "rear_right_window"]
            for window_sensor in window_sensors:
                status = getattr(exterior_status, window_sensor)
                openkey = window_sensor + "_open"
                ajarkey = window_sensor + "_open_ajar"
                if status == OpenStatus.OPEN_STATUS_OPEN:
                    data[openkey] = True
                    data[ajarkey] = False
                elif status == OpenStatus.OPEN_STATUS_AJAR:
                    data[openkey] = True
                    data[ajarkey] = True
                else:
                    data[openkey] = False
                    data[ajarkey] = False
            
            # Set attributes from data dict
            for key, value in data.items():
                setattr(self, key, value)
            
            # Cache successful data
            self._save_to_cache("exterior", data)
            
        except Exception as err:
            _LOGGER.error(
                "Failed to parse exterior for %s: %s",
                vehicle_log_ref(self.vin),
                redact_sensitive(err),
            )
            self._data_source_status["exterior"] = False
            # Try to restore from cache
            if not self._restore_from_cache("exterior"):
                _LOGGER.warning(
                    "No cache available for exterior data on %s",
                    vehicle_log_ref(self.vin),
                )
            return

    async def _parse_health(self):
        try:
            health_resp: GetHealthResp = await self._api.get_health(self.vin)
            _require_message_field(
                health_resp, "data", "HealthService.GetHealth"
            )
            health_status: HealthStatus = health_resp.data

            # Build data dict
            data = {
                "service_warning_msg": health_status.service_warning,
                "service_warning": health_status.service_warning > 1,
                "brake_fluid_level_warning": health_status.brake_fluid_level_warning > 1,
                "engine_coolant_level_warning": health_status.engine_coolant_level_warning > 1,
                "oil_level_warning": health_status.oil_level_warning > 1,
                "washer_fluid_level_warning": health_status.washer_fluid_level_warning > 1,
                "front_left_tyre_pressure_warning": health_status.front_left_tyre_pressure_warning > 1,
                "front_right_tyre_pressure_warning": health_status.front_right_tyre_pressure_warning > 1,
                "rear_left_tyre_pressure_warning": health_status.rear_left_tyre_pressure_warning > 1,
                "rear_right_tyre_pressure_warning": health_status.rear_right_tyre_pressure_warning > 1,
            }
            
            # Set attributes
            for key, value in data.items():
                setattr(self, key, value)
            
            # Cache successful data
            self._save_to_cache("health", data)

        except Exception as err:
            _LOGGER.error(
                "Failed to parse health for %s: %s",
                vehicle_log_ref(self.vin),
                redact_sensitive(err),
            )
            self._data_source_status["health"] = False
            if not self._restore_from_cache("health"):
                _LOGGER.warning(
                    "No cache available for health data on %s",
                    vehicle_log_ref(self.vin),
                )
            return

    async def _parse_fuel(self):
        try:
            fuel_resp: GetFuelResp = await self._api.get_fuel_status(self.vin)
            _require_message_field(fuel_resp, "data", "FuelService.GetFuel")
            fuel_data = fuel_resp.data
            
            # Build data dict
            data = {
                "fuel_amount": round(fuel_data.fuelAmount, 2),
                "distance_to_empty": fuel_data.distanceToEmptyKm,
                "fuel_average_consumption_liters_per_100_km": fuel_data.TMFuelAvgConsum,
                "tm_fuel_consumption": fuel_data.TMFuelAvgConsum,
                "ta_fuel_consumption": fuel_data.ATFuleAvgConsum,
            }
            
            # Set attributes
            for key, value in data.items():
                setattr(self, key, value)
            
            # Cache successful data
            self._save_to_cache("fuel", data)
            
        except Exception as err:
            _LOGGER.error(
                "Failed to parse fuel for %s: %s",
                vehicle_log_ref(self.vin),
                redact_sensitive(err),
            )
            self._data_source_status["fuel"] = False
            if not self._restore_from_cache("fuel"):
                _LOGGER.warning(
                    "No cache available for fuel data on %s",
                    vehicle_log_ref(self.vin),
                )
            return

    async def _parse_battery(
        self, *, refresh_battery: bool = True, refresh_pile: bool = True
    ):
        """Collect battery and home-pile snapshots as independent sources."""
        battery_ok = False
        pile_ok = False
        pile_has_data = False
        battery_available = False

        if refresh_battery:
            try:
                battery_resp = await self._api.get_battery_status(self.vin)
                _require_message_field(
                    battery_resp, "battery", "BatteryService.GetBattery"
                )
                battery = battery_resp.battery
                battery_level = _to_float(
                    battery.batteryChargeLevelPercentage
                )
                charging_power_watts = _to_float(
                    battery.chargingPowerWatts
                )
                if (
                    battery_level is None
                    or not 0 <= battery_level <= 100
                    or charging_power_watts is None
                ):
                    raise VolvoAPIError(
                        "BatteryService returned invalid numeric telemetry"
                    )
                battery_data = {
                    "battery_charge_level_percentage": round(
                        battery_level, 1
                    ),
                    "electric_range": _to_float(
                        battery.estimatedDistanceToEmptyKm
                    ),
                    "tm_energy_consumption": _to_float(
                        battery.averageEnergyConsumptionKwhPer100Km
                    ),
                    "battery_charging_status": (
                        "charging"
                        if charging_power_watts > 0
                        else BATTERY_CHARGING_STATUS.get(
                            battery.chargingStatus, "unknown"
                        )
                    ),
                    "charger_connection_status": (
                        BATTERY_CONNECTION_STATUS.get(
                            battery.chargerConnectionStatus, "unknown"
                        )
                    ),
                    "estimated_charging_time": (
                        battery.estimatedChargingTimeToFullMinutes or None
                    ),
                    "charging_power": round(
                        charging_power_watts / 1000, 2
                    ),
                }
                for key, value in battery_data.items():
                    setattr(self, key, value)
                self._save_to_cache("battery", battery_data)
                battery_ok = True
                battery_available = True
            except Exception as grpc_error:
                _LOGGER.debug(
                    "BatteryService unavailable for %s: %s",
                    vehicle_log_ref(self.vin),
                    redact_sensitive(grpc_error),
                )
                self._data_source_status["battery"] = False
                battery_available = self._restore_from_cache("battery")
        else:
            # A source in backoff keeps its bounded last-good snapshot but must
            # not advance generation or be reported as a successful attempt.
            battery_available = self._restore_from_cache("battery")

        if refresh_pile:
            try:
                # This is a complete pile-owned snapshot. A successful empty
                # response/order list deliberately clears a previously linked
                # pile, ended session, or old order.
                pile_data = {
                    "charge_trade_no": None,
                    "home_charge_status": None,
                    "has_home_charge_pile": False,
                    "plug_and_charge_enabled": None,
                    "last_charge_order": None,
                    "charge_pile_name": None,
                    "charge_pile_address": None,
                    "charging_voltage": None,
                    "charging_current": None,
                    "charging_session_energy": None,
                }
                payload = await self._api.get_charge_pile_status(
                    self.vin, self.series_code
                )
                if payload is not None:
                    pile_has_data = True
                    pile = payload["pile"]
                    status = payload["status"]
                    connector_status = _to_int(
                        status.get("connectorStatus")
                    )
                    if connector_status is None:
                        connector_status = _to_int(
                            pile.get("connectorStatus")
                        )
                    charging_status = _to_int(
                        status.get("startChargeSeqStat")
                    )
                    charging_power = _to_float(status.get("power"))
                    trade_no = (
                        pile.get("tradeNo") or status.get("startChargeSeq")
                    )
                    is_charging = (
                        connector_status == 3
                        or charging_status in (1, 2)
                        or (
                            charging_power is not None
                            and charging_power > 0
                        )
                    )

                    if charging_status is not None:
                        home_charge_status = PILE_CHARGING_STATUS.get(
                            charging_status, "unknown"
                        )
                    elif is_charging:
                        home_charge_status = "charging"
                    elif connector_status in (1, 2):
                        home_charge_status = "idle"
                    else:
                        home_charge_status = "unknown"

                    charging_state = (
                        "charging" if is_charging else home_charge_status
                    )
                    active_trade_no = (
                        trade_no
                        if home_charge_status
                        in {"starting", "charging", "stopping"}
                        else None
                    )
                    plug_and_charge = _to_int(
                        pile.get("plugAndChargeEnabled")
                    )
                    pile_data.update(
                        {
                            "charge_trade_no": active_trade_no,
                            "home_charge_status": home_charge_status,
                            "has_home_charge_pile": True,
                            "plug_and_charge_enabled": (
                                None
                                if plug_and_charge is None
                                else bool(plug_and_charge)
                            ),
                            "charge_pile_name": (
                                pile.get("equipmentName")
                                or pile.get("equipmentUserName")
                            ),
                            "charge_pile_address": pile.get("address"),
                        }
                    )

                    # An idle home pile says nothing about a car charging at a
                    # public charger, so a valid BatteryService snapshot wins.
                    if trade_no or is_charging or not battery_available:
                        session_energy = _to_float(
                            status.get("totalPower")
                        )
                        if session_energy is None:
                            session_energy = _to_float(
                                pile.get("chargeUsePower")
                            )
                        pile_data.update(
                            {
                                "battery_charging_status": charging_state,
                                "charger_connection_status": (
                                    PILE_CONNECTION_STATUS.get(
                                        connector_status, "unknown"
                                    )
                                ),
                                "estimated_charging_time": _to_int(
                                    status.get("estimatedChargingTime")
                                ),
                                "charging_power": charging_power,
                                "charging_voltage": _to_float(
                                    status.get("voltageA")
                                ),
                                "charging_current": _to_float(
                                    status.get("currentA")
                                ),
                                "charging_session_energy": session_energy,
                            }
                        )

                try:
                    orders = await self._api.get_charge_order_list(
                        vin=self.vin, series_code=self.series_code
                    )
                    if orders:
                        latest = orders[0]
                        pile_data["last_charge_order"] = {
                            "order_no": latest.get("orderNo"),
                            "start_time": latest.get("startTime"),
                            "end_time": latest.get("endTime"),
                            "duration": latest.get("chargeUseTime"),
                            "energy_kwh": _to_float(
                                latest.get("chargeUsePower")
                            ),
                            "stop_reason": latest.get("stopFailReason"),
                        }
                except Exception as order_error:
                    # The pile status is still valid. Retain the previous order
                    # only when the order endpoint itself failed.
                    pile_data["last_charge_order"] = self.last_charge_order
                    _LOGGER.debug(
                        "Charge order history unavailable for %s: %s",
                        vehicle_log_ref(self.vin),
                        redact_sensitive(order_error),
                    )

                for key, value in pile_data.items():
                    setattr(self, key, value)
                self._save_to_cache("charge_pile", pile_data)
                pile_ok = True
            except Exception as pile_error:
                _LOGGER.debug(
                    "Home charge-pile data unavailable for %s: %s",
                    vehicle_log_ref(self.vin),
                    redact_sensitive(pile_error),
                )
                self._data_source_status["charge_pile"] = False
                # Fresh BatteryService charging values must not be overwritten
                # by an older active-session cache from the pile.
                restore_keys = (
                    set(CHARGE_PILE_EXCLUSIVE_FIELDS)
                    if battery_available
                    else None
                )
                self._restore_from_cache(
                    "charge_pile", keys=restore_keys
                )
        else:
            # Maintain/expire pile-owned metadata while this source is backed
            # off. BatteryService remains authoritative for shared fields.
            restore_keys = (
                set(CHARGE_PILE_EXCLUSIVE_FIELDS)
                if battery_available
                else None
            )
            self._restore_from_cache("charge_pile", keys=restore_keys)

        if battery_available and pile_ok and pile_has_data:
            self.charge_data_source = "grpc_battery+charge_pile_api"
        elif battery_available:
            self.charge_data_source = "grpc_battery"
        elif pile_ok and pile_has_data:
            self.charge_data_source = "charge_pile_api"
        elif battery_ok or pile_ok:
            self.charge_data_source = None

    async def _parse_odometer(self):
        try:
            odometer_resp: GetOdometerResp = await self._api.get_odometer(self.vin)
            _require_message_field(
                odometer_resp, "data", "OdometerService.GetOdometer"
            )
            odometer_data = odometer_resp.data
            
            # Build data dict
            data = {
                "odo_meter": odometer_data.odometerMeters / 1000,
                "tm_distance": odometer_data.tripMeterManualKm,
                "tm_average_speed": odometer_data.averageSpeedKmPerHour,
                "ta_distance": odometer_data.tripMeterAutomaticKm,
                "ta_average_speed": (
                    odometer_data.averageSpeedKmPerHourAutomatic
                ),
            }
            
            # Set attributes
            for key, value in data.items():
                setattr(self, key, value)
            
            # Cache successful data
            self._save_to_cache("odometer", data)
            
        except Exception as err:
            _LOGGER.error(
                "Failed to parse odometer for %s: %s",
                vehicle_log_ref(self.vin),
                redact_sensitive(err),
            )
            self._data_source_status["odometer"] = False
            if not self._restore_from_cache("odometer"):
                _LOGGER.warning(
                    "No cache available for odometer data on %s",
                    vehicle_log_ref(self.vin),
                )
            return

    async def _parse_availability(self):
        try:
            availability_resp: GetAvailabilityResp = await self._api.get_availability(self.vin)
            _require_message_field(
                availability_resp,
                "data",
                "AvailabilityService.GetAvailability",
            )
            availability_data = availability_resp.data
            
            # Build data dict
            data = {
                "availability_status": availability_data.availableStatus,
                "unavailable_reason": availability_data.unavailableReason,
                "engine_running": (availability_data.availableStatus == AvailabilityStatus.Unavailable 
                                 and availability_data.unavailableReason == AvailabilityReason.CarInUse),
            }
            
            # Set attributes
            for key, value in data.items():
                setattr(self, key, value)
            
            # Cache successful data
            self._save_to_cache("availability", data)
            
        except Exception as err:
            _LOGGER.error(
                "Failed to parse availability for %s: %s",
                vehicle_log_ref(self.vin),
                redact_sensitive(err),
            )
            self._data_source_status["availability"] = False
            if not self._restore_from_cache("availability"):
                _LOGGER.warning(
                    "No cache available for availability data on %s",
                    vehicle_log_ref(self.vin),
                )
            return

    async def _parse_location(self):
        try:
            location_resp: StreamLastKnownLocationsResp = await self._api.get_location(self.vin)

            latitude = location_resp.latitude
            longitude = location_resp.longitude

            # An all-zero response is "no fix", not a position off West
            # Africa. Feeding it through gcj02towgs84 would even produce a
            # plausible-looking non-zero pair, so it has to be caught here.
            if not latitude and not longitude:
                data = {
                    "position": {"latitude": None, "longitude": None},
                    "position_wgs84": {"latitude": None, "longitude": None},
                }
            else:
                wgs84_coords = gcj02towgs84(longitude, latitude)
                data = {
                    "position": {
                        "latitude": latitude,
                        "longitude": longitude,
                    },
                    "position_wgs84": {
                        "longitude": wgs84_coords[0],
                        "latitude": wgs84_coords[1],
                    },
                }


            # Set attributes
            for key, value in data.items():
                setattr(self, key, value)
            
            # Cache successful data
            self._save_to_cache("location", data)
            
        except Exception as err:
            _LOGGER.error(
                "Failed to parse location for %s: %s",
                vehicle_log_ref(self.vin),
                redact_sensitive(err),
            )
            self._data_source_status["location"] = False
            if not self._restore_from_cache("location"):
                _LOGGER.warning(
                    "No cache available for location data on %s",
                    vehicle_log_ref(self.vin),
                )
            return

    async def _parse_engine_status(self):
        try:
            if not self.isAaos:
                return
            
            engine_status_resp: GetEngineRemoteStartResp = await self._api.get_engine_status(self.vin)
            _require_message_field(
                engine_status_resp,
                "data",
                "EngineRemoteStartService.GetEngineRemoteStart",
            )
            engine_status = engine_status_resp.data
            
            # Build data dict
            data = {
                "engine_remote_running": engine_status.engineRunningStatus in (
                    EngineRunningStatus.Starting,
                    EngineRunningStatus.Running,
                ),
                "engine_remote_start_time": engine_status.engineStartTime.seconds,
                "engine_remote_end_time": engine_status.engineEndTime.seconds,
            }
            
            # Set attributes
            for key, value in data.items():
                setattr(self, key, value)
            
            # Cache successful data
            self._save_to_cache("engine_status", data)
            
        except Exception as err:
            _LOGGER.error(
                "Failed to parse engine status for %s: %s",
                vehicle_log_ref(self.vin),
                redact_sensitive(err),
            )
            self._data_source_status["engine_status"] = False
            if not self._restore_from_cache("engine_status"):
                _LOGGER.warning(
                    "No cache available for engine status data on %s",
                    vehicle_log_ref(self.vin),
                )
            return

    async def _parse_car_preference(self):
        try:
            preference_resp: GetPreferencesResp = await self._api.get_car_preferences(self.vin)
            _require_message_field(
                preference_resp,
                "preference",
                "CarPreferences.GetPreferences",
            )
            
            # Build data dict
            data = {
                "nickname": preference_resp.preference.nickName,
            }
            
            # Set attributes
            for key, value in data.items():
                setattr(self, key, value)
            
            # Cache successful data
            self._save_to_cache("preference", data)
            
        except Exception as err:
            _LOGGER.error(
                "Failed to parse car preference for %s: %s",
                vehicle_log_ref(self.vin),
                redact_sensitive(err),
            )
            self._data_source_status["preference"] = False
            if not self._restore_from_cache("preference"):
                _LOGGER.warning(
                    "No cache available for preference data on %s",
                    vehicle_log_ref(self.vin),
                )
            return
    def apply_vehicle_info(self, vehicle_info):
        """Seed the static model fields from an account listing entry.

        The coordinator already fetched listBindCar for this poll, so taking
        the names from there avoids a second identical request on first sight
        of the vehicle.
        """
        if not vehicle_info:
            return
        self.series_name = vehicle_info.get("seriesName") or self.series_name
        self.model_name = vehicle_info.get("modelName") or self.model_name

    def _should_refresh(self, source):
        """Return True when this source is due on the current poll cycle."""
        retry_after = self._source_retry_after.get(source)
        if retry_after is not None:
            if time.monotonic() < retry_after:
                return False
            self._source_retry_after.pop(source, None)

        every = SOURCE_REFRESH_EVERY_CYCLES.get(source)
        if every is None:
            return True
        # A cadence only applies to healthy cached data. If the latest call
        # failed, retry on the next cycle instead of leaving (for example)
        # a failed preference source stale for another 239 polls.
        if self._data_source_status.get(source) is False:
            return True
        # Always run on the first cycle so entities are populated at setup.
        if source not in self._cache:
            return True
        if self._poll_cycle % every == 0:
            return True
        refreshed = self._source_refreshed_monotonic.get(source)
        return refreshed is not None and (
            time.monotonic() - refreshed
            >= every
            * SOURCE_REFRESH_NOMINAL_CYCLE_SECONDS
            * SOURCE_REFRESH_TIME_CEILING_FACTOR
        )

    async def update(self):
        await self._api.get_channel()
        # Sources that can change at any moment run every cycle; slow-moving
        # ones are throttled by SOURCE_REFRESH_EVERY_CYCLES.
        candidates = [
            ("exterior", self._parse_exterior),
            ("odometer", self._parse_odometer),
            ("fuel", self._parse_fuel),
            ("availability", self._parse_availability),
            ("location", self._parse_location),
            ("health", self._parse_health),
            ("preference", self._parse_car_preference),
        ]
        if self.isAaos:
            candidates.append(("engine_status", self._parse_engine_status))

        attempted_sources = []
        generation_before = {}
        scheduled_calls = []
        for source, runf in candidates:
            if not self._should_refresh(source):
                continue
            attempted_sources.append(source)
            generation_before[source] = self._data_source_generation[source]
            scheduled_calls.append(runf)

        if self.supports_electric:
            refresh_battery = self._should_refresh("battery")
            refresh_pile = self._should_refresh("charge_pile")
            if refresh_battery or refresh_pile:
                for source, refresh in (
                    ("battery", refresh_battery),
                    ("charge_pile", refresh_pile),
                ):
                    if refresh:
                        attempted_sources.append(source)
                        generation_before[source] = (
                            self._data_source_generation[source]
                        )

                async def refresh_electric_sources():
                    await self._parse_battery(
                        refresh_battery=refresh_battery,
                        refresh_pile=refresh_pile,
                    )

                scheduled_calls.append(refresh_electric_sources)

        async with asyncio.TaskGroup() as tg:
            for runf in scheduled_calls:
                tg.create_task(runf())

        successful_sources = []
        for source in attempted_sources:
            if (
                self._data_source_generation[source]
                > generation_before[source]
            ):
                successful_sources.append(source)
                self._record_source_success(source)
            else:
                self._record_source_failure(source)

        self._poll_cycle += 1
        if attempted_sources and not successful_sources:
            raise VolvoAPIError(
                "No attempted vehicle data source refreshed successfully"
            )

        applicable_sources = set(self._data_source_status)
        if not self.isAaos:
            applicable_sources.discard("engine_status")
        if (
            not attempted_sources
            and applicable_sources
            and all(
                self._data_source_status[source] is False
                for source in applicable_sources
            )
        ):
            raise VolvoAPIError(
                "All vehicle data sources are waiting for failure backoff"
            )

    async def lock_window(self):
        await self._api.window_control(self.vin, invocationControlType.CLOSE)

    async def unlock_window(self):
        await self._api.window_control(self.vin, invocationControlType.OPEN)

    async def lock_vehicle(self):
        await self._api.door_lock(self.vin)

    async def unlock_vehicle(self):
        await self._api.door_unlock(self.vin, UnlockType.UNLOCK_UNSPECIFIED)

    async def unlock_vehicle_trunk_only(self):
        await self._api.door_unlock(self.vin, UnlockType.TRUNK_ONLY)

    async def flash(self):
        await self._api.honk_flash_control(self.vin, HonkFlashType.FLASH)

    async def honk_and_flash(self):
        await self._api.honk_flash_control(self.vin, HonkFlashType.HONK_AND_FLASH)

    async def honk(self):
        await self._api.honk_flash_control(self.vin, HonkFlashType.HONK)

    async def engine_start(self, duration):
        await self._api.engine_control(self.vin, True, duration)

    async def engine_stop(self):
        await self._api.engine_control(self.vin, False, 0)

    async def climatization_start(self):
        await self._api.climatization_control(self.vin, True)

    async def climatization_stop(self):
        await self._api.climatization_control(self.vin, False)

    async def start_home_charge(self):
        result = await self._api.start_charge_pile(
            self.vin, self.series_code
        )
        if result and result.get("startChargeSeq"):
            self.charge_trade_no = result["startChargeSeq"]
            self.home_charge_status = "starting"
        return result

    async def stop_home_charge(self):
        # Always re-read the live pile order so a stale cached trade number
        # can never target an already-ended session.
        trade_no = await self._api.get_active_trade_no(
            self.vin,
            self.series_code,
            force_refresh=True,
        )
        if not trade_no:
            raise HomeAssistantError(
                "No active home-charge session to stop"
            )
        result = await self._api.stop_charge_pile(
            trade_no, self.vin, self.series_code
        )
        self.charge_trade_no = None
        self.home_charge_status = "stopping"
        return result

    async def set_plug_and_charge(self, enabled: bool):
        return await self._api.set_plug_and_charge(
            enabled, self.vin, self.series_code
        )

    def get(self, key):
        if not hasattr(self, key):
            raise Exception(f"{key} not found")
        return getattr(self, key)

    async def tail_gate_control_open(self):
        await self._api.tailgate_contorl(self.vin, invocationControlType.OPEN)

    async def tail_gate_control_close(self):
        await self._api.tailgate_contorl(self.vin, invocationControlType.CLOSE)

    async def sunroof_control_open(self):
        await self._api.sunroof_contorl(self.vin, invocationControlType.OPEN)

    async def sunroof_control_close(self):
        await self._api.sunroof_contorl(self.vin, invocationControlType.CLOSE)

    async def sign_in(self):
        return await self._api.sign_in(self.vin, self.series_code)


def _to_float(value):
    """Convert an API value to float while preserving missing values."""
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _to_int(value):
    """Convert an API value to int while preserving missing values."""
    number = _to_float(value)
    return int(number) if number is not None else None
