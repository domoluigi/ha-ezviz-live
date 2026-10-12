"""Config flow for EZVIZ Live.

Only the session token is stored; the password is used once to log in and is
never saved. When the token can no longer be refreshed, reauth asks again.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from pyezvizapi.client import EzvizClient
from pyezvizapi.exceptions import (
    EzvizAuthTokenExpired,
    EzvizAuthVerificationCode,
    PyEzvizError,
)
import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_MAX_LIVE_SECONDS,
    CONF_REGION,
    CONF_TOKEN,
    DEFAULT_MAX_LIVE_SECONDS,
    DEFAULT_REGION,
    DOMAIN,
    MAX_MAX_LIVE_SECONDS,
    MIN_MAX_LIVE_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

CONF_MFA_CODE = "mfa_code"


class EzvizLiveConfigFlow(ConfigFlow, domain=DOMAIN):
    """Log in to the EZVIZ cloud."""

    VERSION = 1

    def __init__(self) -> None:
        self._email: str | None = None
        self._region: str = DEFAULT_REGION
        self._client: EzvizClient | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: Any) -> OptionsFlow:
        return EzvizLiveOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._email = user_input[CONF_EMAIL].strip()
            self._region = user_input.get(CONF_REGION, DEFAULT_REGION).strip()
            return await self._async_login(user_input[CONF_PASSWORD], None, errors)

        default_email = self._email or ""
        if self.source == SOURCE_REAUTH:
            default_email = self._get_reauth_entry().title
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL, default=default_email): str,
                    vol.Required(CONF_PASSWORD): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                    vol.Optional(CONF_REGION, default=self._region): str,
                }
            ),
            errors=errors,
        )

    async def async_step_mfa(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            code = str(user_input[CONF_MFA_CODE]).strip()
            if not code.isdigit():
                errors["base"] = "invalid_mfa"
            else:
                return await self._async_login(None, int(code), errors)
        return self.async_show_form(
            step_id="mfa",
            data_schema=vol.Schema({vol.Required(CONF_MFA_CODE): str}),
            errors=errors,
        )

    async def _async_login(
        self, password: str | None, mfa_code: int | None, errors: dict[str, str]
    ) -> ConfigFlowResult:
        if password is not None:
            self._client = EzvizClient(self._email, password, self._region)
        assert self._client is not None
        client = self._client
        try:
            await self.hass.async_add_executor_job(client.login, mfa_code)
        except EzvizAuthVerificationCode:
            return await self.async_step_mfa()
        except EzvizAuthTokenExpired:
            errors["base"] = "invalid_auth"
        except PyEzvizError as err:
            _LOGGER.debug("EZVIZ login failed: %s", err)
            errors["base"] = "invalid_auth" if "password" in str(err).lower() else "cannot_connect"
        except OSError:
            errors["base"] = "cannot_connect"
        else:
            token = dict(client.export_token())
            await self.hass.async_add_executor_job(client.close_session)
            self._client = None
            return await self._async_finish(token)

        if errors.get("base") == "invalid_mfa":
            return self.async_show_form(step_id="mfa", errors=errors)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL, default=self._email or ""): str,
                    vol.Required(CONF_PASSWORD): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                    vol.Optional(CONF_REGION, default=self._region): str,
                }
            ),
            errors=errors,
        )

    async def _async_finish(self, token: dict[str, Any]) -> ConfigFlowResult:
        assert self._email is not None
        await self.async_set_unique_id(self._email.lower())
        data = {CONF_TOKEN: token, CONF_REGION: self._region}
        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(self._get_reauth_entry(), data=data)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=self._email, data=data)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._region = entry_data.get(CONF_REGION, DEFAULT_REGION)
        return await self.async_step_user()


class EzvizLiveOptionsFlow(OptionsFlow):
    """Live view limits."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self.config_entry.options.get(CONF_MAX_LIVE_SECONDS, DEFAULT_MAX_LIVE_SECONDS)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MAX_LIVE_SECONDS, default=current): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=MIN_MAX_LIVE_SECONDS,
                            max=MAX_MAX_LIVE_SECONDS,
                            step=10,
                            unit_of_measurement="s",
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    )
                }
            ),
        )
