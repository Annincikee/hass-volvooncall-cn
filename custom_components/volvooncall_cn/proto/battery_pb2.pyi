from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class GetBatteryRequest(_message.Message):
    __slots__ = ("id", "vin")
    ID_FIELD_NUMBER: _ClassVar[int]
    VIN_FIELD_NUMBER: _ClassVar[int]
    id: str
    vin: str
    def __init__(self, id: _Optional[str] = ..., vin: _Optional[str] = ...) -> None: ...

class Timestamp(_message.Message):
    __slots__ = ("seconds", "nanos")
    SECONDS_FIELD_NUMBER: _ClassVar[int]
    NANOS_FIELD_NUMBER: _ClassVar[int]
    seconds: int
    nanos: int
    def __init__(self, seconds: _Optional[int] = ..., nanos: _Optional[int] = ...) -> None: ...

class Battery(_message.Message):
    __slots__ = ("updateTime", "batteryChargeLevelPercentage", "averageEnergyConsumptionKwhPer100Km", "estimatedDistanceToEmptyKm", "estimatedChargingTimeToFullMinutes", "chargerConnectionStatus", "chargingStatus", "estimatedDistanceToEmptyMiles", "chargingPowerWatts")
    UPDATETIME_FIELD_NUMBER: _ClassVar[int]
    BATTERYCHARGELEVELPERCENTAGE_FIELD_NUMBER: _ClassVar[int]
    AVERAGEENERGYCONSUMPTIONKWHPER100KM_FIELD_NUMBER: _ClassVar[int]
    ESTIMATEDDISTANCETOEMPTYKM_FIELD_NUMBER: _ClassVar[int]
    ESTIMATEDCHARGINGTIMETOFULLMINUTES_FIELD_NUMBER: _ClassVar[int]
    CHARGERCONNECTIONSTATUS_FIELD_NUMBER: _ClassVar[int]
    CHARGINGSTATUS_FIELD_NUMBER: _ClassVar[int]
    ESTIMATEDDISTANCETOEMPTYMILES_FIELD_NUMBER: _ClassVar[int]
    CHARGINGPOWERWATTS_FIELD_NUMBER: _ClassVar[int]
    updateTime: Timestamp
    batteryChargeLevelPercentage: float
    averageEnergyConsumptionKwhPer100Km: float
    estimatedDistanceToEmptyKm: int
    estimatedChargingTimeToFullMinutes: int
    chargerConnectionStatus: int
    chargingStatus: int
    estimatedDistanceToEmptyMiles: int
    chargingPowerWatts: int
    def __init__(self, updateTime: _Optional[_Union[Timestamp, _Mapping]] = ..., batteryChargeLevelPercentage: _Optional[float] = ..., averageEnergyConsumptionKwhPer100Km: _Optional[float] = ..., estimatedDistanceToEmptyKm: _Optional[int] = ..., estimatedChargingTimeToFullMinutes: _Optional[int] = ..., chargerConnectionStatus: _Optional[int] = ..., chargingStatus: _Optional[int] = ..., estimatedDistanceToEmptyMiles: _Optional[int] = ..., chargingPowerWatts: _Optional[int] = ...) -> None: ...

class GetBatteryResponse(_message.Message):
    __slots__ = ("id", "vin", "battery")
    ID_FIELD_NUMBER: _ClassVar[int]
    VIN_FIELD_NUMBER: _ClassVar[int]
    BATTERY_FIELD_NUMBER: _ClassVar[int]
    id: str
    vin: str
    battery: Battery
    def __init__(self, id: _Optional[str] = ..., vin: _Optional[str] = ..., battery: _Optional[_Union[Battery, _Mapping]] = ...) -> None: ...
