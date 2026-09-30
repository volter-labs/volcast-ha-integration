"""Config flow for Volcast Solar Forecast."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import timedelta
from typing import Any, Callable
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

try:
    from homeassistant.data_entry_flow import section
except ImportError:  # HA sprzed sekcji formularza — pole adresu płasko w formularzu
    section = None

from .cloud.client import Backend, PairingClient, PairingDisabled, PairingError, PollResult, is_https_url
from .control import direct_search as ds
from .control.runtime import async_control_change_allowed, async_direct_search
from .control.telemetry import TELEMETRY_FIELDS
from .core.control.caps import entity_mode_options, entity_mode_ready
from .core.control.limits import BATTERY_CAPACITY_RANGE_KWH, RATED_POWER_RANGE_W
from .core.discovery.identify import Candidate
from .core.prices import has_usable_prices_now
from .core.transports.base import check_target
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
    CONTROL_MODE_DIRECT,
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
    OPT_DIRECT_POLL_S,
    OPT_DIRECT_TARGET,
    OPT_DIRECT_TRIAL,
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
# Zwinięta sekcja formularza parowania z adresem usługi parowania.
PAIR_ADVANCED = "advanced"

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
        """Start pairing: a one-click form, then the external step.

        Formularz ma zwiniętą sekcję „zaawansowane" z adresem usługi parowania: puste
        pole = usługa domyślna. Tryb zaawansowany HA jest wycofywany (frontend nie
        przekazuje już tej flagi do kreatora), więc nadpisanie adresu żyje w sekcji
        formularza, widocznej dla każdego, ale domyślnie zwiniętej.

        External step idzie bez (przestarzałego) `step_id`, więc HA zapisuje go jako
        krok `pair` i po odpowiedzi chmury wraca TUTAJ bez danych — wtedy od razu
        do obsługi kroku zewnętrznego, nigdy do formularza.
        """
        if self._session is not None or self._result is not None:
            return await self._async_step_external()
        if self._pair_target_disabled():
            return self.async_abort(reason="existing_entry_disabled")
        if user_input is None:
            return self.async_show_form(step_id="pair", data_schema=self._pair_schema(None))
        url = _pairing_url_from(user_input)
        if url and not is_https_url(url):
            return self.async_show_form(step_id="pair", data_schema=self._pair_schema(url),
                                        errors={"base": "invalid_url"})
        self._pairing_url = url or BETA_PAIRING_URL
        return await self._async_step_external()

    @staticmethod
    def _pair_schema(url: str | None) -> vol.Schema:
        field = {vol.Optional(CONF_PAIRING_URL, description={"suggested_value": url or None}): str}
        if section is None:
            return vol.Schema(field)
        return vol.Schema({vol.Optional(PAIR_ADVANCED): section(vol.Schema(field), {"collapsed": True})})

    def _discovery_target_disabled(self) -> bool:
        """Wpis „tylko rozpoznanie" do przejęcia przez konto jest wyłączony.

        Nie przerabiamy go: właściciel dostałby „teraz używa konta", a nic by nie ruszyło,
        dopóki sam go nie włączy — lepiej od razu powiedzieć, że najpierw trzeba go włączyć.
        """
        accounts, discovery = self._entries_by_kind()
        return not accounts and bool(discovery) and bool(getattr(discovery[0], "disabled_by", None))

    def _account_target_disabled(self) -> bool:
        """The single existing account entry that pairing would update is disabled.

        Same reasoning as `_discovery_target_disabled`: updating a disabled entry in
        place would silently do nothing until the owner re-enables it — better to say
        so up front than to claim `paired_existing`.
        """
        accounts, _ = self._entries_by_kind()
        return len(accounts) == 1 and bool(getattr(accounts[0], "disabled_by", None))

    def _pair_target_disabled(self) -> bool:
        return self._discovery_target_disabled() or self._account_target_disabled()

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
        if self._pair_target_disabled():
            # Wyłączony w trakcie oczekiwania na potwierdzenie — nie przerabiamy go po cichu.
            return self.async_abort(reason="existing_entry_disabled")
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
            if self._discovery_target_disabled():
                return self.async_abort(reason="existing_entry_disabled")

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
            if self._discovery_target_disabled():
                return self.async_abort(reason="existing_entry_disabled")
            _, discovery = self._entries_by_kind()
            if discovery:
                self._async_update_and_reload_once(discovery[0], {
                    "data": {CONF_API_KEY: self._api_data[CONF_API_KEY],
                             CONF_API_URL: self._api_data[CONF_API_URL]},
                    "options": options, "unique_id": account_unique_id(self._api_data[CONF_API_KEY]),
                    "title": self._api_data["title"]})
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


def _pairing_url_from(user_input: dict[str, Any]) -> str:
    """Adres z formularza parowania: z sekcji „zaawansowane" albo płaskiego pola (starsze HA)."""
    adv = user_input.get(PAIR_ADVANCED)
    raw = adv.get(CONF_PAIRING_URL) if isinstance(adv, dict) else user_input.get(CONF_PAIRING_URL)
    return str(raw or "").strip()
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

    async def _finish(self, options: dict[str, Any], *,
                      retry_form: Callable[[dict[str, str]], ConfigFlowResult] | None = None) -> ConfigFlowResult:
        """Zapis opcji; zmiana sterowania najpierw oddaje falownik przez obecnego wykonawcę.

        Nieudany powrót blokuje zapis (nic nie zapisane: błąd formularza `retry_form` albo przerwanie),
        gdy wykonawca po przeładowaniu nie przejąłby własności — inny sposób sterowania, cel albo
        mapowanie. Przy tym samym powiązaniu (np. wyłączenie sterowania przez encje) zapis idzie, a nowy
        wykonawca ponawia powrót co cykl, dopóki sterowanie jest wyłączone.
        """
        if not await async_control_change_allowed(self._runtime(), self.config_entry.options, options):
            if retry_form is not None:
                return retry_form({"base": RESTORE_FAILED})
            return self.async_abort(reason=RESTORE_FAILED)
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
        return self.async_show_menu(
            step_id="init", menu_options=["forecast", "control", "details", "prices", "ev_charger"])

    async def async_step_forecast(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return await self._finish(self._forecast_options(user_input))
        return self.async_show_form(step_id="forecast", data_schema=self._forecast_schema())

    async def async_step_control(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        # Trzy pozycje, żadnej domyślnej; „Bezpośrednio” sprawdza dostępność dopiero po wyborze.
        return self.async_show_menu(step_id="control",
                                    menu_options=["control_entities", "control_direct", "control_off"])

    async def async_step_control_direct(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """„Bezpośrednio”: tylko z ostatniego wyszukiwania — falownik rozpoznany, próba udana, profil i jego
        ścieżka rejestrów zweryfikowane, brak innej integracji na tym adresie."""
        rt = self._runtime()
        reports = list(getattr(rt, "last_probe", None) or [])
        hits = ds.found(reports)
        # Bez rozpoznanego falownika bierzemy dowolny raport z adresem — tylko po to, żeby podać właściwy
        # powód odmowy (kolizja albo „nie znaleziono”).
        report = hits[0] if hits else next((r for r in reports if r.candidate is not None), None)
        clash: tuple[str, ...] = ()
        if report is not None and report.candidate is not None:
            clash = await ds.async_clash(self.hass, self.config_entry.entry_id, report.candidate.host)
        reason = ds.offer_reason(report, await self._async_profiles(), clash)
        if reason == ds.CONFLICT:
            other = next((d for d in clash if d != "volcast"), "unknown")
            return self.async_abort(reason=reason, description_placeholders={"integration": other})
        if reason is not None:
            return self.async_abort(reason=reason)
        target = ds.target_from_report(report)
        return await self._finish(self._merged({OPT_CONTROL_MODE: CONTROL_MODE_DIRECT, OPT_DIRECT_TARGET: target,
                                                OPT_DIRECT_TRIAL: None}))

    async def async_step_control_entities(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        rt = self._runtime()
        choice = getattr(rt, "choice", None)
        if not entity_mode_ready(choice, getattr(rt, "mapped", None) or {}):
            return self.async_abort(reason="entity_mode_unavailable")
        return await self._finish(self._merged(entity_mode_options(choice)))

    async def async_step_control_off(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._finish(self._merged({OPT_CONTROL_MODE: None}))

    async def async_step_details(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self._async_profiles()
        if user_input is not None:
            errors = self._direct_errors(user_input)
            if errors:
                return self.async_show_form(step_id="details", data_schema=self._details_schema(), errors=errors)
            tmap = {k: v for k in TELEMETRY_FIELDS if (v := (user_input.get(k) or "").strip())}
            patch = {
                OPT_TELEMETRY_MAP: tmap,
                OPT_GRID_NEGATE: bool(user_input.get(OPT_GRID_NEGATE)) or None,
                OPT_RATED_POWER_W: user_input.get(OPT_RATED_POWER_W),
                OPT_BATTERY_CAPACITY_KWH: user_input.get(OPT_BATTERY_CAPACITY_KWH),
                OPT_LOAD_ENERGY: user_input.get(OPT_LOAD_ENERGY)}
            if self._trial_offered():
                patch[OPT_DIRECT_TRIAL] = True if user_input.get(OPT_DIRECT_TRIAL) else None
            if isinstance(self.config_entry.options.get(OPT_DIRECT_TARGET), dict):
                patch[OPT_DIRECT_POLL_S] = user_input.get(OPT_DIRECT_POLL_S)
            if user_input.get(_SEARCH):
                self._pending = self._merged(patch)
                return await self.async_step_direct_search()
            return await self._finish(self._merged(patch), retry_form=lambda errors: self.async_show_form(
                step_id="details", data_schema=self._details_schema(), errors=errors))
        return self.async_show_form(step_id="details", data_schema=self._details_schema())

    # ── ładowarka EV: potwierdzenie znalezisk z wykrywania ─────────────────
    def _ev_findings(self) -> list:
        runner = (getattr(self.hass, "data", None) or {}).get(DOMAIN, {}).get(
            self.config_entry.entry_id, {}).get("discovery")
        classification = getattr(runner, "classification", None)
        return list(getattr(classification, "chargers", None) or [])

    def _ev_saved(self) -> list[dict[str, Any]]:
        saved = self.config_entry.options.get(OPT_EV_CHARGERS)
        return [c for c in saved if isinstance(c, dict) and c.get("device_id")] if isinstance(saved, list) else []

    async def async_step_ev_charger(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Zaznaczenie ładowarek do śledzenia; zapisane, a niewidoczne w ostatnim wykrywaniu, zostają na liście."""
        findings = {f.device_id: f for f in self._ev_findings()}
        saved = {c["device_id"]: c for c in self._ev_saved()}
        if not findings:
            return self.async_abort(reason=EV_NONE_FOUND)
        if user_input is not None:
            chosen = list(user_input.get(_EV_SELECT) or [])
            self._ev_done: list[dict[str, Any]] = []
            self._ev_queue = [findings[d] for d in chosen if d in findings]
            # zapisane, których wykrywanie nie widzi, zaznaczone nadal — bez zmian
            kept = {d: saved[d] for d in chosen if d in saved and d not in findings}
            self._ev_kept = kept
            self._ev_order = [d for d in chosen if d in findings or d in kept]
            return await self._async_ev_next()
        labels = {d: _ev_label(f.name, f.manufacturer, f.model, d) for d, f in findings.items()}
        labels.update({d: c.get("label") or d for d, c in saved.items() if d not in labels})
        schema = vol.Schema({vol.Optional(_EV_SELECT, default=[d for d in saved if d in labels]): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[selector.SelectOptionDict(value=d, label=n) for d, n in labels.items()],
                multiple=True, mode=selector.SelectSelectorMode.LIST))})
        return self.async_show_form(step_id="ev_charger", data_schema=schema)

    async def _async_ev_next(self) -> ConfigFlowResult:
        if self._ev_queue:
            return self._ev_roles_form(self._ev_queue[0])
        by_id = {c["device_id"]: c for c in self._ev_done}
        by_id.update(self._ev_kept)
        chargers = [by_id[d] for d in self._ev_order if d in by_id]
        return await self._finish(self._merged({OPT_EV_CHARGERS: chargers or None}))

    def _ev_roles_form(self, finding, errors: dict[str, str] | None = None) -> ConfigFlowResult:
        saved = next((c for c in self._ev_saved() if c["device_id"] == finding.device_id), None)
        current = dict(saved.get("roles") or {}) if saved else {r: v.entity_id for r, v in finding.roles.items()}
        fields: dict[Any, Any] = {}
        for role in _EV_ROLES + tuple(r for r in finding.roles if r not in _EV_ROLES):
            sel = selector.EntitySelector(selector.EntitySelectorConfig(domain=list(_EV_ROLE_DOMAINS.get(role, ("sensor",)))))
            key = (vol.Required if role == "status" else vol.Optional)(
                role, description={"suggested_value": current.get(role)})
            fields[key] = sel
        return self.async_show_form(
            step_id="ev_charger_roles", data_schema=vol.Schema(fields), errors=errors or {},
            description_placeholders={"charger": _ev_label(
                finding.name, finding.manufacturer, finding.model, finding.device_id)})

    async def async_step_ev_charger_roles(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        queue = getattr(self, "_ev_queue", None)
        if not queue:
            return self.async_abort(reason=EV_NONE_FOUND)
        finding = queue[0]
        if user_input is None:
            return self._ev_roles_form(finding)
        roles = {r: v.strip() for r, v in user_input.items() if isinstance(v, str) and v.strip()}
        if "status" not in roles:
            return self._ev_roles_form(finding, {"status": "role_required"})
        self._ev_done.append({"device_id": finding.device_id, "roles": roles,
                              "label": finding.name or finding.model or ""})
        queue.pop(0)
        return await self._async_ev_next()

    # ── połączenie bezpośrednie: wyszukiwanie, wybór, cel ręczny ──────────
    async def _async_profiles(self) -> list:
        cached = getattr(self, "_profiles_cache", None)
        if cached is None:
            job = getattr(self.hass, "async_add_executor_job", None)
            cached = await job(ds.load_profiles) if job is not None else ds.load_profiles()
            self._profiles_cache = cached
        return cached

    def _profiles_now(self) -> list:
        cached = getattr(self, "_profiles_cache", None)
        if cached is None:
            cached = self._profiles_cache = ds.load_profiles()
        return cached

    def _trial_offered(self) -> bool:
        """Połączenie próbne tylko dla celu, którego ścieżka rejestrów nie jest jeszcze zweryfikowana."""
        o = self.config_entry.options
        target = o.get(OPT_DIRECT_TARGET)
        if not isinstance(target, dict):
            return False
        if o.get(OPT_DIRECT_TRIAL) is True:
            return True                       # wyłączenie próby zawsze możliwe
        profile = next((p for p in self._profiles_now() if p.id == target.get("profile_id")), None)
        return profile is not None and profile.modbus.status == "draft"

    def _direct_errors(self, user_input: dict[str, Any]) -> dict[str, str]:
        o = self.config_entry.options
        if not (self._trial_offered() and user_input.get(OPT_DIRECT_TRIAL)):
            return {}
        if o.get(OPT_CONTROL_MODE) == CONTROL_MODE_ENTITIES:
            return {OPT_DIRECT_TRIAL: "trial_with_entities"}      # dwie drogi do jednego falownika
        executor = getattr(self._runtime(), "executor", None)
        # Próba sama nigdy nie przejmuje falownika (pisarz bez zapisu), więc włączona już próba nie jest
        # blokowana; odmawiamy tylko NOWEGO włączenia, dopóki trwa własność z wcześniejszego sterowania.
        if o.get(OPT_DIRECT_TRIAL) is not True and getattr(executor, "owned", False):
            return {OPT_DIRECT_TRIAL: "trial_while_owned"}        # najpierw powrót do trybu bazowego
        return {}

    def _pending_options(self) -> dict[str, Any]:
        pending = getattr(self, "_pending", None)
        return dict(pending) if pending is not None else dict(self.config_entry.options)

    async def async_step_direct_search(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        task = getattr(self, "_search_task", None)
        if task is None:
            task = self._search_task = self.hass.async_create_task(
                async_direct_search(self.hass, self.config_entry))
        if not task.done():
            return self.async_show_progress(step_id="direct_search", progress_action="direct_search",
                                            progress_task=task)
        try:
            self._reports = list(task.result() or [])
        except Exception:  # noqa: BLE001 — wyszukiwanie nigdy nie wywraca opcji
            self._reports = []
        self._search_task = None
        return self.async_show_progress_done(next_step_id="direct_pick")

    def _pick_labels(self) -> dict[str, str]:
        """Kandydaci do wyboru — etykiety BEZ adresu (marka, model, profil, status ścieżki rejestrów)."""
        profiles = self._profiles_now()
        labels = {str(i): ds.label(r, profiles) for i, r in enumerate(ds.found(getattr(self, "_reports", [])))}
        labels[_MANUAL] = "Enter the inverter address manually"
        return labels

    async def async_step_direct_pick(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        labels = self._pick_labels()
        if user_input is not None:
            pick = user_input.get("candidate")
            if pick == _MANUAL:
                return await self.async_step_direct_manual()
            hits = ds.found(getattr(self, "_reports", []))
            index = int(pick) if isinstance(pick, str) and pick.isdigit() else -1
            target = ds.target_from_report(hits[index]) if 0 <= index < len(hits) else None
            if target is None:
                return self.async_abort(reason=ds.NOT_FOUND)
            return await self._finish({**self._pending_options(), OPT_DIRECT_TARGET: target},
                                      retry_form=lambda errors: self._pick_form(labels, errors))
        return self._pick_form(labels, {} if len(labels) > 1 else {"base": ds.NOT_FOUND})

    def _pick_form(self, labels: dict[str, str], errors: dict[str, str]) -> ConfigFlowResult:
        return self.async_show_form(step_id="direct_pick", errors=errors,
                                    data_schema=vol.Schema({vol.Required("candidate"): vol.In(labels)}))

    async def async_step_direct_manual(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            errors: dict[str, str] = {}
            host = str(user_input.get("host") or "").strip()
            transport = user_input.get("transport")
            try:
                host = check_target(host, allow_loopback=ds.ALLOW_LOOPBACK)
            except (ValueError, TypeError):
                errors["host"] = "invalid_host"
            serial = str(user_input.get("logger_serial") or "").strip()
            logger_serial = None
            if transport == "solarman_v5":
                if _LOGGER_SERIAL.fullmatch(serial) and int(serial) <= 0xFFFFFFFF:
                    logger_serial = int(serial)
                else:
                    errors["logger_serial"] = "logger_serial_required"
            if not errors:
                port, unit = int(user_input.get("port")), int(user_input.get("unit_id"))
                reports = await async_direct_search(
                    self.hass, self.config_entry, manual=Candidate(host, "manual", logger_serial,
                                                                   transports=(transport,)),
                    port=port, unit_id=unit)
                hits = ds.found(reports)
                target = ds.target_from_report(hits[0]) if hits else None
                if target is None:
                    errors["base"] = ds.NOT_FOUND           # cel ręczny też musi przejść sondę (odcisk urządzenia)
                else:
                    target.update(port=port, unit_id=unit)
                    return await self._finish(
                        {**self._pending_options(), OPT_DIRECT_TARGET: target},
                        retry_form=lambda errors: self.async_show_form(
                            step_id="direct_manual", data_schema=self._manual_schema(), errors=errors))
            return self.async_show_form(step_id="direct_manual", data_schema=self._manual_schema(), errors=errors)
        return self.async_show_form(step_id="direct_manual", data_schema=self._manual_schema())

    def _manual_schema(self) -> vol.Schema:
        transports = sorted({k for p in self._profiles_now() for k in p.modbus.transport_options})
        return vol.Schema({
            vol.Required("host"): str,
            vol.Required("transport"): vol.In(transports),
            vol.Required("port"): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
            vol.Required("unit_id"): vol.All(vol.Coerce(int), vol.Range(min=0, max=247)),
            vol.Optional("logger_serial"): str,
        })

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
        # Połączenie bezpośrednie: wyszukanie falownika, próba (tylko ścieżka rejestrów w wersji testowej),
        # okres odczytu (gdy jest cel).
        fields[vol.Optional(_SEARCH, default=False)] = bool
        if self._trial_offered():
            fields[vol.Optional(OPT_DIRECT_TRIAL, default=o.get(OPT_DIRECT_TRIAL) is True)] = bool
        if isinstance(o.get(OPT_DIRECT_TARGET), dict):
            poll = vol.Optional(OPT_DIRECT_POLL_S, description={"suggested_value": o.get(OPT_DIRECT_POLL_S)})
            fields[poll] = vol.All(vol.Coerce(int), vol.Range(min=5, max=60))
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


_SEARCH = "direct_search"
OPT_EV_CHARGERS = "ev_chargers"
EV_NONE_FOUND = "no_ev_chargers"
_EV_SELECT = "chargers"
_EV_ROLES = ("status", "setpoint", "start_stop", "power", "energy")
_EV_ROLE_DOMAINS = {
    "status": ("sensor", "binary_sensor", "select"), "setpoint": ("number",),
    "start_stop": ("switch", "select"), "start": ("button",), "stop": ("button",),
    "power": ("sensor",), "energy": ("sensor",)}


def _ev_label(name: str | None, manufacturer: str | None, model: str | None, device_id: str) -> str:
    """Nazwa ładowarki na liście: nazwa urządzenia albo producent i model, na końcu identyfikator."""
    return name or " ".join(x for x in (manufacturer, model) if x) or device_id
_MANUAL = "manual"
RESTORE_FAILED = "restore_failed"
_LOGGER_SERIAL = re.compile(r"\d{1,10}")


def _entity_field(key: str, saved: Any) -> vol.Optional:
    """Pole encji opcjonalne: zapisana wartość jako podpowiedź (nie default) — da się wyczyścić."""
    return vol.Optional(key, description={"suggested_value": saved or None})


class CannotConnect(Exception):
    """Error to indicate we cannot connect."""


class InvalidAuth(Exception):
    """Error to indicate invalid auth."""
