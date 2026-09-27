"""Config flow for Volcast Solar Forecast."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

import aiohttp
import voluptuous as vol

import homeassistant.util.dt as dt_util
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithConfigEntry,
)
from homeassistant.const import CONF_API_KEY
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import callback
from homeassistant.helpers import instance_id, selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .cloud.client import PairingClient, PairingDisabled, PairingError, PollResult, is_https_url
from .key_format import check_api_key_format
from .pairing import PairingPoller
from .version import async_integration_version
from .const import (
    BETA_PAIRING_URL,
    CONF_API_URL,
    CONF_BACKEND,
    CONF_BATTERY_CHARGE_POWER_ENTITY,
    CONF_MODE,
    CONF_PAIRED_AT,
    CONF_PAIRING,
    CONF_PAIRING_URL,
    CONF_USER_ID,
    CONF_PEAK_THRESHOLD,
    CONF_PV_ENERGY_ENTITY,
    CONF_BATTERY_SOC_ENTITY,
    CONF_PV_POWER_ENTITY,
    CONF_UPDATE_INTERVAL,
    DEFAULT_API_URL,
    DEFAULT_PEAK_THRESHOLD,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    MODE_DISCOVERY_ONLY,
)

_LOGGER = logging.getLogger(__name__)

PAIR_POLL_INTERVAL_S = 3.0
# Chmura zamyka niepotwierdzoną sesję po 10 min (poll → 410). Lokalny termin to tylko
# siatka bezpieczeństwa — z zapasem, żeby potwierdzenie z ostatnich sekund nie przepadło.
PAIR_DEADLINE_S = 630.0
# Okno postępu i wyborów po potwierdzeniu (chmura liczy je od potwierdzenia).
LIVE_WINDOW = timedelta(minutes=30)
_ABORT_BY_STATUS = {"expired": "pairing_expired", "gone": "pairing_expired", "disabled": "pairing_disabled"}
# Poświadczenia wydane (także w zgubionej odpowiedzi) — anulowanie sesji nic by nie cofnęło.
_CREDENTIALS_ISSUED = ("confirmed", "consumed")

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
        self._pairing_url = BETA_PAIRING_URL
        self._pairing: PairingClient | None = None
        self._session = None
        self._pair_task: asyncio.Task | None = None
        self._result: PollResult | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step — pair with an account, API key or read-only discovery."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["pair", "api_key", "discovery_only"],
        )

    async def async_step_pair(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start pairing; advanced mode may point it at another pairing service."""
        if user_input is not None:
            url = str(user_input.get(CONF_PAIRING_URL, "")).strip()
            if not is_https_url(url):
                return self.async_show_form(step_id="pair", data_schema=self._pair_schema(url),
                                            errors={"base": "invalid_url"})
            self._pairing_url = url
        elif self.show_advanced_options:
            return self.async_show_form(step_id="pair", data_schema=self._pair_schema(BETA_PAIRING_URL))
        return await self.async_step_pair_wait()

    @staticmethod
    def _pair_schema(default: str) -> vol.Schema:
        return vol.Schema({vol.Required(CONF_PAIRING_URL, default=default): str})

    def _entries_by_kind(self) -> tuple[list, list]:
        """(wpisy konta, wpisy „tylko rozpoznanie") — ignorowane pomijamy."""
        entries = self._async_current_entries(include_ignore=False)
        discovery = [e for e in entries if e.data.get(CONF_MODE) == MODE_DISCOVERY_ONLY]
        accounts = [e for e in entries if e.data.get(CONF_MODE) != MODE_DISCOVERY_ONLY]
        return accounts, discovery

    async def async_step_pair_wait(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """External step: the owner confirms in the app or on the web page.

        Po wejściu w external step wolno zwrócić wyłącznie kolejny external step albo
        `external_step_done` — przerwania rozstrzyga dopiero `pair_finish`.
        """
        if self._result is not None:
            return self.async_external_step_done(next_step_id="pair_finish")
        if self._session is None:
            accounts, _ = self._entries_by_kind()
            if len(accounts) > 1:
                return self.async_abort(reason="multiple_accounts")
            self._pairing = PairingClient(async_get_clientsession(self.hass), self._pairing_url)
            try:
                self._session = await self._pairing.async_begin(
                    instance_id=await instance_id.async_get(self.hass),
                    instance_name=(self.hass.config.location_name or "Home Assistant"),
                    ha_version=HA_VERSION,
                    client_version=await async_integration_version(self.hass),
                )
            except PairingDisabled:
                return self.async_abort(reason="pairing_disabled")
            except PairingError as err:
                _LOGGER.debug("Volcast pairing: session not started (%s)", err)
                return self.async_abort(reason="cannot_connect")
            self._pair_task = self.hass.async_create_task(self._async_wait_for_owner())
        return self.async_external_step(step_id="pair_wait", url=self._session.connect_url)

    async def _async_wait_for_owner(self) -> None:
        try:
            self._result = await PairingPoller(
                self._pairing, self._session,
                interval_s=PAIR_POLL_INTERVAL_S, deadline_s=PAIR_DEADLINE_S,
            ).async_wait()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — kreator musi się zakończyć czytelnym powodem
            _LOGGER.exception("Volcast pairing: waiting for confirmation failed")
            self._result = PollResult("error")
        try:
            await self.hass.config_entries.flow.async_configure(flow_id=self.flow_id)
        except Exception:  # noqa: BLE001 — kreator mógł zostać zamknięty
            _LOGGER.debug("Volcast pairing: flow already gone")

    async def async_step_pair_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the account entry, or upgrade the existing one in place."""
        r = self._result
        if r is None or r.status != "confirmed" or r.backend is None or not r.api_key:
            return self.async_abort(reason=_ABORT_BY_STATUS.get(getattr(r, "status", ""), "pairing_failed"))
        now = dt_util.utcnow()
        data = {
            CONF_API_KEY: r.api_key,
            CONF_API_URL: r.backend.forecast,
            CONF_BACKEND: r.backend.as_dict(),
            CONF_USER_ID: r.user_id,
            CONF_PAIRED_AT: now.isoformat(),
            CONF_PAIRING: {
                "session_id": self._session.session_id,
                "poll_token": self._session.poll_token,
                "live_until": (now + LIVE_WINDOW).isoformat(),
                "url": self._pairing_url,
            },
        }
        accounts, discovery = self._entries_by_kind()
        target = (accounts or discovery or [None])[0]
        if target is not None:
            # Aktualizacja w miejscu: entry_id zostaje, więc encje prognozy, statystyki
            # i opcje też; wpis „tylko rozpoznanie" traci `mode` i staje się wpisem konta.
            new_data = {k: v for k, v in target.data.items() if k != CONF_MODE} | data
            self.hass.config_entries.async_update_entry(target, data=new_data, unique_id=r.api_key)
            await self.hass.config_entries.async_reload(target.entry_id)
            return self.async_abort(reason="paired_existing")
        await self.async_set_unique_id(r.api_key)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"Volcast — {self.hass.config.location_name or 'Home'}", data=data,
        )

    @callback
    def async_remove(self) -> None:
        """Kreator zamknięty albo zakończony — sesja bez odebranego klucza nie wisi 10 min."""
        if self._pair_task is not None and not self._pair_task.done():
            self._pair_task.cancel()
        issued = self._result is not None and self._result.status in _CREDENTIALS_ISSUED
        if self._session is not None and self._pairing is not None and not issued:
            self.hass.async_create_task(self._async_cancel_session())

    async def _async_cancel_session(self) -> None:
        try:
            await self._pairing.async_cancel(self._session)
        except Exception:  # noqa: BLE001 — sprzątanie; chmura i tak zamknie sesję po terminie
            _LOGGER.debug("Volcast pairing: cancel failed")

    async def async_step_api_key(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle API key entry (former "user" step, unchanged logic)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            api_url = user_input.get(CONF_API_URL, DEFAULT_API_URL).strip()

            # Shape check before any network call: catches the app's shortened
            # preview (vk_xxxx...xxxx) pasted instead of the full key.
            format_error = check_api_key_format(api_key)
            if format_error:
                errors["base"] = format_error
                return self.async_show_form(
                    step_id="api_key",
                    data_schema=STEP_USER_DATA_SCHEMA,
                    errors=errors,
                )

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
            step_id="api_key",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_discovery_only(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create an account-less entry — only read-only installation discovery."""
        # Any existing Volcast entry already runs discovery (forecast entries
        # included), so a separate discovery-only entry would only duplicate it.
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        await self.async_set_unique_id(MODE_DISCOVERY_ONLY)
        # Distinct abort reason: "already_configured" talks about an API key,
        # which this entry doesn't have.
        self._abort_if_unique_id_configured(error="single_instance_allowed")

        return self.async_create_entry(
            title="Volcast — discovery",
            data={CONF_MODE: MODE_DISCOVERY_ONLY},
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
        if self.config_entry.data.get(CONF_MODE) == MODE_DISCOVERY_ONLY:
            # Wpis bez konta nie ma żadnych opcji do skonfigurowania.
            return self.async_create_entry(data={})

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
