"""Config flow for Volcast Solar Forecast."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

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

from .cloud.client import Backend, PairingClient, PairingDisabled, PairingError, PollResult, is_https_url
from .control.runtime import async_restore_if_control_changed
from .control.telemetry import TELEMETRY_FIELDS
from .core.control.caps import entity_mode_options, entity_mode_ready
from .core.control.limits import BATTERY_CAPACITY_RANGE_KWH, RATED_POWER_RANGE_W
from .core.prices import has_usable_prices_now
from .key_format import account_unique_id, check_api_key_format
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
    CONTROL_MODE_ENTITIES,
    DEFAULT_API_URL,
    DEFAULT_PEAK_THRESHOLD,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    MODE_DISCOVERY_ONLY,
    OPT_BATTERY_CAPACITY_KWH,
    OPT_CONTROL_MODE,
    OPT_GRID_NEGATE,
    OPT_INVERTER_DOMAIN,
    OPT_LOAD_ENERGY,
    OPT_PRICE_BUY,
    OPT_PRICE_CURRENCY,
    OPT_PRICE_SELL,
    OPT_PROFILE_ID,
    OPT_RATED_POWER_W,
    OPT_TELEMETRY_MAP,
)

_LOGGER = logging.getLogger(__name__)

PAIR_POLL_INTERVAL_S = 3.0
# Chmura zamyka niepotwierdzoną sesję po 10 min (poll → 410). Lokalny termin to tylko
# siatka bezpieczeństwa — z zapasem, żeby potwierdzenie z ostatnich sekund nie przepadło.
PAIR_DEADLINE_S = 630.0
# Okno postępu i wyborów po potwierdzeniu (chmura liczy je od potwierdzenia).
LIVE_WINDOW = timedelta(minutes=30)
_ABORT_BY_STATUS = {"expired": "pairing_expired", "gone": "pairing_expired", "disabled": "pairing_disabled",
                    "error": "pairing_connection_lost"}
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
        """Start pairing; advanced mode may point it at another pairing service.

        External step idzie bez (przestarzałego) `step_id`, więc HA zapisuje go jako
        krok `pair` i po odpowiedzi chmury wraca TUTAJ bez danych — wtedy od razu
        do obsługi kroku zewnętrznego, nigdy do formularza adresu.
        """
        if self._session is not None or self._result is not None:
            return await self._async_step_external()
        if user_input is not None:
            url = str(user_input.get(CONF_PAIRING_URL, "")).strip()
            if not is_https_url(url):
                return self.async_show_form(step_id="pair", data_schema=self._pair_schema(url),
                                            errors={"base": "invalid_url"})
            self._pairing_url = url
        elif self._advanced_requested():
            return self.async_show_form(step_id="pair", data_schema=self._pair_schema(BETA_PAIRING_URL))
        return await self._async_step_external()

    def _advanced_requested(self) -> bool:
        """Formularz adresu tylko na jawną prośbę frontendu (tryb zaawansowany użytkownika).

        Bieżące HA wycofuje tryb zaawansowany i `show_advanced_options` zwraca True dla
        każdego — formularz zmiany usługi parowania pokazywałby się wszystkim.
        """
        context = getattr(self, "context", None)
        return isinstance(context, dict) and context.get("show_advanced_options") is True

    @staticmethod
    def _pair_schema(default: str) -> vol.Schema:
        return vol.Schema({vol.Required(CONF_PAIRING_URL, default=default): str})

    def _entries_by_kind(self) -> tuple[list, list]:
        """(wpisy konta, wpisy „tylko rozpoznanie") — ignorowane pomijamy."""
        entries = self._async_current_entries(include_ignore=False)
        discovery = [e for e in entries if e.data.get(CONF_MODE) == MODE_DISCOVERY_ONLY]
        accounts = [e for e in entries if e.data.get(CONF_MODE) != MODE_DISCOVERY_ONLY]
        return accounts, discovery

    async def _async_step_external(self) -> ConfigFlowResult:
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
        return self.async_external_step(url=self._session.connect_url)

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
            self._result = PollResult("failed")
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
        unique_id = account_unique_id(r.api_key)
        title = f"Volcast — {self.hass.config.location_name or 'Home'}"
        accounts, discovery = self._entries_by_kind()
        if accounts or discovery:
            target = (accounts or discovery)[0]
            # Aktualizacja w miejscu: entry_id zostaje, więc encje prognozy, statystyki
            # i opcje też; wpis „tylko rozpoznanie" traci `mode` i staje się wpisem konta.
            changes: dict[str, Any] = {
                "data": {k: v for k, v in target.data.items() if k != CONF_MODE} | data,
                "unique_id": unique_id,
            }
            if not accounts:
                changes["title"] = title
            self._async_update_and_reload_once(target, changes)
            return self.async_abort(reason="paired_existing")
        # Krok końcowy jest rozstrzygający — równoległy kreator klucza nie może go przerwać.
        await self.async_set_unique_id(unique_id, raise_on_progress=False)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=title, data=data)

    def _async_update_and_reload_once(self, entry: ConfigEntry, changes: dict[str, Any]) -> None:
        """Jedno przeładowanie po aktualizacji wpisu.

        Uruchomiony wpis konta ma listener aktualizacji, który sam przeładowuje wpis —
        HA każe wtedy polegać na nim (`async_update_reload_and_abort` z listenerem
        ostrzega, a w przyszłych wersjach odmawia). Bez listenera (wpis „tylko
        rozpoznanie", wpis czekający na ponowienie) przeładowanie planujemy sami,
        nie czekając na nie w kroku kreatora.
        """
        ce = self.hass.config_entries
        has_listener = bool(getattr(entry, "update_listeners", None))
        changed = ce.async_update_entry(entry, **changes)
        if has_listener and changed:
            return
        schedule = getattr(ce, "async_schedule_reload", None)
        if schedule is not None:
            schedule(entry.entry_id)
        else:  # HA sprzed async_schedule_reload
            self.hass.async_create_task(ce.async_reload(entry.entry_id))

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

            if any(e.data.get(CONF_API_KEY) == api_key
                   for e in self._async_current_entries(include_ignore=False)):
                # Wpis z tym kluczem (także sprzed skrótu w unique_id).
                return self.async_abort(reason="already_configured")

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
                await self.async_set_unique_id(account_unique_id(api_key))
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
        entries = self._async_current_entries()
        # A disabled entry cannot be reached to add an account to later, and the
        # generic "single instance" message below would not say why. Point the
        # owner at the actual, fixable cause instead.
        if entries and all(getattr(e, "disabled_by", None) for e in entries):
            return self.async_abort(reason="existing_entry_disabled")
        # Any existing Volcast entry already runs discovery (forecast entries
        # included), so a separate discovery-only entry would only duplicate it.
        if entries:
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
            # An existing account-less (discovery-only) entry becomes redundant the
            # moment an account is added — update it in place instead of running
            # both side by side.
            _, discovery = self._entries_by_kind()
            if discovery:
                target = discovery[0]
                self.hass.config_entries.async_update_entry(
                    target,
                    data={CONF_API_KEY: self._api_data[CONF_API_KEY], CONF_API_URL: self._api_data[CONF_API_URL]},
                    options=options, unique_id=self._api_data[CONF_API_KEY], title=self._api_data["title"])
                await self.hass.config_entries.async_reload(target.entry_id)
                return self.async_abort(reason="converted_existing")
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


