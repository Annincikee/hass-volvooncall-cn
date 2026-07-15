"""Constants for the Volvo On Call CN integration."""

CONF_POWERTRAIN_TYPE = "powertrain_type"

POWERTRAIN_FUEL = "b4_b5_b6"
POWERTRAIN_HYBRID = "t8"
DEFAULT_POWERTRAIN_TYPE = POWERTRAIN_HYBRID

POWERTRAIN_OPTIONS = {
    POWERTRAIN_FUEL: "轻混/纯油",
    POWERTRAIN_HYBRID: "混动",
}

ELECTRIC_SENSOR_KEYS = (
    "tm_energy_consumption",
    "battery_charge_level_percentage",
    "electric_range",
    "full_charge_electric_range",
    "battery_charging_status",
    "charger_connection_status",
    "estimated_charging_time",
    "charging_power",
    "charging_voltage",
    "charging_current",
    "charging_session_energy",
)
