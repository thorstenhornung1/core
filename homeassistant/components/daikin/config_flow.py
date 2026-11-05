"""Config flow for the Daikin platform."""

import asyncio
import logging
from typing import Any, override
from uuid import uuid4

from aiohttp import ClientError, web_exceptions
import probatio
from pydaikin.daikin_base import Appliance
from pydaikin.discovery import Discovery
from pydaikin.exceptions import DaikinException
from pydaikin.factory import DaikinFactory

from homeassistant.config_entries import (
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_PASSWORD, CONF_UUID
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from homeassistant.util.ssl import client_context_no_verify

from .const import DOMAIN, KEY_MAC, TIMEOUT_SEC
from .coordinator import DaikinConfigEntry
from .temperature_step import DaikinStepResetError, DaikinTemperatureStep

_LOGGER = logging.getLogger(__name__)


class FlowHandler(ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1

    @staticmethod
    @callback
    @override
    def async_get_options_flow(
        config_entry: DaikinConfigEntry,
    ) -> DaikinOptionsFlow:
        """Return the flow for inspecting and resetting learned step detection."""
        return DaikinOptionsFlow()

    def __init__(self) -> None:
        """Initialize the Daikin config flow."""
        self.host: str | None = None

    @property
    def schema(self) -> probatio.Schema:
        """Return current schema."""
        return probatio.Schema(
            {
                probatio.Required(CONF_HOST, default=self.host): str,
                probatio.Optional(probatio.Secret(CONF_API_KEY)): str,
                probatio.Optional(probatio.Secret(CONF_PASSWORD)): str,
            }
        )

    async def _create_entry(
        self,
        host: str,
        mac: str,
        key: str | None = None,
        uuid: str | None = None,
        password: str | None = None,
    ) -> ConfigFlowResult:
        """Register new entry."""
        if not self.unique_id:
            await self.async_set_unique_id(mac)
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=host,
            data={
                CONF_HOST: host,
                KEY_MAC: mac,
                CONF_API_KEY: key,
                CONF_UUID: uuid,
                CONF_PASSWORD: password,
            },
        )

    async def _create_device(
        self, host: str, key: str | None = None, password: str | None = None
    ) -> ConfigFlowResult:
        """Create device."""
        # BRP07Cxx devices needs uuid together with key
        if key:
            uuid = str(uuid4())
        else:
            uuid = None
            key = None

        if not password:
            password = None

        try:
            async with asyncio.timeout(TIMEOUT_SEC):
                device: Appliance = await DaikinFactory(
                    host,
                    async_get_clientsession(self.hass),
                    key=key,
                    uuid=uuid,
                    password=password,
                    ssl_context=client_context_no_verify(),
                )
        except TimeoutError, ClientError:
            self.host = None
            return self.async_show_form(
                step_id="user",
                data_schema=self.schema,
                errors={"base": "cannot_connect"},
            )
        except web_exceptions.HTTPForbidden:
            return self.async_show_form(
                step_id="user",
                data_schema=self.schema,
                errors={"base": "invalid_auth"},
            )
        except DaikinException:
            return self.async_show_form(
                step_id="user",
                data_schema=self.schema,
                errors={"base": "cannot_connect"},
            )
        except Exception:
            _LOGGER.exception("Unexpected error creating device")
            return self.async_show_form(
                step_id="user",
                data_schema=self.schema,
                errors={"base": "unknown"},
            )

        mac = device.mac
        return await self._create_entry(host, mac, key, uuid, password)

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """User initiated config flow."""
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=self.schema)
        if user_input.get(CONF_API_KEY) and user_input.get(CONF_PASSWORD):
            self.host = user_input[CONF_HOST]
            return self.async_show_form(
                step_id="user",
                data_schema=self.schema,
                errors={"base": "api_password"},
            )
        return await self._create_device(
            user_input[CONF_HOST],
            user_input.get(CONF_API_KEY),
            user_input.get(CONF_PASSWORD),
        )

    @override
    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Prepare configuration for a discovered Daikin device."""
        _LOGGER.debug("Zeroconf user_input: %s", discovery_info)
        devices = Discovery().poll(ip=discovery_info.host)
        if not devices:
            _LOGGER.debug(
                (
                    "Could not find MAC-address for %s, make sure the required UDP"
                    " ports are open (see integration documentation)"
                ),
                discovery_info.host,
            )
            return self.async_abort(reason="cannot_connect")
        await self.async_set_unique_id(next(iter(devices))[KEY_MAC])
        self._abort_if_unique_id_configured()
        self.host = discovery_info.host
        return await self.async_step_user()


class DaikinOptionsFlow(OptionsFlow):
    """Show the learned step and let users explicitly reset it."""

    def _learned_step(self) -> DaikinTemperatureStep | str:
        """Return the learned step adapter, or why there is nothing to reset."""
        if self.config_entry.state is not ConfigEntryState.LOADED:
            return "step_not_loaded"
        step = self.config_entry.runtime_data.temperature_step
        if step is None:
            return "step_fixed"
        if step.status == "unknown":
            return "step_unknown"
        return step

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the learned step and offer to keep or reset it."""
        if isinstance(step := self._learned_step(), str):
            return self.async_abort(reason=step)
        if step.status == "confirmed":
            return await self.async_step_confirmed()
        return await self.async_step_inferred()

    async def async_step_confirmed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the menu for a confirmed half-degree step."""
        return self.async_show_menu(step_id="confirmed", menu_options=["keep", "reset"])

    async def async_step_inferred(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the menu for an inferred whole-degree step."""
        return self.async_show_menu(step_id="inferred", menu_options=["keep", "reset"])

    async def async_step_keep(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Leave the learned step unchanged."""
        return self.async_abort(reason="step_kept")

    async def async_step_reset(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Forget the learned step without contacting the device."""
        if isinstance(step := self._learned_step(), str):
            return self.async_abort(reason=step)
        try:
            await step.async_reset()
        except DaikinStepResetError:
            return self.async_abort(reason="reset_failed")
        self.config_entry.runtime_data.async_update_listeners()
        return self.async_abort(reason="reset_successful")
