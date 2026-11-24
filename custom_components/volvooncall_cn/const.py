"""Constants for the Volvo On Call CN integration."""

CONF_POWERTRAIN_TYPE = "powertrain_type"
# Successful config-flow authentication hands tokens to setup in memory. This
# avoids immediately repeating the password login without ever placing tokens
# in ConfigEntry data/options.
VALIDATED_TOKENS_DATA = "volvooncall_cn_validated_tokens"
# Username -> unix time of the last password login attempted from a config
# flow, so the risk-control cooldown also covers repeated attempts in the UI.
PASSWORD_LOGIN_TIMES_DATA = "volvooncall_cn_password_login_times"

POWERTRAIN_FUEL = "b4_b5_b6"
POWERTRAIN_HYBRID = "t8"
DEFAULT_POWERTRAIN_TYPE = POWERTRAIN_HYBRID

POWERTRAIN_OPTIONS = {
    POWERTRAIN_FUEL: "轻混/纯油",
    POWERTRAIN_HYBRID: "混动",
}

# Electric-only control entities (non-sensor platforms) that must be
# cleaned up alongside ELECTRIC_SENSOR_KEYS when switching to a fuel car.
ELECTRIC_CONTROL_KEYS = (
    "home_charge_switch",
    "plug_and_charge_switch",
    "charge_limit_number",
)

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

# --- Dynamic polling -------------------------------------------------------
# On by default, including for entries created before the option existed:
# it cuts a parked car's traffic by roughly 88%. Slower night-time polling
# also delays automations that react to lock or door state, which is why it
# stays a switch and the README calls the trade-off out.
CONF_DYNAMIC_POLLING = "dynamic_polling"
CONF_DYNAMIC_POLLING_SETTINGS = "dynamic_polling_settings"
CONF_IN_USE_SCAN_INTERVAL = "in_use_scan_interval"
CONF_CHARGING_SCAN_INTERVAL = "charging_scan_interval"
CONF_DAY_SCAN_INTERVAL = "day_scan_interval"
CONF_NIGHT_SCAN_INTERVAL = "night_scan_interval"
CONF_DAY_START = "day_start"
CONF_NIGHT_START = "night_start"
CONF_IN_USE_WINDOWS = "in_use_windows"

DEFAULT_DYNAMIC_POLLING = True
# Driving (or a configured commute window): position and door state change
# by the minute.
DEFAULT_IN_USE_SCAN_INTERVAL = 60
# A ~3 kW home charger adds about 1% of a T8 battery every 2.5-4 minutes, so
# anything faster than five minutes mostly re-reads the same percentage.
DEFAULT_CHARGING_SCAN_INTERVAL = 5 * 60
DEFAULT_DAY_SCAN_INTERVAL = 10 * 60
DEFAULT_NIGHT_SCAN_INTERVAL = 30 * 60
DEFAULT_DAY_START = "07:00"
DEFAULT_NIGHT_START = "22:00"
DEFAULT_IN_USE_WINDOWS = "07:00-09:00,17:00-19:30"
MAX_DYNAMIC_SCAN_INTERVAL = 60 * 60
