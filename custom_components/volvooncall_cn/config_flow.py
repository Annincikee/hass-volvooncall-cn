import hashlib
import logging

from homeassistant import config_entries
# NOTE: keep imports explicit. A wildcard import of homeassistant.const
# touches every deprecated constant and triggers deprecation warnings that
# become errors in future Home Assistant releases.
from homeassistant.const import CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
    TimeSelector,
)
import voluptuous as vol
from .volvooncall_base import VolvoAPIError, VolvoAuthThrottledError
from .volvooncall_base import DEFAULT_SCAN_INTERVAL, MIN_SCAN_INTERVAL
from .volvooncall_cn import VehicleAPI
from .volvooncall_cn import DOMAIN
from .const import (
    CONF_CHARGING_SCAN_INTERVAL,
    CONF_DAY_SCAN_INTERVAL,
    CONF_DAY_START,
    CONF_DYNAMIC_POLLING,
    CONF_DYNAMIC_POLLING_SETTINGS,
    CONF_IN_USE_SCAN_INTERVAL,
    CONF_IN_USE_WINDOWS,
    CONF_NIGHT_SCAN_INTERVAL,
    CONF_NIGHT_START,
    CONF_POWERTRAIN_TYPE,
    DEFAULT_CHARGING_SCAN_INTERVAL,
    DEFAULT_DAY_SCAN_INTERVAL,
    DEFAULT_DAY_START,
    DEFAULT_DYNAMIC_POLLING,
    DEFAULT_IN_USE_SCAN_INTERVAL,
    DEFAULT_IN_USE_WINDOWS,
    DEFAULT_NIGHT_SCAN_INTERVAL,
    DEFAULT_NIGHT_START,
    DEFAULT_POWERTRAIN_TYPE,
    MAX_DYNAMIC_SCAN_INTERVAL,
    PASSWORD_LOGIN_TIMES_DATA,
    POWERTRAIN_OPTIONS,
    VALIDATED_TOKENS_DATA,
)
from .polling import (
    format_time_of_day,
    format_time_windows,
    parse_time_of_day,
    parse_time_windows,
)

_LOGGER = logging.getLogger(__name__)
PASSWORD_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD)
)
DYNAMIC_INTERVAL = vol.All(
    vol.Coerce(int),
    vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_DYNAMIC_SCAN_INTERVAL),
)
# key -> default, in the order the fields are shown.
DYNAMIC_POLLING_DEFAULTS = {
    CONF_IN_USE_SCAN_INTERVAL: DEFAULT_IN_USE_SCAN_INTERVAL,
    CONF_IN_USE_WINDOWS: DEFAULT_IN_USE_WINDOWS,
    CONF_CHARGING_SCAN_INTERVAL: DEFAULT_CHARGING_SCAN_INTERVAL,
    CONF_DAY_SCAN_INTERVAL: DEFAULT_DAY_SCAN_INTERVAL,
    CONF_DAY_START: DEFAULT_DAY_START,
    CONF_NIGHT_SCAN_INTERVAL: DEFAULT_NIGHT_SCAN_INTERVAL,
    CONF_NIGHT_START: DEFAULT_NIGHT_START,
}


def normalize_dynamic_polling(settings: dict) -> tuple[dict, str | None]:
    """Canonicalize the dynamic-polling fields; return (values, error key).

    Times are stored as HH:MM and windows as "HH:MM-HH:MM,..." so the
    coordinator never has to parse free-form user text.
    """
    values = {**DYNAMIC_POLLING_DEFAULTS, **settings}
    try:
        day_start = parse_time_of_day(values[CONF_DAY_START])
        night_start = parse_time_of_day(values[CONF_NIGHT_START])
    except ValueError:
        return values, "invalid_day_night_time"
    if day_start == night_start:
        return values, "invalid_day_night_time"
    try:
        windows = parse_time_windows(values[CONF_IN_USE_WINDOWS])
    except ValueError:
        return values, "invalid_in_use_windows"
    values[CONF_DAY_START] = format_time_of_day(day_start)
    values[CONF_NIGHT_START] = format_time_of_day(night_start)
    values[CONF_IN_USE_WINDOWS] = format_time_windows(windows)
    return values, None


