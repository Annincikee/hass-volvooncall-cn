import hashlib
import logging

from homeassistant import config_entries
# NOTE: keep imports explicit. A wildcard import of homeassistant.const
# touches every deprecated constant and triggers deprecation warnings that
# become errors in future Home Assistant releases.
from homeassistant.const import CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol
from .volvooncall_base import VolvoAPIError
from .volvooncall_base import DEFAULT_SCAN_INTERVAL, MIN_SCAN_INTERVAL
from .volvooncall_cn import VehicleAPI
from .volvooncall_cn import DOMAIN
from .const import (
    CONF_POWERTRAIN_TYPE,
    DEFAULT_POWERTRAIN_TYPE,
    POWERTRAIN_OPTIONS,
)

_LOGGER = logging.getLogger(__name__)


def masked_account_title(username: str) -> str:
    """Return a useful config-entry title without exposing the full account."""
    username = str(username).strip()
    if len(username) == 11 and username.isdigit():
        return f"车辆账户 · {username[:3]}****{username[-4:]}"
    digest = hashlib.sha256(username.encode("utf-8")).hexdigest()[:8]
    return f"车辆账户 · account-{digest}"


async def volvo_validation(hass, username, password) -> dict:
    errors = {}
    session = async_get_clientsession(hass)
    try:
        volvo_api = VehicleAPI(
            session=session, username=username, password=password)
        await volvo_api.login()
    except VolvoAPIError as err:
        errors["base"] = err.message
    except Exception as err:
        _LOGGER.error(
            "Unhandled exception in user step (%s); details suppressed",
            type(err).__name__,
        )
        errors["base"] = "unknown"
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
            username = user_input.get(CONF_USERNAME, "")
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
                    return self.async_create_entry(
                        title=masked_account_title(username), data=user_input
                    )
        config_schema = vol.Schema({
            vol.Required(CONF_USERNAME): str,
            vol.Required(CONF_PASSWORD): str,
            vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.All(vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL)),
            vol.Required(
                CONF_POWERTRAIN_TYPE, default=DEFAULT_POWERTRAIN_TYPE
            ): vol.In(POWERTRAIN_OPTIONS),
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
        username = entry.data.get(CONF_USERNAME, "")

        if user_input is not None:
            password = user_input.get(CONF_PASSWORD, "")
            if not password:
                errors["base"] = "missing_credentials"
            else:
                errors = await volvo_validation(self.hass, username, password)
                if not errors:
                    data = {**entry.data, CONF_PASSWORD: password}
                    options = dict(entry.options)
                    # The options flow may carry its own stale password copy.
                    if CONF_PASSWORD in options:
                        options[CONF_PASSWORD] = password
                    self.hass.config_entries.async_update_entry(
                        entry, data=data, options=options
                    )
                    await self.hass.config_entries.async_reload(entry.entry_id)
                    return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
            description_placeholders={"username": username},
        )


class VolvoOnCallCnOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    @property
    def config_entry(self):
        # 优先用基类提供的，回退到我们传入的
        return getattr(super(), 'config_entry', self._config_entry)

    async def async_step_init(self, user_input=None):
        return await self.async_step_user()

    async def async_step_user(self, user_input=None):
        init_done = False
        if not user_input:
            user_input = {**self.config_entry.data, **self.config_entry.options}
        else:
            init_done = True

        username = user_input.get(CONF_USERNAME, vol.UNDEFINED)
        password = user_input.get(CONF_PASSWORD, vol.UNDEFINED)
        scan_interval = user_input.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        powertrain_type = user_input.get(
            CONF_POWERTRAIN_TYPE, DEFAULT_POWERTRAIN_TYPE
        )
        errors = {}
        if init_done:
            errors = await volvo_validation(self.hass, username, password)
            if not errors:
                return self.async_create_entry(title="", data=user_input)

        config_schema = vol.Schema({
            vol.Required(CONF_USERNAME, default=username): str,
            vol.Required(CONF_PASSWORD, default=password): str,
            vol.Optional(CONF_SCAN_INTERVAL, default=scan_interval): vol.All(vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL)),
            vol.Required(
                CONF_POWERTRAIN_TYPE, default=powertrain_type
            ): vol.In(POWERTRAIN_OPTIONS),
        })
        return self.async_show_form(step_id="user", data_schema=config_schema, errors=errors)
