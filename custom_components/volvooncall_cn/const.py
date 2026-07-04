"""Constants for the Volvo On Call CN integration."""

CONF_POWERTRAIN_TYPE = "powertrain_type"

POWERTRAIN_FUEL = "b4_b5_b6"
POWERTRAIN_T8 = "t8"
DEFAULT_POWERTRAIN_TYPE = POWERTRAIN_T8

POWERTRAIN_OPTIONS = {
    POWERTRAIN_FUEL: "B4/B5/B6",
    POWERTRAIN_T8: "T8",
}

ELECTRIC_SENSOR_KEYS = (
    "tm_energy_consumption",
    "battery_charge_level_percentage",
    "electric_range",
    "battery_charging_status",
    "charger_connection_status",
    "estimated_charging_time",
    "charging_power",
)