def masked_account_title(username: str) -> str:
    """Return a useful config-entry title without exposing the full account."""
    username = str(username).strip()
    if len(username) == 11 and username.isdigit():
        return f"车辆账户 · {username[:3]}****{username[-4:]}"
    digest = hashlib.sha256(username.encode("utf-8")).hexdigest()[:8]
    return f"车辆账户 · account-{digest}"


def _account_is_configured_elsewhere(hass, entry, username: str) -> bool:
    """Return whether another entry already runs the same account."""
    for candidate in hass.config_entries.async_entries(DOMAIN):
        if candidate.entry_id == entry.entry_id:
            continue
        candidate_config = {**candidate.data, **candidate.options}
        if str(candidate_config.get(CONF_USERNAME, "")).strip() == username:
            return True
    return False


def _last_password_login_at(hass, username: str) -> int:
    """Return when a password login was last attempted for this account.

    Every validation builds a fresh API object, whose cooldown starts at
    zero, so without this each submit of the form was a real password post:
    a few wrong guesses in a row is exactly the burst risk control punishes.
    Considers earlier flow attempts and the running integration alike.
    """
    latest = hass.data.get(PASSWORD_LOGIN_TIMES_DATA, {}).get(username, 0)
    for coordinator in hass.data.get(DOMAIN, {}).values():
        api = getattr(coordinator, "volvo_api", None)
        if getattr(api, "_username", None) != username:
            continue
        attempted = getattr(api, "_last_password_login_at", 0)
        if isinstance(attempted, (int, float)):
            latest = max(latest, attempted)
    return int(latest)


async def volvo_validation(hass, username, password) -> dict:
    errors = {}
    session = async_get_clientsession(hass)
    volvo_api = None
    try:
        volvo_api = VehicleAPI(
            session=session, username=username, password=password)
        volvo_api._last_password_login_at = _last_password_login_at(
            hass, username
        )
        await volvo_api.login()
        # Setup consumes this one-shot handoff. Tokens remain in memory and in
        # the private auth Store, never in the ConfigEntry or flow result.
        hass.data.setdefault(VALIDATED_TOKENS_DATA, {})[username] = (
            volvo_api.export_tokens()
        )
    except VolvoAuthThrottledError:
        errors["base"] = "login_cooldown"
    except VolvoAPIError as err:
        errors["base"] = err.message or "unknown"
    except Exception as err:
        _LOGGER.error(
            "Unhandled exception in user step (%s); details suppressed",
            type(err).__name__,
        )
        errors["base"] = "unknown"
    finally:
        attempted = getattr(volvo_api, "_last_password_login_at", 0)
        if isinstance(attempted, (int, float)) and attempted:
            hass.data.setdefault(PASSWORD_LOGIN_TIMES_DATA, {})[username] = (
                int(attempted)
            )
    return errors


class VolvoOnCallCnConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow with user setup and reauthentication support."""
    # The schema version of the entries that it creates
    # Home Assistant will call your migrate method if the version changes
    VERSION = 3
    _reauth_entry = None

    @staticmethod
    @callback
    def async_get_options_flow(entry: config_entries.ConfigEntry):
        return VolvoOnCallCnOptionsFlow(entry)

    async def async_step_user(self, user_input):
        errors = {}
        if user_input is not None:
            username = str(user_input.get(CONF_USERNAME, "")).strip()
            password = user_input.get(CONF_PASSWORD, "")

            # Validate non-empty username and password. Surface the problem as
            # a form error rather than an unhandled exception so the user sees
            # a field message instead of a generic "unknown error".
            if not username or not password:
                errors["base"] = "missing_credentials"
            else:
                # Set unique_id and check for duplicates
                await self.async_set_unique_id(username)
                self._abort_if_unique_id_configured()

                errors = await volvo_validation(self.hass, username, password)
                if not errors:
                    user_input = {**user_input, CONF_USERNAME: username}
                    return self.async_create_entry(
                        title=masked_account_title(username), data=user_input
                    )
        config_schema = vol.Schema({
            vol.Required(CONF_USERNAME): str,
            vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
            vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.All(vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL)),
            vol.Required(
                CONF_POWERTRAIN_TYPE, default=DEFAULT_POWERTRAIN_TYPE
            ): vol.In(POWERTRAIN_OPTIONS),
            # Shown here so the fixed interval above is not silently unused;
            # the per-mode intervals are tuned later in Options.
            vol.Optional(
                CONF_DYNAMIC_POLLING, default=DEFAULT_DYNAMIC_POLLING
            ): bool,
        })
        return self.async_show_form(step_id="user", data_schema=config_schema, errors=errors)

    async def async_step_reauth(self, entry_data):
        """Start reauthentication after repeated credential rejections."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        """Ask for the new password and revalidate the account."""
        errors = {}
        entry = self._reauth_entry
        if entry is None:
            return self.async_abort(reason="reauth_failed")
        # Options may contain newer credentials than data. Setup and the
        # options listener both use this same merged precedence, so reauth
        # must validate the account that is actually running.
        config_data = {**entry.data, **entry.options}
        username = str(config_data.get(CONF_USERNAME, "")).strip()

        if user_input is not None:
            password = user_input.get(CONF_PASSWORD, "")
            if not password:
                errors["base"] = "missing_credentials"
            elif _account_is_configured_elsewhere(
                self.hass, entry, username
            ):
                errors["base"] = "account_already_configured"
            else:
                errors = await volvo_validation(self.hass, username, password)
                if not errors:
                    # Credentials have one canonical home: entry.data. Older
                    # versions also wrote them to entry.options, so normalize
                    # both successful reauth and Options saves while we still
                    # honour the account those legacy options were running.
                    data = {
                        **entry.data,
                        CONF_USERNAME: username,
                        CONF_PASSWORD: password,
                    }
                    options = {
                        key: value
                        for key, value in entry.options.items()
                        if key not in (CONF_USERNAME, CONF_PASSWORD)
                    }
                    self.hass.config_entries.async_update_entry(
                        entry, data=data, options=options
                    )
                    # Always restart after reauth, even when the same password
                    # produced a fresh session or only legacy options changed.
                    # Otherwise the one-shot validated token could remain
                    # unused while the rejected runtime session kept polling.
                    self.hass.config_entries.async_schedule_reload(
                        entry.entry_id
                    )
                    return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR}
            ),
            errors=errors,
            description_placeholders={"username": username},
        )


class VolvoOnCallCnOptionsFlow(config_entries.OptionsFlow):
    """Options flow.

    OptionsFlow.config_entry is a read-only property on modern Home
    Assistant that resolves the live entry from the handler id, so there is
    nothing to store or override here. The previous override also could not
    do what it looked like it did: getattr() with a default only swallows
    AttributeError, not the ValueError the base property raises before hass
    is set, so the "fallback" never ran — it just risked serving a stale
    entry captured at flow-creation time if the base property ever went away.
    """

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        # Signature kept: async_get_options_flow() still passes the entry.
        pass

    async def async_step_init(self, user_input=None):
        return await self.async_step_user()

    async def async_step_user(self, user_input=None):
        entry = self.config_entry
        # Keep compatibility with entries created by older versions, where
        # Options could override credentials. The next successful save moves
        # that effective account back to entry.data and removes both sensitive
        # fields from Options.
        current_config = {**entry.data, **entry.options}
        username = str(current_config.get(CONF_USERNAME, "")).strip()
        current_password = current_config.get(CONF_PASSWORD, "")
        scan_interval = current_config.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        powertrain_type = current_config.get(
            CONF_POWERTRAIN_TYPE, DEFAULT_POWERTRAIN_TYPE
        )
        dynamic_polling = bool(
            current_config.get(CONF_DYNAMIC_POLLING, DEFAULT_DYNAMIC_POLLING)
        )
        dynamic_settings = {
            key: current_config.get(key, default)
            for key, default in DYNAMIC_POLLING_DEFAULTS.items()
        }
        errors = {}

        if user_input is not None:
            dynamic_polling = bool(
                user_input.get(CONF_DYNAMIC_POLLING, dynamic_polling)
            )
            # The section is collapsed by default; when it is not submitted
            # the stored values are kept.
            dynamic_settings, dynamic_error = normalize_dynamic_polling(
                {
                    **dynamic_settings,
                    **(user_input.get(CONF_DYNAMIC_POLLING_SETTINGS) or {}),
                }
            )
            submitted_password = user_input.get(CONF_PASSWORD, "") or ""
            scan_interval = user_input.get(
                CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
            )
            powertrain_type = user_input.get(
                CONF_POWERTRAIN_TYPE, DEFAULT_POWERTRAIN_TYPE
            )

            if not username:
                errors["base"] = "missing_credentials"
            elif _account_is_configured_elsewhere(
                self.hass, entry, username
            ):
                errors["base"] = "account_already_configured"
            elif dynamic_error:
                # Checked before the password so a typo in a time field can
                # never cost a real login attempt.
                errors["base"] = dynamic_error
            elif submitted_password and submitted_password != current_password:
                errors = await volvo_validation(
                    self.hass, username, submitted_password
                )
            elif not current_password:
                errors["base"] = "missing_credentials"

            if not errors:
                password = submitted_password or current_password
                data = {
                    **entry.data,
                    CONF_USERNAME: username,
                    CONF_PASSWORD: password,
                }
                options = {
                    key: value
                    for key, value in entry.options.items()
                    if key not in (CONF_USERNAME, CONF_PASSWORD)
                }
                options.update(
                    {
                        CONF_SCAN_INTERVAL: scan_interval,
                        CONF_POWERTRAIN_TYPE: powertrain_type,
                        CONF_DYNAMIC_POLLING: dynamic_polling,
                        **dynamic_settings,
                    }
                )
                # Update data and options together so an update listener can
                # never observe new data shadowed by a stale option password.
                self.hass.config_entries.async_update_entry(
                    entry,
                    data=data,
                    options=options,
                    unique_id=username,
                    title=masked_account_title(username),
                )
                return self.async_create_entry(title="", data=options)

        config_schema = vol.Schema({
            # Never round-trip the stored password to the frontend. Blank
            # means keep it; only a genuinely changed value is revalidated.
            vol.Optional(CONF_PASSWORD, default=""): PASSWORD_SELECTOR,
            vol.Optional(CONF_SCAN_INTERVAL, default=scan_interval): vol.All(vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL)),
            vol.Required(
                CONF_POWERTRAIN_TYPE, default=powertrain_type
            ): vol.In(POWERTRAIN_OPTIONS),
            vol.Optional(CONF_DYNAMIC_POLLING, default=dynamic_polling): bool,
            vol.Optional(CONF_DYNAMIC_POLLING_SETTINGS): section(
                vol.Schema({
                    vol.Optional(
                        CONF_IN_USE_SCAN_INTERVAL,
                        default=dynamic_settings[CONF_IN_USE_SCAN_INTERVAL],
                    ): DYNAMIC_INTERVAL,
                    vol.Optional(
                        CONF_IN_USE_WINDOWS,
                        default=dynamic_settings[CONF_IN_USE_WINDOWS],
                    ): str,
                    vol.Optional(
                        CONF_CHARGING_SCAN_INTERVAL,
                        default=dynamic_settings[CONF_CHARGING_SCAN_INTERVAL],
                    ): DYNAMIC_INTERVAL,
                    vol.Optional(
                        CONF_DAY_SCAN_INTERVAL,
                        default=dynamic_settings[CONF_DAY_SCAN_INTERVAL],
                    ): DYNAMIC_INTERVAL,
                    vol.Optional(
                        CONF_DAY_START,
                        default=dynamic_settings[CONF_DAY_START],
                    ): TimeSelector(),
                    vol.Optional(
                        CONF_NIGHT_SCAN_INTERVAL,
                        default=dynamic_settings[CONF_NIGHT_SCAN_INTERVAL],
                    ): DYNAMIC_INTERVAL,
                    vol.Optional(
                        CONF_NIGHT_START,
                        default=dynamic_settings[CONF_NIGHT_START],
                    ): TimeSelector(),
                }),
                {"collapsed": True},
            ),
        })
        return self.async_show_form(step_id="user", data_schema=config_schema, errors=errors)