_FORECAST_KEYS = (CONF_UPDATE_INTERVAL, CONF_PEAK_THRESHOLD, CONF_PV_ENERGY_ENTITY, CONF_PV_POWER_ENTITY,
                  CONF_BATTERY_SOC_ENTITY, CONF_BATTERY_CHARGE_POWER_ENTITY)
_EMPTY = (None, "", {})
_CURRENCY = re.compile(r"[A-Z]{3}")


def _clean(values: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in values.items() if not any(v is e or v == e for e in _EMPTY)}


class VolcastOptionsFlow(OptionsFlowWithConfigEntry):
    """Opcje: wpis bez konta — jeden formularz prognozy; wpis sparowany — menu.

    Sposób sterowania nie ma wartości domyślnej: tryb encji tylko na wyraźny wybór.
    Zmiana, która zmienia sterowanie (sposób, profil, integracja falownika), najpierw
    przywraca tryb bazowy przez OBECNEGO wykonawcę — dopiero potem zapis opcji i
    przeładowanie wpisu.
    """

    def _paired(self) -> bool:
        return Backend.from_dict(self.config_entry.data.get(CONF_BACKEND)) is not None

    def _merged(self, patch: dict[str, Any]) -> dict[str, Any]:
        out = dict(self.config_entry.options)
        for key, value in patch.items():
            if any(value is e or value == e for e in _EMPTY):
                out.pop(key, None)
            else:
                out[key] = value
        return out

    def _runtime(self):
        data = getattr(self.hass, "data", None) or {}
        return (data.get(DOMAIN, {}).get(self.config_entry.entry_id) or {}).get("control")

    async def _finish(self, options: dict[str, Any]) -> ConfigFlowResult:
        """Zapis opcji; zmiana sterowania najpierw oddaje falownik przez obecnego wykonawcę.

        Nieudany powrót nie blokuje zapisu i jest bezpieczny: wykonawca zostaje właścicielem
        (migawka i powiązanie z tym samym profilem i encją trybu), a nowy po przeładowaniu
        ponawia powrót co cykl, dopóki sterowanie jest wyłączone.
        """
        await async_restore_if_control_changed(self._runtime(), self.config_entry.options, options)
        return self.async_create_entry(data=options)

    def _forecast_options(self, user_input: dict[str, Any]) -> dict[str, Any]:
        """Formularz prognozy posiada swoje klucze: pominięty = wyczyszczony, reszta zostaje."""
        kept = {k: v for k, v in self.config_entry.options.items() if k not in _FORECAST_KEYS}
        return {**kept, **_clean(user_input)}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage the options."""
        if self.config_entry.data.get(CONF_MODE) == MODE_DISCOVERY_ONLY:
            # Wpis bez konta nie ma żadnych opcji do skonfigurowania.
            return self.async_create_entry(data={})
        if not self._paired():
            if user_input is not None:
                return await self._finish(self._forecast_options(user_input))
            return self.async_show_form(step_id="init", data_schema=self._forecast_schema())
        return self.async_show_menu(step_id="init", menu_options=["forecast", "control", "details", "prices"])

    async def async_step_forecast(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return await self._finish(self._forecast_options(user_input))
        return self.async_show_form(step_id="forecast", data_schema=self._forecast_schema())

    async def async_step_control(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        # Dwie pozycje, żadnej domyślnej. Połączenie bezpośrednie nie jest dostępne w tej wersji.
        return self.async_show_menu(step_id="control", menu_options=["control_entities", "control_off"])

    async def async_step_control_entities(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        rt = self._runtime()
        choice = getattr(rt, "choice", None)
        if not entity_mode_ready(choice, getattr(rt, "mapped", None) or {}):
            return self.async_abort(reason="entity_mode_unavailable")
        return await self._finish(self._merged(entity_mode_options(choice)))

    async def async_step_control_off(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._finish(self._merged({OPT_CONTROL_MODE: None}))

    async def async_step_details(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            tmap = {k: v for k in TELEMETRY_FIELDS if (v := (user_input.get(k) or "").strip())}
            return await self._finish(self._merged({
                OPT_TELEMETRY_MAP: tmap,
                OPT_GRID_NEGATE: bool(user_input.get(OPT_GRID_NEGATE)) or None,
                OPT_RATED_POWER_W: user_input.get(OPT_RATED_POWER_W),
                OPT_BATTERY_CAPACITY_KWH: user_input.get(OPT_BATTERY_CAPACITY_KWH),
                OPT_LOAD_ENERGY: user_input.get(OPT_LOAD_ENERGY)}))
        return self.async_show_form(step_id="details", data_schema=self._details_schema())

    async def async_step_prices(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            saved = self.config_entry.options
            buy = (user_input.get(OPT_PRICE_BUY) or "").strip()
            sell = (user_input.get(OPT_PRICE_SELL) or "").strip()
            currency = (user_input.get(OPT_PRICE_CURRENCY) or "").strip().upper() or None
            errors: dict[str, str] = {}
            if currency is not None and not _CURRENCY.fullmatch(currency):
                errors[OPT_PRICE_CURRENCY] = "currency_invalid"
            # Błąd tylko dla encji WYBRANEJ teraz: zapisana, chwilowo bez pełnej serii (np. przed
            # publikacją cen na jutro), nie blokuje zapisu pozostałych pól ani jej wyczyszczenia.
            for key, eid in ((OPT_PRICE_BUY, buy), (OPT_PRICE_SELL, sell)):
                if eid and eid != saved.get(key) and not self._price_usable(eid, currency):
                    errors[key] = "prices_not_usable"
            if errors:
                return self.async_show_form(step_id="prices", data_schema=self._prices_schema(), errors=errors)
            return await self._finish(self._merged({
                OPT_PRICE_BUY: buy or None, OPT_PRICE_SELL: sell or None, OPT_PRICE_CURRENCY: currency}))
        return self.async_show_form(step_id="prices", data_schema=self._prices_schema())

    # ── ceny ──────────────────────────────────────────────────────────────
    def _price_usable(self, entity_id: str, currency: str | None) -> bool:
        """Encja ceny daje pełną serię JUŻ TERAZ (ta sama reguła co w onboardingu)."""
        st = self.hass.states.get(entity_id)
        if st is None:
            return False
        try:
            return has_usable_prices_now(st.attributes, currency, ZoneInfo(self.hass.config.time_zone),
                                         dt_util.utcnow())
        except Exception:  # noqa: BLE001 — zła encja = nieużywalna
            return False

    def _usable_price_entities(self) -> list[str]:
        currency = (self.config_entry.options.get(OPT_PRICE_CURRENCY) or "").strip().upper() or None
        try:
            states = self.hass.states.async_all("sensor")
        except Exception:  # noqa: BLE001
            return []
        return sorted(st.entity_id for st in states
                      if st is not None and self._price_usable(st.entity_id, currency))

    # ── schematy ──────────────────────────────────────────────────────────
    def _forecast_schema(self) -> vol.Schema:
        o = self.config_entry.options

        def sensor(key: str, device_class: str):
            # Podpowiedź, nie wartość domyślna: wyczyszczone pole zostaje puste (HA wstawiłby default).
            return (_entity_field(key, o.get(key)),
                    selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", device_class=device_class)))

        fields: dict = {
            vol.Optional(CONF_UPDATE_INTERVAL, default=o.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)):
                vol.All(int, vol.Range(min=15, max=1440)),
            vol.Optional(CONF_PEAK_THRESHOLD, default=o.get(CONF_PEAK_THRESHOLD, DEFAULT_PEAK_THRESHOLD)):
                vol.All(int, vol.Range(min=50, max=100)),
        }
        for key, dc in ((CONF_PV_ENERGY_ENTITY, "energy"), (CONF_PV_POWER_ENTITY, "power"),
                        (CONF_BATTERY_SOC_ENTITY, "battery"), (CONF_BATTERY_CHARGE_POWER_ENTITY, "power")):
            k, v = sensor(key, dc)
            fields[k] = v
        return vol.Schema(fields)

    def _details_schema(self) -> vol.Schema:
        o = self.config_entry.options
        tmap = o.get(OPT_TELEMETRY_MAP) or {}
        fields: dict = {
            _entity_field(key, tmap.get(key)): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
            for key in TELEMETRY_FIELDS
        }
        fields[vol.Optional(OPT_GRID_NEGATE, default=bool(o.get(OPT_GRID_NEGATE)))] = bool
        rated = vol.Optional(OPT_RATED_POWER_W, description={"suggested_value": o.get(OPT_RATED_POWER_W)})
        fields[rated] = vol.All(vol.Coerce(int), vol.Range(min=int(RATED_POWER_RANGE_W[0]),
                                                                 max=int(RATED_POWER_RANGE_W[1])))
        cap = vol.Optional(OPT_BATTERY_CAPACITY_KWH,
                           description={"suggested_value": o.get(OPT_BATTERY_CAPACITY_KWH)})
        fields[cap] = vol.All(vol.Coerce(float), vol.Range(min=BATTERY_CAPACITY_RANGE_KWH[0],
                                                           max=BATTERY_CAPACITY_RANGE_KWH[1]))
        fields[_entity_field(OPT_LOAD_ENERGY, o.get(OPT_LOAD_ENERGY))] = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class="energy"))
        return vol.Schema(fields)

    def _prices_schema(self) -> vol.Schema:
        o = self.config_entry.options
        usable = self._usable_price_entities()
        buy_cfg = {"domain": "sensor"}
        if usable:
            # Encje, które już teraz dają pełną serię cen — i zapisana, żeby dało się zapisać
            # resztę formularza, gdy chwilowo jej brakuje pełnej serii.
            saved = o.get(OPT_PRICE_BUY)
            buy_cfg["include_entities"] = sorted({*usable, *([saved] if saved else [])})
        return vol.Schema({
            _entity_field(OPT_PRICE_BUY, o.get(OPT_PRICE_BUY)):
                selector.EntitySelector(selector.EntitySelectorConfig(**buy_cfg)),
            _entity_field(OPT_PRICE_SELL, o.get(OPT_PRICE_SELL)):
                selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            vol.Optional(OPT_PRICE_CURRENCY, description={"suggested_value": o.get(OPT_PRICE_CURRENCY)}): str,
        })


def _entity_field(key: str, saved: Any) -> vol.Optional:
    """Pole encji opcjonalne: zapisana wartość jako podpowiedź (nie default) — da się wyczyścić."""
    return vol.Optional(key, description={"suggested_value": saved or None})


class CannotConnect(Exception):
    """Error to indicate we cannot connect."""


class InvalidAuth(Exception):
    """Error to indicate invalid auth."""
