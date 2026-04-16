"""Config flow for Volcast Solar Forecast."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithConfigEntry,
)
from homeassistant.const import CONF_API_KEY
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_API_URL,
    CONF_BATTERY_CHARGE_POWER_ENTITY,
    CONF_PEAK_THRESHOLD,
    CONF_PV_ENERGY_ENTITY,
    CONF_BATTERY_SOC_ENTITY,
    CONF_PV_POWER_ENTITY,
    CONF_UPDATE_INTERVAL,
    DEFAULT_API_URL,
    DEFAULT_PEAK_THRESHOLD,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_KEY): str,
        vol.Optional(CONF_API_URL, default=DEFAULT_API_URL): str,
    }
)


async def _validate_api_key(api_key: str, api_url: str) -> dict[str, Any]:
    """Validate API key by making a test request."""
    url = f"{api_url}?key={api_key}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 401:
                    raise InvalidAuth
                if resp.status == 403:
                    raise InvalidAuth("Premium subscription required")
                if resp.status >= 500:
                    raise CannotConnect(f"Server error: {resp.status}")
                if not resp.ok:
                    raise CannotConnect(f"Unexpected status: {resp.status}")

                data = await resp.json()
                location = data.get("attributes", {}).get("location", "Volcast")
                return {"title": f"Volcast — {location}"}

    except aiohttp.ClientError as err:
        raise CannotConnect(str(err)) from err


class VolcastConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Volcast."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize."""
        self._api_data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step — API key entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            api_url = user_input.get(CONF_API_URL, DEFAULT_API_URL).strip()

            try:
                info = await _validate_api_key(api_key, api_url)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected exception during validation")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(api_key)
                self._abort_if_unique_id_configured()

                self._api_data = {
                    CONF_API_KEY: api_key,
                    CONF_API_URL: api_url,
                    "title": info["title"],
                }
                return await self.async_step_production()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_production(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle step 2 — optional PV production sensor mapping."""
        if user_input is not None:
            options = {
                CONF_PV_ENERGY_ENTITY: user_input.get(CONF_PV_ENERGY_ENTITY, ""),
                CONF_PV_POWER_ENTITY: user_input.get(CONF_PV_POWER_ENTITY, ""),
                CONF_BATTERY_SOC_ENTITY: user_input.get(CONF_BATTERY_SOC_ENTITY, ""),
                CONF_BATTERY_CHARGE_POWER_ENTITY: user_input.get(CONF_BATTERY_CHARGE_POWER_ENTITY, ""),
            }
            return self.async_create_entry(
                title=self._api_data["title"],
                data={
                    CONF_API_KEY: self._api_data[CONF_API_KEY],
                    CONF_API_URL: self._api_data[CONF_API_URL],
                },
                options=options,
            )

        production_schema = vol.Schema(
            {
                vol.Optional(CONF_PV_ENERGY_ENTITY, default=""): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor",
                        device_class="energy",
                    )
                ),
                vol.Optional(CONF_PV_POWER_ENTITY, default=""): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor",
                        device_class="power",
                    )
                ),
                vol.Optional(CONF_BATTERY_SOC_ENTITY, default=""): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor",
                        device_class="battery",
                    )
                ),
                vol.Optional(CONF_BATTERY_CHARGE_POWER_ENTITY, default=""): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor",
                        device_class="power",
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="production",
            data_schema=production_schema,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> VolcastOptionsFlow:
        """Create the options flow."""
        return VolcastOptionsFlow(config_entry)


class VolcastOptionsFlow(OptionsFlowWithConfigEntry):
    """Handle options flow for Volcast."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options_schema = vol.Schema(
            {
                vol.Optional(
                    CONF_UPDATE_INTERVAL,
                    default=self.config_entry.options.get(
                        CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL
                    ),
                ): vol.All(int, vol.Range(min=15, max=1440)),
                vol.Optional(
                    CONF_PEAK_THRESHOLD,
                    default=self.config_entry.options.get(
                        CONF_PEAK_THRESHOLD, DEFAULT_PEAK_THRESHOLD
                    ),
                ): vol.All(int, vol.Range(min=50, max=100)),
                vol.Optional(
                    CONF_PV_ENERGY_ENTITY,
                    default=self.config_entry.options.get(
                        CONF_PV_ENERGY_ENTITY, ""
                    ),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor",
                        device_class="energy",
                    )
                ),
                vol.Optional(
                    CONF_PV_POWER_ENTITY,
                    default=self.config_entry.options.get(
                        CONF_PV_POWER_ENTITY, ""
                    ),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor",
                        device_class="power",
                    )
                ),
                vol.Optional(
                    CONF_BATTERY_SOC_ENTITY,
                    default=self.config_entry.options.get(
                        CONF_BATTERY_SOC_ENTITY, ""
                    ),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor",
                        device_class="battery",
                    )
                ),
                vol.Optional(
                    CONF_BATTERY_CHARGE_POWER_ENTITY,
                    default=self.config_entry.options.get(
                        CONF_BATTERY_CHARGE_POWER_ENTITY, ""
                    ),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor",
                        device_class="power",
                    )
                ),
            }
        )

        return self.async_show_form(step_id="init", data_schema=options_schema)


class CannotConnect(Exception):
    """Error to indicate we cannot connect."""


class InvalidAuth(Exception):
    """Error to indicate invalid auth."""
