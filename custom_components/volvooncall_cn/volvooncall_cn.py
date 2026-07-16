import logging
import datetime
import grpc
import asyncio
from datetime import datetime as dt, timedelta, timezone
from typing import Dict, Any, Optional
import copy
from .volvooncall_base import (
    VehicleBaseAPI,
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
from .proto.invocation_pb2 import invocationHead, invocationStatus, invocationControlType, invocationCommResp
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
from .proto.car_preferences_pb2 import UpdatePreferencesReq, UpdatePreferencesResp, Preference
from .proto.battery_pb2_grpc import BatteryServiceStub
from .proto.battery_pb2 import GetBatteryRequest, GetBatteryResponse


_LOGGER = logging.getLogger(__name__)

GRPC_DIGITALVOLVO_HOST = "cepmobtoken.prod.c3.volvocars.com.cn:443"
GRPC_LBS_VOLVO_HOST = "cepmobtoken.lbs.prod.c3.volvocars.com.cn:443"
USER_AGENT = "vca-android/5.53.1 grpc-java-okhttp/1.68.0"
MAX_RETRIES = 1
TIMEOUT = datetime.timedelta(seconds=10)
DOMAIN = "volvooncall_cn"

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
        callCreds = grpc.metadata_call_credentials(self._metadata_callback)
        sslCreds = grpc.ssl_channel_credentials()
        creds = grpc.composite_channel_credentials(sslCreds, callCreds)
        channel_options: tuple = (("grpc.primary_user_agent", USER_AGENT), ('grpc.accept_encoding', 'gzip'),)
        channel = grpc.secure_channel(target, creds, options=channel_options)
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

    def raise_invocation_fail(self, status):
        if status in [invocationStatus.SUCCESS, invocationStatus.SENT, invocationStatus.DELIVERED]:
            return
        if status == invocationStatus.CAR_OFFLINE:
            raise Exception("车辆离线或无网络")
        elif status in [invocationStatus.DELIVERY_TIMEOUT, invocationStatus.RESPONSE_TIMEOUT]:
            raise Exception("请求超时")
        elif status == invocationStatus.UNKNOWN_CAR_ERROR:
            raise Exception("车辆未知错误")
        elif status == invocationStatus.NOT_ALLOWED_PRIVACY_ENABLED:
            raise Exception("车辆隐私协议未同意")
        elif status == invocationStatus.NOT_ALLOWED_WRONG_USAGE_MODE:
            raise Exception("请求模式错误")
        elif status == invocationStatus.NOT_ALLOWED_CONFLICTING_INVOCATION:
            raise Exception("请求操作存在冲突")
        else:
            raise Exception("未知错误")

    async def get_fuel_status(self, vin) -> GetFuelResp:
        stub = FuelServiceStub(await self.get_channel())
        req = GetFuelReq(vin=vin)
        metadata: list = [("vin", vin)]
        res = GetFuelResp()
        for res in stub.GetFuel(req, metadata=metadata, timeout=TIMEOUT.seconds):
            break
        return res

    async def get_exterior(self, vin) -> GetExteriorResp:
        stub = ExteriorServiceStub(await self.get_channel())
        req = GetExteriorReq(vin=vin)
        metadata: list = [("vin", vin)]
        res = GetExteriorResp()
        for res in stub.GetExterior(req, metadata=metadata, timeout=TIMEOUT.seconds):
            break
        return res

    async def get_health(self, vin) -> GetHealthResp:
        stub = HealthServiceStub(await self.get_channel())
        req = GetHealthReq(vin=vin)
        metadata: list = [("vin", vin)]
        res = GetHealthResp()
        for res in stub.GetHealth(req, metadata=metadata, timeout=TIMEOUT.seconds):
            break
        return res

    async def get_odometer(self, vin) -> GetOdometerResp:
        stub = OdometerServiceStub(await self.get_channel())
        req = GetOdometerReq(vin=vin)
        metadata: list = [("vin", vin)]
        res = GetOdometerResp()
        for res in stub.GetOdometer(req, metadata=metadata, timeout=TIMEOUT.seconds):
            break
        return res

    async def get_availability(self, vin) -> GetAvailabilityResp:
        stub = AvailabilityServiceStub(await self.get_channel())
        req = GetAvailabilityReq(vin=vin)
        metadata: list = [("vin", vin)]
        res = GetAvailabilityResp()
        for res in stub.GetAvailability(req, metadata=metadata, timeout=TIMEOUT.seconds):
            break
        return res

    async def window_control(self, vin, opentype):
        stub = InvocationServiceStub(await self.get_channel())
        req_header = invocationHead(vin=vin)
        req = windowControlReq(head=req_header, openType=opentype)
        metadata: list = [("vin", vin)]
        res: invocationCommResp = invocationCommResp()
        for res in stub.WindowControl(req, metadata=metadata, timeout=TIMEOUT.seconds):
            self.raise_invocation_fail(res.data.status)
            break
        return

    async def get_location(self, vin) -> StreamLastKnownLocationsResp:
        stub = DtlInternetServiceStub(await self.get_lbs_channel())
        req = StreamLastKnownLocationsReq(vin=vin)
        metadata: list = [("vin", vin)]
        res: StreamLastKnownLocationsResp = StreamLastKnownLocationsResp()
        for res in stub.StreamLastKnownLocations(req, metadata=metadata, timeout=TIMEOUT.seconds):
            break
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
        res: invocationCommResp = invocationCommResp()
        for res in stub.EngineStart(req, metadata=metadata, timeout=TIMEOUT.seconds):
            self.raise_invocation_fail(res.data.status)
            break
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
                req, metadata=metadata, timeout=TIMEOUT.seconds
            )
        else:
            req = ClimatizationStopReq(head=req_header)
            responses = stub.ClimatizationStop(
                req, metadata=metadata, timeout=TIMEOUT.seconds
            )

        for res in responses:
            self.raise_invocation_fail(res.data.status)
            break

    async def honk_flash_control(self, vin, honk_flash_type: HonkFlashType):
        stub = InvocationServiceStub(await self.get_channel())
        req_header = invocationHead(vin=vin)
        req = HonkFlashReq(head=req_header, honkFlashType=honk_flash_type)
        metadata: list = [("vin", vin)]
        res: invocationCommResp = invocationCommResp()
        for res in stub.HonkFlash(req, metadata=metadata, timeout=TIMEOUT.seconds):
            self.raise_invocation_fail(res.data.status)
            break
        return

    async def door_lock(self, vin):
        stub = InvocationServiceStub(await self.get_channel())
        req_header = invocationHead(vin=vin)
        req = LockReq(head=req_header, lockType=LockType.LOCK_REDUCED_GUARD)
        metadata: list = [("vin", vin)]
        res: invocationCommResp = invocationCommResp()
        for res in stub.Lock(req, metadata=metadata, timeout=TIMEOUT.seconds):
            self.raise_invocation_fail(res.data.status)
            break
        return

    async def door_unlock(self, vin, unlockType):
        stub = InvocationServiceStub(await self.get_channel())
        req_header = invocationHead(vin=vin)
        req = UnlockReq(head=req_header)
        if unlockType != UnlockType.UNLOCK_UNSPECIFIED:
            req = UnlockReq(head=req_header, unlockType=unlockType)
        metadata: list = [("vin", vin)]
        res: invocationCommResp = invocationCommResp()
        for res in stub.Unlock(req, metadata=metadata, timeout=TIMEOUT.seconds):
            self.raise_invocation_fail(res.data.status)
            break
        return

    async def get_engine_status(self, vin):
        stub = EngineRemoteStartServiceStub(await self.get_channel())
        req = GetEngineRemoteStartReq(vin=vin)
        metadata: list = [("vin", vin)]
        res: GetEngineRemoteStartResp = GetEngineRemoteStartResp()
        for res in stub.GetEngineRemoteStart(req, metadata=metadata, timeout=TIMEOUT.seconds):
            break
        return res

    async def sunroof_contorl(self, vin: str, controlType: invocationControlType):
        stub = InvocationServiceStub(await self.get_channel())
        req_header = invocationHead(vin=vin)
        req = SunroofControlReq(head=req_header, type=controlType)
        metadata: list = [("vin", vin)]
        res: invocationCommResp = invocationCommResp()
        for res in stub.SunroofControl(req, metadata=metadata, timeout=TIMEOUT.seconds):
            self.raise_invocation_fail(res.data.status)
            break
        return

    async def tailgate_contorl(self, vin: str, controlType: invocationControlType):
        stub = InvocationServiceStub(await self.get_channel())
        req_header = invocationHead(vin=vin)
        req = TailgateControlReq(head=req_header, type=controlType)
        metadata: list = [("vin", vin)]
        res: invocationCommResp = invocationCommResp()
        for res in stub.TailgateControl(req, metadata=metadata, timeout=TIMEOUT.seconds):
            self.raise_invocation_fail(res.data.status)
            break
        return

    async def update_status(self, vin: str):
        stub = InvocationServiceStub(await self.get_channel())
        req_header = invocationHead(vin=vin)
        req = UpdateStatusReq(head=req_header)
        metadata: list = [("vin", vin)]
        res: invocationCommResp = invocationCommResp()
        for res in stub.UpdateStatus(req, metadata=metadata, timeout=TIMEOUT.seconds):
            self.raise_invocation_fail(res.data.status)
            break
        return

    async def get_car_preferences(self, vin: str):
        stub = CarPreferencesStub(await self.get_channel())
        req = GetPreferencesReq(vin=vin)
        metadata: list = [("vin", vin)]
        res: GetPreferencesResp = GetPreferencesResp()
        for res in stub.GetPreferences(req, metadata=metadata, timeout=TIMEOUT.seconds):
            break
        return res

    async def get_battery_status(self, vin: str) -> GetBatteryResponse:
        """Fetch PHEV/BEV battery and charging status."""
        stub = BatteryServiceStub(await self.get_channel())
        req = GetBatteryRequest(vin=vin)
        metadata: list = [("vin", vin)]
        res: GetBatteryResponse = GetBatteryResponse()
        for res in stub.GetLatestBattery(req, metadata=metadata, timeout=TIMEOUT.seconds):
            break
        return res

    async def update_car_preference(self, vin: str, nickname: str):
        stub = CarPreferencesStub(await self.get_channel())
        preference = Preference(nickName=nickname)
        req = UpdatePreferencesReq(vin=vin, preference=preference)
        metadata: list = [("vin", vin)]
        res: UpdatePreferencesResp = UpdatePreferencesResp()
        for res in stub.UpdatePreferences(req, metadata=metadata, timeout=TIMEOUT.seconds):
            break
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
        self.car_locked = False
        self.distance_to_empty = 0  # 续航公里
        self.tail_gate_open = False
        self.rear_right_door_open = False
        self.rear_left_door_open = False
        self.front_right_door_open = False
        self.front_left_door_open = False
        self.hood_open = False
        self.sunroof_open = False
        self.engine_running = False
        self.engine_remote_running = False
        self.odo_meter = 0
        self.front_left_window_open = False
        self.front_right_window_open = False
        self.rear_left_window_open = False
        self.rear_right_window_open = False
        self.front_left_window_open_ajar = False
        self.front_right_window_open_ajar = False
        self.rear_left_window_open_ajar = False
        self.rear_right_window_open_ajar = False
        self.fuel_amount = 0
        self.fuel_average_consumption_liters_per_100_km = 0
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
        self.tank_lid_open = False
        self.availability_status = AvailabilityStatus.Available
        self.unavailable_reason = AvailabilityReason.Unspecified1
        self.engine_remote_start_time = 0
        self.engine_remote_end_time = 0
        # self.fuel_amount_level = 0
        self.position = {
            "longitude": 0.0,
            "latitude": 0.0
        }
        self.position_wgs84 = {
            "longitude": 0.0,
            "latitude": 0.0
        }
        self.service_warning_msg = "1"
        self.service_warning = False
        self.brake_fluid_level_warning = False
        self.engine_coolant_level_warning = False
        self.oil_level_warning = False
        self.washer_fluid_level_warning = False
        self.front_left_tyre_pressure_warning = False
        self.front_right_tyre_pressure_warning = False
        self.rear_left_tyre_pressure_warning = False
        self.rear_right_tyre_pressure_warning = False
        self.nickname = ""

        # Caching infrastructure for resilience
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


    def _save_to_cache(self, source: str, data_dict: Dict[str, Any]):
        """Save successful data to cache."""
        self._cache[source] = copy.deepcopy(data_dict)
        self._cache_timestamp[source] = dt.now(timezone.utc)
        self._last_successful_update = dt.now(timezone.utc)
        self._data_source_status[source] = True
        self._consecutive_failures = 0
        _LOGGER.debug("Cached %s data for %s", source, vehicle_log_ref(self.vin))
    
    def _restore_from_cache(self, source: str) -> bool:
        """Restore data from cache if available and not too old."""
        if source not in self._cache:
            return False
        
        # Check if cache is not too old (1 hour default)
        cache_age = dt.now(timezone.utc) - self._cache_timestamp.get(source, dt.min.replace(tzinfo=timezone.utc))
        if cache_age > timedelta(hours=1):
            _LOGGER.warning(f"Cache for {source} is too old ({cache_age}), not restoring")
            return False
        
        # Restore cached values
        cached_data = self._cache[source]
        for key, value in cached_data.items():
            if hasattr(self, key):
                setattr(self, key, value)
        
        _LOGGER.info(
            "Restored %s from cache (age: %s) for %s",
            source,
            cache_age,
            vehicle_log_ref(self.vin),
        )
        return True
    
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

    async def _parse_battery(self):
        """Collect vehicle battery and home-pile data without mixing sources."""
        # Build a complete snapshot on every poll so ended or failed sessions
        # cannot leave stale current, voltage, order or battery values behind.
        data = {
            "battery_charge_level_percentage": None,
            "electric_range": None,
            "tm_energy_consumption": None,
            "battery_charging_status": None,
            "charger_connection_status": None,
            "estimated_charging_time": None,
            "charging_power": None,
            "charging_voltage": None,
            "charging_current": None,
            "charging_session_energy": None,
            "charge_trade_no": None,
            "home_charge_status": None,
            "has_home_charge_pile": False,
            "plug_and_charge_enabled": None,
            "charge_data_source": None,
            "charge_pile_name": None,
            "charge_pile_address": None,
        }
        battery_ok = False
        pile_ok = False

        try:
            battery_resp = await self._api.get_battery_status(self.vin)
            if not battery_resp.HasField("battery"):
                raise ValueError("BatteryService returned no battery data")

            battery = battery_resp.battery
            charging_power = battery.chargingPowerWatts / 1000
            data.update({
                "battery_charge_level_percentage": round(
                    battery.batteryChargeLevelPercentage, 1
                ),
                "electric_range": battery.estimatedDistanceToEmptyKm,
                "tm_energy_consumption": (
                    battery.averageEnergyConsumptionKwhPer100Km
                ),
                "battery_charging_status": (
                    "charging"
                    if battery.chargingPowerWatts > 0
                    else BATTERY_CHARGING_STATUS.get(
                        battery.chargingStatus, "unknown"
                    )
                ),
                "charger_connection_status": BATTERY_CONNECTION_STATUS.get(
                    battery.chargerConnectionStatus, "unknown"
                ),
                "estimated_charging_time": (
                    battery.estimatedChargingTimeToFullMinutes or None
                ),
                "charging_power": round(charging_power, 2),
                "charge_data_source": "grpc_battery",
                "charge_pile_name": None,
                "charge_pile_address": None,
            })
            battery_ok = True
        except Exception as grpc_error:
            _LOGGER.debug(
                "BatteryService unavailable for %s: %s",
                vehicle_log_ref(self.vin),
                redact_sensitive(grpc_error),
            )

        try:
            payload = await self._api.get_charge_pile_status(
                self.vin, self.series_code
            )
            if payload is None:
                # A successful empty list means this account has no linked
                # home pile; that is a valid configuration, not an API error.
                pile_ok = True
            else:
                pile = payload["pile"]
                status = payload["status"]
                # Only a running session has telemetry; otherwise the pile entry
                # itself is the only source of connector state.
                connector_status = _to_int(status.get("connectorStatus"))
                if connector_status is None:
                    connector_status = _to_int(pile.get("connectorStatus"))
                charging_status = _to_int(status.get("startChargeSeqStat"))
                charging_power = _to_float(status.get("power"))
                trade_no = pile.get("tradeNo") or status.get("startChargeSeq")
                is_charging = (
                    connector_status == 3
                    or charging_status in (1, 2)
                    or (charging_power is not None and charging_power > 0)
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

                data.update({
                    "charge_trade_no": active_trade_no,
                    "home_charge_status": home_charge_status,
                    "has_home_charge_pile": True,
                    "plug_and_charge_enabled": bool(
                        _to_int(pile.get("plugAndChargeEnabled"))
                    ),
                    "charge_data_source": (
                        "grpc_battery+charge_pile_api"
                        if battery_ok
                        else "charge_pile_api"
                    ),
                    "charge_pile_name": (
                        pile.get("equipmentName")
                        or pile.get("equipmentUserName")
                    ),
                    "charge_pile_address": pile.get("address"),
                })

                # An idle home pile says nothing about a car charging elsewhere,
                # so it must not overwrite BatteryService telemetry.
                if trade_no or is_charging or not battery_ok:
                    session_energy = _to_float(status.get("totalPower"))
                    if session_energy is None:
                        session_energy = _to_float(
                            pile.get("chargeUsePower")
                        )
                    data.update({
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
                    })
                pile_ok = True

            try:
                orders = await self._api.get_charge_order_list(
                    vin=self.vin, series_code=self.series_code
                )
                if orders:
                    latest = orders[0]
                    data["last_charge_order"] = {
                        "order_no": latest.get("orderNo"),
                        "start_time": latest.get("startTime"),
                        "end_time": latest.get("endTime"),
                        "duration": latest.get("chargeUseTime"),
                        "energy_kwh": _to_float(latest.get("chargeUsePower")),
                        "stop_reason": latest.get("stopFailReason"),
                    }
            except Exception as order_error:
                _LOGGER.debug(
                    "Charge order history unavailable for %s: %s",
                    vehicle_log_ref(self.vin),
                    redact_sensitive(order_error),
                )
        except Exception as pile_error:
            _LOGGER.debug(
                "Home charge-pile data unavailable for %s: %s",
                vehicle_log_ref(self.vin),
                redact_sensitive(pile_error),
            )

        if not battery_ok and not pile_ok:
            self._data_source_status["battery"] = False
            self._data_source_status["charge_pile"] = False
            self._restore_from_cache("battery")
            return

        for key, value in data.items():
            setattr(self, key, value)
        self._save_to_cache("battery", data)
        self._data_source_status["battery"] = battery_ok
        self._data_source_status["charge_pile"] = pile_ok

    async def _parse_odometer(self):
        try:
            odometer_resp: GetOdometerResp = await self._api.get_odometer(self.vin)
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
            
            # Build data dict
            data = {
                "position": {
                    "latitude": location_resp.latitude,
                    "longitude": location_resp.longitude,
                },
            }
            
            # Calculate WGS84 coordinates
            wgs84_coords = gcj02towgs84(location_resp.longitude, location_resp.latitude)
            data["position_wgs84"] = {
                "longitude": wgs84_coords[0],
                "latitude": wgs84_coords[1],
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
    async def update(self):
        if not self.series_name:
            vehicles = await self._api.get_vehicles()
            for vehicle in vehicles:
                if vehicle["vinCode"] == self.vin:
                    self.series_name = vehicle["seriesName"]
                    self.model_name = vehicle["modelName"]

        tasks = []
        await self._api.get_channel()
        async with asyncio.TaskGroup() as tg:
            funcs = [self._parse_exterior, self._parse_odometer,
                     self._parse_fuel, self._parse_availability,
                     self._parse_location, self._parse_engine_status,
                     self._parse_health, self._parse_car_preference]
            if self.supports_electric:
                funcs.append(self._parse_battery)
            for runf in funcs:
                task = tg.create_task(runf())
                tasks.append(task)
        for task in tasks:
            task.result()

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
            raise Exception("No active home-charge session to stop")
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
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value):
    """Convert an API value to int while preserving missing values."""
    number = _to_float(value)
    return int(number) if number is not None else None
