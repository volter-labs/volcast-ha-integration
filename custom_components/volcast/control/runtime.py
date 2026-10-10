"""Złożenie sterowania dla wpisu sparowanego z kontem (część zależna od HA).

Zmiana opcji sterowania (sposób sterowania, profil, integracja falownika — od nich
zależy mapowanie encji) przeładowuje wpis. Nowy wykonawca nie może bezpiecznie
przywrócić migawki przez NOWE mapowanie, więc powrót do trybu bazowego robi STARY
wykonawca, zanim wpis się przeładuje: `async_restore_if_control_changed`. Woła go
przepływ opcji przed zapisem i słuchacz aktualizacji wpisu przed przeładowaniem
(względem kopii opcji z chwili złożenia — `ControlRuntime.options_at_setup`). Przepływ opcji
odmawia zapisu, gdy powrót się nie udał, a nowy wykonawca nie przejąłby własności
(`async_control_change_allowed`). Gdy mimo to magazyn ma własność innego sposobu sterowania
(np. opcje zmienione inną drogą), setup składa najpierw wykonawcę TAMTEGO sposobu — tylko do
powrotu; rekord zostaje, dopóki powrót nie dojdzie, potem przeładowanie wpisu.

Kolejni wykonawcy tego samego wpisu dzielą jedną blokadę zapisu: po przeładowaniu nowy
czeka, aż stary skończy zapis w toku, także gdy `async_stop` starego się poddał.
Zaraz po złożeniu idzie jeden cykl (zmiana opcji = przeładowanie = cykl od razu), a po
każdym odświeżeniu planu — następny (zmiana zgody działa od razu).

Błąd złożenia za `executor.async_start()` zatrzymuje wykonawcę i telemetrię, zanim
wyjątek pójdzie dalej — nieudany setup ani przeładowanie nie zostawia żywego wykonawcy.

Tryb bezpośredni (`control_mode == "direct"` albo `direct_trial`, oba z `direct_target`): wykonawca
dostaje `DirectIO` na połączeniu `DirectConnection` (start w tle: kolizje i tożsamość mogą trwać).
Kolejność zatrzymania: wykonawca (z powrotem przy wyłączeniu/usunięciu wpisu) → połączenie
(zwolnienie hosta w `direct_hosts`). Zmiana celu albo trybu próbnego to zmiana sterowania — powrót
idzie przez STARE połączenie przed przeładowaniem.

Drugi sterownik (`conflicts.ConflictMonitor`, `ControlRuntime.conflicts`): lista `conflicts` obok
`recommendation` i `verification` (`control_state_payload`); dowody adresu z rekomendacji i połączenia
bezpośredniego, klient na łączu z połączenia, Box z planu (`async_set_box_active`). Wybór właściciela
z chmury (`async_apply_controller_choice`): `volcast` — koniec trybu „tylko plan”, obecne konflikty
potwierdzone (nie zatrzymują już drabiny; naprawa trwa do końca dowodu), drabina po trybie „tylko plan”
rusza od szczebla startowego, a zatrzymana konfliktem — ponownie od szczebla stopu; pauza przejęcia się
kończy; `own_ems` — tryb „tylko plan” w magazynie, drabina odstawiona po cichu (`idle`), powrót do trybu
bazowego i opcja sterowania wyłączona (ta sama droga co `control_off` w opcjach: powrót przez obecnego
wykonawcę, potem przeładowanie), bez zgłoszeń o konflikcie.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Callable, Mapping

import homeassistant.util.dt as dt_util
from homeassistant.const import CONF_API_KEY, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from ..cloud import control_choice
from ..cloud.client import Backend, PairingClient, PairingSession, VolcastCloud
from ..cloud.fetcher import SCHEDULE_FETCH_INTERVAL_S, ScheduleFetcher
from ..cloud.signal_channel import SignalChannel
from ..const import (BETA_PAIRING_URL, CONF_BACKEND, CONF_PAIRING, CONF_PV_ENERGY_ENTITY, CONTROL_MODE_DIRECT,
                     CONTROL_MODE_ENTITIES, DIRECT_POLL_S, DOMAIN, OPT_BATTERY_CAPACITY_KWH, OPT_CONTROL_MODE, OPT_DIRECT_POLL_S,
                     OPT_DIRECT_TARGET, OPT_DIRECT_TRIAL, OPT_GRID_NEGATE, OPT_INVERTER_DOMAIN,
                     OPT_LOAD_ENERGY, OPT_PROFILE_ID, OPT_RATED_POWER_W, OPT_TELEMETRY_MAP,
                     SIGNAL_CONTROL_STATE_UPDATED, SIGNAL_DISCOVERY_UPDATED)
from ..core.control.caps import direct_capabilities, entity_mode_options, entity_mode_ready
from ..core.control.limits import executor_limits, rated_power_from_model
from ..core.control.ladder import CONTROLLER_CONFLICT, STOPPED
from ..core.control.recommend import recommend
from ..core.control.select import InverterHint, ProfileChoice, select_profile
from ..core.discovery.known import INVERTER_DOMAINS
from ..core.entity_map import EntityCandidate, resolve_entities
from ..core.modbus.views import UNCORRELATED_KINDS
from ..registry_compat import all_devices
from . import direct_search as ds
from .device_io import DirectIO, EntityIO
from .direct import DirectConnection
from .conflicts import ConflictMonitor
from .executor import VolcastExecutor
from .ha_writer import EntityServiceWriter
from .history_import import async_import_history_once
from .live import LiveSender
from .signals_hub import SignalsHub
from .store import ControlStore, async_installation_salt
from .telemetry import TelemetrySender, control_block
from .verification import VerificationRunner, default_params, start_rung_for

try:
    from homeassistant.loader import async_get_integration
except ImportError:  # atrapy w testach nie mają loadera
    async_get_integration = None

_LOGGER = logging.getLogger(__name__)
# W prawdziwym HA stała z rejestru zgłoszeń; atrapa testowa jej nie ma.
_ISSUE_WARNING = getattr(getattr(ir, "IssueSeverity", None), "WARNING", "warning")
ONBOARDING_KEY = "volcast_onboarding"
# Blokady zapisu per wpis — wspólne dla kolejnych wykonawców (przeładowania).
_LOCKS_KEY = "volcast_control_locks"

# Opcje, od których zależą: czy sterujemy i przez które encje.
CONTROL_OPTION_KEYS = (OPT_CONTROL_MODE, OPT_PROFILE_ID, OPT_INVERTER_DOMAIN, OPT_DIRECT_TARGET, OPT_DIRECT_TRIAL)
_POLL_RANGE_S = (5.0, 60.0)
# Opcje, których zmiana nie wymaga przeładowania wpisu (wystarczy import historii).
RELOAD_FREE_KEYS = frozenset({OPT_LOAD_ENERGY})
# Wybór sterownika z chmury (`control_choice.controller`).
CONTROLLER_VOLCAST, CONTROLLER_OWN_EMS = "volcast", "own_ems"


URGENT_FLUSH_INTERVAL_S = 10.0


class FlushLimiter:
    """Natychmiastowa telemetria po zmianie stanu sterowania: najwyżej jedna na `interval_s`; zmiany w oknie
    zlewają się w jedną wysyłkę po jego końcu (najnowszy stan)."""

    def __init__(self, hass, flush, *, interval_s: float = URGENT_FLUSH_INTERVAL_S, clock=None) -> None:
        self._hass, self._flush, self._interval = hass, flush, interval_s
        self._clock = clock or time.monotonic
        self._last: float | None = None
        self._pending = False
        self._timer = None

    @callback
    def request(self) -> None:
        if self._pending:
            return
        self._pending = True
        wait = 0.0 if self._last is None else max(0.0, self._last + self._interval - self._clock())
        if wait <= 0:
            self._start()
        else:
            self._timer = self._hass.loop.call_later(wait, self._start)

    def _start(self) -> None:
        self._timer = None
        self._hass.async_create_background_task(self._run(), "volcast_control_telemetry")

    async def _run(self) -> None:
        self._pending = False
        self._last = self._clock()
        await self._flush()

    def cancel(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._pending = False


@dataclass
class ControlRuntime:
    executor: object
    fetcher: object
    telemetry: object
    cloud: object
    choice: object | None
    mapped: dict[str, str]
    rated_power_w: float | None
    unsubs: list = field(default_factory=list)
    # kopia `entry.options` z chwili złożenia — słuchacz aktualizacji porównuje z nią
    options_at_setup: dict = field(default_factory=dict)
    # encje wpisu konfiguracji / urządzenia zmapowanego falownika (`inverter_entity_ids`)
    inverter_entities: frozenset = frozenset()
    # połączenie bezpośrednie z falownikiem (tryb bezpośredni albo próba), inaczej None
    direct: object | None = None
    # ostatnie wyszukiwanie falownika (raporty sondy) — opcje i onboarding oceniają z niego „Bezpośrednio”
    last_probe: list = field(default_factory=list)
    # sygnały z chmury: kanał (wss), nadajnik „na żywo” i hub łączący je z pobieraniem planu
    channel: object | None = None
    live: object | None = None
    hub: object | None = None
    # raport rozpoznania (getter runnera) i rekomendacja ścieżki sterowania z niego i z `last_probe`
    report: Callable[[], dict | None] | None = None
    recommendation: object | None = None
    recommendation_gen: int = 0           # numer ostatniego przeliczenia (starsze wyniki odrzucane)
    # drabina weryfikacji urządzenia (`VerificationRunner`); blok `verification` = `verification.payload()`
    verification: object | None = None
    # drugi sterownik (`ConflictMonitor`); blok `conflicts` = `conflicts.conflicts()`
    conflicts: object | None = None
    hass: object | None = None
    entry: object | None = None
    _meta_dirty: bool = False

    def control_state_payload(self) -> dict:
        """Stan sterowania dla chmury: `recommendation`, `verification` (gdy są) i `conflicts` (zawsze)."""
        out: dict = {}
        if self.recommendation is not None:
            out["recommendation"] = self.recommendation.to_payload()
        ver = self.verification.payload() if self.verification is not None else None
        if ver is not None:
            out["verification"] = ver
        if self.conflicts is not None:
            out["conflicts"] = self.conflicts.conflicts()
        else:
            out["conflicts"] = self.recommendation.conflicts_payload() if self.recommendation is not None else []
        return out

    def control_block(self) -> dict:
        """Blok `driver.control` (z `seq`) dla telemetrii; `seq` rośnie tylko przy zmianie treści."""
        meta = self.executor.control_meta
        before = dict(meta)
        block = control_block(self.control_state_payload(), meta.get("ack"), meta, dt_util.utcnow().timestamp())
        self._meta_dirty = self._meta_dirty or meta != before
        return block

    async def async_persist_control_meta(self) -> None:
        """Trwały zapis licznika po przyjętej telemetrii (restart nie cofa `seq`)."""
        if self._meta_dirty:
            self._meta_dirty = False
            await self.executor.async_save_control_meta()

    def notify_control_state(self) -> None:
        """Zmiana stanu sterowania → natychmiastowa telemetria (przez limit)."""
        if self.hass is not None and self.entry is not None:
            async_dispatcher_send(self.hass, SIGNAL_CONTROL_STATE_UPDATED.format(entry_id=self.entry.entry_id))

    async def async_apply_path_choice(self, path: str) -> str:
        """Ścieżka sterowania wybrana zdalnie (chmura albo sesja parowania): "applied"; "ignored" (nie da się
        jej teraz zastosować); "restore_failed" (jak w `async_apply_controller_choice`). `plan_only` = własny
        sterownik: tryb „tylko plan” i sterowanie wyłączone."""
        if path == "plan_only":
            return await self.async_apply_controller_choice(CONTROLLER_OWN_EMS)
        entry, ex = self.entry, self.executor
        if path == CONTROL_MODE_ENTITIES:
            if not entity_mode_ready(self.choice, self.mapped):
                return "ignored"
            patch = entity_mode_options(self.choice)
        elif path == CONTROL_MODE_DIRECT:
            hits = ds.found(self.last_probe)
            if not hits:
                return "ignored"
            profiles = await self.hass.async_add_executor_job(ds.load_profiles)
            clash = await ds.async_clash(self.hass, entry.entry_id, hits[0].candidate.host)
            if ds.offer_reason(hits[0], profiles, clash) is not None:
                return "ignored"
            patch = {OPT_CONTROL_MODE: CONTROL_MODE_DIRECT, OPT_DIRECT_TARGET: ds.target_from_report(hits[0]),
                     OPT_DIRECT_TRIAL: None}
        else:
            return "ignored"
        old = dict(entry.options)
        new = {k: v for k, v in {**old, **patch}.items() if v is not None}
        changed = control_options_changed(old, new)
        if changed and not await async_control_change_allowed(self, old, new):
            _LOGGER.debug("Volcast control: path %s refused (the return to the baseline failed)", path)
            return "ignored"
        if ex.plan_only:
            await ex.async_set_plan_only(False)
        if changed:
            self.hass.config_entries.async_update_entry(entry, options=new)
        _LOGGER.info("Volcast control: path %s chosen", path)
        return "applied"

    def report_choice_error(self) -> None:
        """Decyzja z chmury nie dała się zastosować mimo ponowień — to samo zgłoszenie co błąd sterowania."""
        ir.async_create_issue(self.hass, DOMAIN, f"control_error_{self.entry.entry_id}", is_fixable=False,
                              severity=getattr(getattr(ir, "IssueSeverity", None), "WARNING", "warning"), translation_key="control_error")

    async def async_set_box_active(self, active: bool) -> None:
        """`box_active` z planu → konflikt `box`."""
        if self.conflicts is not None:
            await self.conflicts.async_set_box_active(active)

    async def async_apply_controller_choice(self, controller: str) -> str:
        """Wybór sterownika: "applied"; "ignored" (nieznana wartość); "restore_failed" (`own_ems`: tryb
        „tylko plan” włączony, ale powrót do trybu bazowego się nie udał — opcje bez zmian, każdy cykl
        go ponawia)."""
        ex = self.executor
        if controller == CONTROLLER_OWN_EMS:
            await ex.async_set_plan_only(True)
            if self.verification is not None:
                await self.verification.async_park()            # po cichu: bez stopu i bez pusha
            if self.conflicts is not None:
                await self.conflicts.async_refresh()            # zgłoszenie o konflikcie znika
            entry = self.entry
            old = dict(entry.options)
            new = {k: v for k, v in old.items() if k != OPT_CONTROL_MODE}
            if control_options_changed(old, new):
                if not await async_control_change_allowed(self, old, new):
                    _LOGGER.warning("Volcast control: own controller chosen, but the return to the baseline "
                                    "failed — retrying every cycle")
                    return "restore_failed"
                self.hass.config_entries.async_update_entry(entry, options=new)
            elif getattr(ex, "owned", False):
                await ex.async_restore_now()
            _LOGGER.info("Volcast control: own controller chosen — plan only, no writes")
            return "applied"
        if controller == CONTROLLER_VOLCAST:
            was_plan_only = ex.plan_only
            if was_plan_only:
                await ex.async_set_plan_only(False)
            if self.conflicts is not None:
                await self.conflicts.async_acknowledge()
            lad = getattr(self.verification, "ladder", None)
            if was_plan_only and self.verification is not None:
                await self.verification.async_restart()          # od szczebla startowego
            elif lad is not None and lad.state.state == STOPPED and lad.state.stop_reason == CONTROLLER_CONFLICT:
                await self.verification.async_retry()
            if ex.paused:
                await ex.async_resume_control()
            _LOGGER.info("Volcast control: Volcast chosen as the controller")
            return "applied"
        return "ignored"


# Pola celu, które wyznaczają połączenie i urządzenie; odświeżone możliwości z ponownej sondy to nie zmiana.
_TARGET_IDENTITY = ("profile_id", "transport", "host", "port", "unit_id", "logger_serial", "device_fp")


def address_clashes(rt: ControlRuntime) -> tuple[str, ...]:
    """Domeny wpisów kolidujących z falownikiem: z rekomendacji i z połączenia bezpośredniego."""
    out = [c.get("label") for c in getattr(rt.recommendation, "conflicts", None) or ()
           if isinstance(c, Mapping) and c.get("kind") == "entry"]
    conn = rt.direct
    if conn is not None:
        out.extend(conn.static_conflicts)
        refused = conn.refused()
        if isinstance(refused, str) and refused.startswith(ds.CONFLICT + ":"):
            out.append(refused.split(":", 1)[1])
    return tuple(d for d in out if isinstance(d, str) and d)


def lan_client_seen(conn) -> str | None:
    """Inny klient na łączu: zajęte połączenie albo sygnały `ContentionMonitor` (kod powodu)."""
    if conn is None:
        return None
    if conn.refused() == ds.IN_USE:
        return "in_use"
    monitor = getattr(conn, "monitor", None)
    if getattr(monitor, "state", None) == "conflict":
        return monitor.reason or "bus"
    return None


def _target_identity(target) -> tuple | None:
    if not isinstance(target, Mapping):
        return None
    return tuple(target.get(k) for k in _TARGET_IDENTITY)


def control_options_changed(old: Mapping, new: Mapping) -> bool:
    for key in CONTROL_OPTION_KEYS:
        if key == OPT_DIRECT_TARGET:
            if _target_identity(old.get(key)) != _target_identity(new.get(key)):
                return True
        elif old.get(key) != new.get(key):
            return True
    return False


async def async_direct_search(hass, entry, *, manual=None, port: int | None = None, unit_id: int | None = None,
                              **kw) -> list:
    """Wyszukanie falownika (albo tylko celu wpisanego ręcznie); raporty zapamiętane w runtime wpisu."""
    profiles = await hass.async_add_executor_job(ds.load_profiles)
    if port is not None or unit_id is not None:
        def factory(cfg):
            return ds.make_transport(replace(cfg, port=port if port is not None else cfg.port,
                                             unit=unit_id if unit_id is not None else cfg.unit),
                                     allow_loopback=ds.ALLOW_LOOPBACK)
        kw.setdefault("transport_factory", factory)
    reports = await ds.async_search(hass, entry, profiles, manual=manual, **kw)
    rt = (hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}).get("control")
    if rt is not None:
        rt.last_probe = list(reports)
        schedule_recommendation(hass, entry, rt)
    return reports


async def async_update_recommendation(hass, entry, rt: ControlRuntime, profiles=None):
    """Rekomendacja ścieżki z ostatniego rozpoznania i wyszukiwania; zapis w runtime, sygnał przy zmianie.

    Nigdy nie rzuca: błąd zostawia poprzednią rekomendację (None = nie policzono).
    """
    rt.recommendation_gen += 1
    gen = rt.recommendation_gen
    try:
        if profiles is None:
            profiles = await hass.async_add_executor_job(ds.load_profiles)
        probe = _recommendation_probe(entry.options, rt.last_probe)
        clash = await ds.async_clash(hass, entry.entry_id, probe.candidate.host) if probe is not None else ()
        choice = _choice_for(hass, entry, profiles)
        report = rt.report() if rt.report is not None else None
        domains = {i.get("domain") for i in (report or {}).get("inverters") or () if isinstance(i, Mapping)}
        if choice is not None and choice.integration_domain:
            domains.add(choice.integration_domain)
        rec = recommend(report, profiles, probe, ds.offer_reason(probe, profiles, clash),
                        map_entities(hass, choice), clash, choice=choice,
                        origins=await _async_origins(hass, domains))
    except Exception as err:  # noqa: BLE001 — rekomendacja nie psuje sterowania ani opcji
        _LOGGER.warning("Volcast control: path recommendation failed (%s)", type(err).__name__)
        return None
    if gen != rt.recommendation_gen:
        return None                       # w międzyczasie ruszyło nowsze przeliczenie — ono zapisze wynik
    if rec != rt.recommendation:
        rt.recommendation = rec
        async_dispatcher_send(hass, SIGNAL_CONTROL_STATE_UPDATED.format(entry_id=entry.entry_id))
    return rec


def schedule_recommendation(hass, entry, rt: ControlRuntime) -> None:
    """Przeliczenie w tle wpisu (unload je anuluje) — sprawdzenie kolizji rozwiązuje nazwy hostów
    i nie może opóźniać wołającego."""
    entry.async_create_background_task(hass, async_update_recommendation(hass, entry, rt), "volcast_recommendation")


def _recommendation_probe(options: Mapping, reports):
    """Raport sondy do rekomendacji: urządzenie z celu w opcjach, gdy jest wśród trafień, inaczej pierwsze."""
    hits = ds.found(reports or [])
    target = options.get(OPT_DIRECT_TARGET)
    fp = target.get("device_fp") if isinstance(target, Mapping) else None
    return next((r for r in hits if fp and r.identity.device_fp == fp), hits[0] if hits else None)


async def _async_origins(hass, domains) -> dict[str, str]:
    """Pochodzenie integracji (`core` = wbudowana w HA, `custom` = doinstalowana); nieznane pomijamy."""
    out: dict[str, str] = {}
    if async_get_integration is None:
        return out
    for domain in sorted(d for d in domains if isinstance(d, str) and d):
        try:
            integration = await async_get_integration(hass, domain)
        except Exception:  # noqa: BLE001 — brak pochodzenia nie blokuje rekomendacji
            continue
        out[domain] = "core" if getattr(integration, "is_built_in", False) else "custom"
    return out


def changed_option_keys(old: Mapping, new: Mapping) -> set[str]:
    return {k for k in {*old, *new} if old.get(k) != new.get(k)}


async def async_restore_if_control_changed(runtime, old: Mapping, new: Mapping) -> bool:
    """Stary wykonawca przywraca tryb bazowy, gdy zmiana opcji zmienia sterowanie.

    True = powrót wykonany (wykonawca nie jest już właścicielem). Nigdy nie rzuca.
    """
    executor = getattr(runtime, "executor", None)
    if executor is None or not control_options_changed(old, new) or not getattr(executor, "owned", False):
        return False
    try:
        await executor.async_restore_now()
    except Exception as err:  # noqa: BLE001 — zapis opcji nie może się przez to wywrócić
        _LOGGER.warning("Volcast control: return to the baseline before reload failed (%s)",
                        type(err).__name__)
        return False
    return not getattr(executor, "owned", False)


def _binding(options: Mapping) -> tuple:
    """Z czym wiąże się własność wykonawcy złożonego dla tych opcji (sposób sterowania + cel/mapowanie)."""
    found = _direct_target(options)
    if found is not None:
        return CONTROL_MODE_DIRECT, _target_identity(found[0])
    return CONTROL_MODE_ENTITIES, options.get(OPT_PROFILE_ID), options.get(OPT_INVERTER_DOMAIN)


async def async_control_change_allowed(runtime, old: Mapping, new: Mapping) -> bool:
    """Czy zmianę opcji wolno zapisać: najpierw powrót przez obecnego wykonawcę (`async_restore_if_control_changed`).

    Nieudany powrót blokuje zapis (False), gdy wykonawca złożony po przeładowaniu nie przejąłby rekordu
    własności — inny sposób sterowania, inny cel albo inne mapowanie encji; ten sam sposób i to samo
    powiązanie (np. wyłączenie sterowania przez encje) ponawia powrót co cykl, więc zapis jest bezpieczny.
    """
    if await async_restore_if_control_changed(runtime, old, new):
        return True
    executor = getattr(runtime, "executor", None)
    if not control_options_changed(old, new) or not getattr(executor, "owned", False):
        return True
    return _binding(old) == _binding(new)


def _schedule_reload(hass, entry_id: str) -> None:
    schedule = getattr(hass.config_entries, "async_schedule_reload", None)
    if callable(schedule):
        schedule(entry_id)
    else:  # HA sprzed async_schedule_reload
        hass.async_create_task(hass.config_entries.async_reload(entry_id))


async def async_resume_control(hass, entry_id: str | None = None) -> list[str] | None:
    """„Wznów teraz" dla wpisu (albo wszystkich): wyniki wykonawców; None = brak sterowania.

    Wspólne dla naprawy w zgłoszeniu i serwisu `volcast.resume_control`.
    """
    entries = hass.data.get(DOMAIN, {})
    ids = [entry_id] if entry_id is not None else list(entries)
    runtimes = [rt for i in ids if isinstance(entries.get(i), dict)
                and (rt := entries[i].get("control")) is not None]
    if not runtimes:
        return None
    return [await rt.executor.async_resume_control() for rt in runtimes]


def _entry_lock(hass, entry_id: str) -> asyncio.Lock:
    locks = hass.data.setdefault(_LOCKS_KEY, {})
    lock = locks.get(entry_id)
    if lock is None:
        lock = locks[entry_id] = asyncio.Lock()
    return lock


def inverter_hints(hass) -> list[InverterHint]:
    out: list[InverterHint] = []
    for dev in all_devices(dr.async_get(hass)):
        if getattr(dev, "disabled_by", None):
            continue
        for ce_id in sorted(getattr(dev, "config_entries", None) or ()):
            ce = hass.config_entries.async_get_entry(ce_id)
            if ce is not None and ce.domain in INVERTER_DOMAINS:
                out.append(InverterHint(ce.domain, dev.manufacturer, dev.model))
    return out


def _choice_for(hass, entry, profiles) -> ProfileChoice | None:
    hints = inverter_hints(hass)
    pid, domain = entry.options.get(OPT_PROFILE_ID), entry.options.get(OPT_INVERTER_DOMAIN)
    if pid and domain:
        prof = next((p for p in profiles if p.id == pid), None)
        if prof is not None:
            model = next((h.model for h in hints if h.domain == domain), None)
            return ProfileChoice(prof, domain, model)
    return select_profile(hints, profiles)


def map_entities(hass, choice: ProfileChoice | None) -> dict[str, str]:
    if choice is None or not choice.integration_domain:
        return {}
    cands = []
    for e in er.async_get(hass).entities.values():
        if e.platform != choice.integration_domain or getattr(e, "disabled_by", None):
            continue
        st = hass.states.get(e.entity_id)
        unit = (st.attributes.get("unit_of_measurement") if st else None) or getattr(e, "unit_of_measurement", None)
        cands.append(EntityCandidate(e.entity_id, e.platform, e.unique_id or "", unit))
    return dict(resolve_entities(choice.profile, choice.integration_domain, cands).mapped)


def mode_unique_id(hass, mapped: Mapping[str, str]) -> str | None:
    """Identyfikator rejestru zmapowanej encji trybu — wiąże własność niezależnie od entity_id."""
    eid = mapped.get("mode")
    if not eid:
        return None
    entry = er.async_get(hass).entities.get(eid)
    uid = getattr(entry, "unique_id", None) if entry is not None else None
    return uid if isinstance(uid, str) and uid else None


def inverter_entity_ids(hass, choice: ProfileChoice | None, mapped: Mapping[str, str]) -> frozenset[str]:
    """Encje z tego samego wpisu konfiguracji albo urządzenia co zmapowane encje falownika.

    Tylko platforma integracji falownika; nic zmapowanego = pusty zbiór. Z nich jedynych
    wolno automatycznie wybrać licznik zużycia domu.
    """
    domain = choice.integration_domain if choice else None
    if not domain or not mapped:
        return frozenset()
    entities = [e for e in er.async_get(hass).entities.values() if e.platform == domain]
    targets = set(mapped.values())
    entries = {getattr(e, "config_entry_id", None) for e in entities if e.entity_id in targets} - {None}
    devices = {getattr(e, "device_id", None) for e in entities if e.entity_id in targets} - {None}
    return frozenset(e.entity_id for e in entities
                     if e.entity_id in targets or getattr(e, "config_entry_id", None) in entries
                     or getattr(e, "device_id", None) in devices)


def _direct_target(options: Mapping) -> tuple[dict, bool] | None:
    target = options.get(OPT_DIRECT_TARGET)
    trial = options.get(OPT_DIRECT_TRIAL) is True
    mode = options.get(OPT_CONTROL_MODE)
    if not isinstance(target, Mapping) or not (mode == CONTROL_MODE_DIRECT or trial):
        return None
    if mode not in (None, CONTROL_MODE_DIRECT):
        return None                  # encje + próba = dwie drogi do jednego falownika (opcje odrzucają)
    return dict(target), trial


def _str_keys(raw) -> tuple[str, ...]:
    return tuple(k for k in raw if isinstance(k, str)) if isinstance(raw, (list, tuple)) else ()


def _positive(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None


def direct_rated_power(options: Mapping, target: Mapping) -> float | None:
    """Moc znamionowa w trybie bezpośrednim: ręczna z opcji wygrywa, inaczej z rejestrów (sonda)."""
    return _positive(options.get(OPT_RATED_POWER_W)) or _positive(target.get("rated_power_w"))


def direct_limits(options: Mapping, target: Mapping) -> dict | None:
    manual = _positive(options.get(OPT_RATED_POWER_W))
    return executor_limits(rated_power_w=direct_rated_power(options, target),
                           battery_capacity_kwh=options.get(OPT_BATTERY_CAPACITY_KWH),
                           source="user" if manual else "registers")


def direct_caps_for(options: Mapping, profile) -> dict[str, bool] | None:
    """Możliwości do bloku `driver` — tylko przy trybie bezpośrednim (w próbie nie sterujemy)."""
    found = _direct_target(options)
    if found is None or found[1] or options.get(OPT_CONTROL_MODE) != CONTROL_MODE_DIRECT:
        return None
    target = found[0]
    caps = target.get("capabilities")
    return direct_capabilities(profile, caps if isinstance(caps, Mapping) else {}, _str_keys(target.get("unreadable")))


def compose_direct(hass, entry, profiles, *, salt: bytes, found: tuple[dict, bool] | None = None):
    """(choice, DirectIO, DirectConnection) dla wpisu w trybie bezpośrednim albo próbnym; None, gdy nie.

    Z celu (`direct_target`, wynik sondy) idą WYŁĄCZNIE: klucze bez odczytu zwrotnego (`unreadable`) —
    do klienta, pisarza, celu i pamięci — oraz możliwości (`capabilities`, False = brak rejestru).
    `found` = (cel, próba) podane wprost — powrót przez tryb bezpośredni po zmianie sposobu sterowania.
    """
    if found is None:
        found = _direct_target(entry.options)
    if found is None:
        return None
    target, trial = found
    profile = next((p for p in profiles if p.id == target.get("profile_id")), None)
    if profile is None:
        _LOGGER.warning("Volcast direct control: the saved inverter profile is not available")
        return None
    unreadable = _str_keys(target.get("unreadable"))
    caps = target.get("capabilities")
    # bez rejestru według sondy — poza cyklem odpytywania (`DirectConnection.unsupported`); tylko łącze
    # bez korelacji odpowiedzi (wyjątek byłby „poprzednią odpowiedzią”), inne odpytują po staremu
    unsupported = set()
    if target.get("transport") in UNCORRELATED_KINDS and isinstance(caps, Mapping):
        unsupported = {k for k, v in caps.items() if v is False}
    poll = entry.options.get(OPT_DIRECT_POLL_S)
    poll_s = float(poll) if isinstance(poll, (int, float)) and not isinstance(poll, bool) else float(DIRECT_POLL_S)
    poll_s = min(max(poll_s, _POLL_RANGE_S[0]), _POLL_RANGE_S[1])
    conn = DirectConnection(hass, entry, profile, target, trial=trial, salt=salt, poll_s=poll_s,
                            unreadable=unreadable, unsupported=unsupported, allow_loopback=ds.ALLOW_LOOPBACK)
    io = DirectIO(conn, profile, trial=trial, unreadable=unreadable,
                  capabilities=caps if isinstance(caps, Mapping) else None, salt=salt)
    return ProfileChoice(profile, None, None), io, conn


@dataclass
class _Composed:
    choice: ProfileChoice | None
    mapped: dict[str, str]
    io: DirectIO | None
    conn: DirectConnection | None
    # True = wykonawca sposobu sterowania z rekordu własności, tylko do powrotu (potem przeładowanie)
    returning: bool = False


async def _async_owner_record(store: ControlStore) -> dict | None:
    """Rekord własności z magazynu; nieczytelny magazyn rozstrzyga sam wykonawca (wyłącza się)."""
    try:
        state = await store.async_load()
    except Exception:  # noqa: BLE001
        return None
    return dict(state.owner) if state.owned and state.owner else None


def _entity_owner_matches(hass, choice, mapped, record: Mapping) -> bool:
    profile = choice.profile if choice else None
    domain = choice.integration_domain if choice else None
    probe = EntityIO(hass, profile, domain, mapped if domain else {}, None,
                     mode_unique_id=mode_unique_id(hass, mapped))
    return probe.owner_matches(record)


def _compose_return(hass, entry, profiles, record: Mapping, salt: bytes | None) -> _Composed | None:
    """Wykonawca sposobu sterowania, w którym zapisano własność — gdy opcje wskazują już inny.

    Bezpośrednio: cel z opcji (zmiana sposobu go nie kasuje) i tylko przy zgodnym odcisku celu i urządzenia.
    Encje: profil i integracja z rekordu. None = nie ma którędy wrócić.
    """
    if record.get("mode") == CONTROL_MODE_DIRECT:
        target = entry.options.get(OPT_DIRECT_TARGET)
        if not isinstance(target, Mapping) or salt is None:
            return None
        composed = compose_direct(hass, entry, profiles, salt=salt, found=(dict(target), False))
        if composed is None or not composed[1].owner_matches(record):
            return None
        choice, io, conn = composed
        return _Composed(choice, {}, io, conn, returning=True)
    profile = next((p for p in profiles if p.id == record.get("profile")), None)
    domain = record.get("domain")
    if profile is None or not isinstance(domain, str) or not domain:
        return None
    choice = ProfileChoice(profile, domain, None)
    mapped = map_entities(hass, choice)
    if not _entity_owner_matches(hass, choice, mapped, record):
        return None
    return _Composed(choice, mapped, None, None, returning=True)


async def _async_compose(hass, entry, profiles, store: ControlStore) -> _Composed:
    """Złożenie wykonawcy dla opcji wpisu — chyba że magazyn ma własność, której ten wykonawca nie przejmie.

    Wtedy najpierw wykonawca sposobu z rekordu (powrót przez STARY sposób); rekord zostaje, dopóki
    powrót nie dojdzie. Bez drogi powrotu — zwykłe złożenie (wykonawca porzuca rekord ze zgłoszeniem).
    """
    record = await _async_owner_record(store)
    need_salt = _direct_target(entry.options) is not None or (record or {}).get("mode") == CONTROL_MODE_DIRECT
    salt = await async_installation_salt(hass) if need_salt else None
    composed = None
    if _direct_target(entry.options) is not None:
        composed = compose_direct(hass, entry, profiles, salt=salt)
    if composed is not None:
        choice, io, conn = composed
        out = _Composed(choice, {}, io, conn)
        matches = record is None or io.owner_matches(record)
    else:
        choice = _choice_for(hass, entry, profiles)
        out = _Composed(choice, map_entities(hass, choice), None, None)
        matches = record is None or _entity_owner_matches(hass, choice, out.mapped, record)
    if matches:
        return out
    back = _compose_return(hass, entry, profiles, record, salt)
    if back is None:
        return out
    _LOGGER.warning("Volcast control: the inverter still has settings from the previous control method — "
                    "returning them through that method first")
    return back


async def async_setup_control(hass, entry, *, report: Callable[[], dict | None]) -> ControlRuntime | None:
    backend = Backend.from_dict(entry.data.get(CONF_BACKEND))
    if backend is None:
        return None
    opts = entry.options
    cloud = VolcastCloud(async_get_clientsession(hass), entry.data[CONF_API_KEY], backend)
    profiles = await hass.async_add_executor_job(ds.load_profiles)
    store = ControlStore(hass, entry.entry_id)
    composed = await _async_compose(hass, entry, profiles, store)
    choice, mapped, io, conn = composed.choice, composed.mapped, composed.io, composed.conn
    manual_rated = opts.get(OPT_RATED_POWER_W)
    rated = float(manual_rated) if manual_rated else rated_power_from_model(choice.model if choice else None)
    if conn is not None:
        rated = direct_rated_power(opts, conn.target)
    released = (lambda: _schedule_reload(hass, entry.entry_id)) if composed.returning else None
    executor = VolcastExecutor(hass, entry, choice=choice, mapped=mapped, rated_power_w=rated,
                               store=store, writer=EntityServiceWriter(hass),
                               lock=_entry_lock(hass, entry.entry_id),
                               mode_unique_id=mode_unique_id(hass, mapped), io=io, on_released=released)
    await executor.async_start()
    telemetry = None
    rt = None
    verification = None
    channel = live = hub = None
    try:
        def _task(coro, name):
            # Zadania sygnałów należą do HA — zatrzymuje je teardown, nie anulowanie wpisu w pół kroku.
            # Zadanie HA startuje gorliwie, czyli zanim wołający zapisze jego uchwyt (kanał → wake →
            # hub → update wołałby się rekurencyjnie i osierocił pierwsze zadanie) — pierwszy krok
            # oddaje więc sterowanie (bez `eager_start`, którego starsze wersje HA nie znają).
            # Kanał, hub i nadajnik są na to odporne same; to tylko dodatkowy bezpiecznik.
            async def _deferred():
                try:
                    await asyncio.sleep(0)
                except BaseException:
                    coro.close()
                    raise
                await coro
            return hass.async_create_background_task(_deferred(), name)

        async def _wake() -> None:
            await hub.request_refresh()         # hub powstaje niżej; wołane dopiero po złożeniu

        async def _apply(raw) -> None:
            await hub.apply(raw)

        async def _control(block) -> None:
            if rt is not None:
                await control_choice.apply(block, rt)

        async def _fetch(_now=None) -> None:
            # Odświeżenie planu (i zgody) — zaraz po nim cykl: cofnięta zgoda działa od razu.
            await fetcher.async_refresh()
            await executor.async_tick()

        channel = SignalChannel(async_get_clientsession(hass), on_wake=_wake, task_factory=_task)
        if conn is not None:
            limits = direct_limits(opts, conn.target)
        else:
            limits = executor_limits(rated_power_w=rated, battery_capacity_kwh=opts.get(OPT_BATTERY_CAPACITY_KWH),
                                     source="user" if manual_rated else "entities")
        telemetry = TelemetrySender(hass, entry, cloud, executor, choice=choice, profile_map=mapped,
                                    manual_map=opts.get(OPT_TELEMETRY_MAP) or {},
                                    grid_negate=bool(opts.get(OPT_GRID_NEGATE)), limits=limits, direct=conn,
                                    direct_capabilities=direct_caps_for(opts, choice.profile) if conn else None,
                                    on_signals=_apply, signal_connected=lambda: channel.connected)
        live = LiveSender(cloud=cloud, telemetry=telemetry, on_signals=_apply, task_factory=_task)
        hub = SignalsHub(base_url=backend.base_url, channel=channel, live=live, refresh=_fetch, task_factory=_task)
        fetcher = ScheduleFetcher(cloud, on_plan=executor.async_on_plan, on_consent=executor.async_set_consent,
                                  on_auth_failure=executor.async_on_auth_failure, on_signals=hub.apply,
                                  on_control=_control)
        await telemetry.async_start()
        if choice is not None and not composed.returning:
            # Przed pierwszym cyklem: w trybie bezpośrednim plan czeka na zweryfikowane urządzenie.
            verification = VerificationRunner(
                hass, entry, executor, params=default_params(choice.profile),
                start_rung=start_rung_for(choice.profile, choice.integration_domain, direct=conn is not None),
                salt=await async_installation_salt(hass), on_urgent=telemetry.async_flush)
            await verification.async_start()
        rt = ControlRuntime(executor, fetcher, telemetry, cloud, choice, mapped, rated,
                            options_at_setup=dict(opts),
                            inverter_entities=inverter_entity_ids(hass, choice, mapped), direct=conn,
                            channel=channel, live=live, hub=hub, report=report, verification=verification,
                            hass=hass, entry=entry)

        telemetry.control_runtime = rt
        limiter = FlushLimiter(hass, telemetry.async_flush)
        rt.unsubs.append(limiter.cancel)
        rt.unsubs.append(async_dispatcher_connect(
            hass, SIGNAL_CONTROL_STATE_UPDATED.format(entry_id=entry.entry_id), limiter.request))
        rt.unsubs.append(async_track_time_interval(hass, _fetch, timedelta(seconds=SCHEDULE_FETCH_INTERVAL_S)))
        rt.conflicts = ConflictMonitor(hass, entry, executor, verification=verification,
                                       clashes=lambda: address_clashes(rt), lan_client=lambda: lan_client_seen(conn))
        rt.unsubs.append(rt.conflicts.stop)
        await rt.conflicts.async_start()

        @callback
        def _on_discovery() -> None:
            schedule_recommendation(hass, entry, rt)

        rt.unsubs.append(async_dispatcher_connect(
            hass, SIGNAL_DISCOVERY_UPDATED.format(entry_id=entry.entry_id), _on_discovery))
        track_ha_stop(hass, rt)
        # Runtime w hass.data PRZED onboardingiem — ten czyta go od razu (start „na gorąco").
        hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})["control"] = rt
        _maybe_start_onboarding(hass, entry, report, profiles)
    except BaseException:
        # Nieudane złożenie nie zostawia żywego wykonawcy (bez encji wyłącznika nikt by go
        # nie zatrzymał, a każde przeładowanie dokładałoby kolejnego).
        if verification is not None:
            await verification.async_stop()
        await _async_abort_setup(hass, entry, executor, telemetry, rt, signals=(hub, live, channel))
        if conn is not None:
            await conn.async_stop()
        raise
    # Pierwszy cykl i pierwsze pobranie — zadania HA, nie wpisu: unload nie anuluje ich
    # w połowie zapisu grupy (zatrzymany wykonawca i tak już nic nie zapisze).
    hass.async_create_background_task(executor.async_tick(), "volcast_first_tick")
    hass.async_create_background_task(_fetch(), "volcast_first_fetch")
    if conn is not None:
        entry.async_create_background_task(hass, _async_start_direct(conn, executor), "volcast_direct_start")
    entry.async_create_background_task(
        hass, async_import_history_once(hass, cloud, executor, load_entity=opts.get(OPT_LOAD_ENERGY),
                                        pv_entity=opts.get(CONF_PV_ENERGY_ENTITY) or None,
                                        now_utc=dt_util.utcnow()),
        "volcast_history_import")
    return rt


async def _async_start_direct(conn, executor) -> None:
    """Start połączenia w tle (kolizje, tożsamość), pierwszy odczyt i od razu cykl."""
    try:
        # Własność z wcześniejszej sesji: kolizja statyczna nie odcina powrotu do trybu bazowego.
        conn.allow_conflicted_restore = bool(getattr(executor, "owned", False))
        await conn.async_start()
        if conn.refused() is None:
            await conn.async_poll()
            await executor.async_tick()
    except Exception as err:  # noqa: BLE001 — połączenie nie psuje prognozy ani wpisu
        _LOGGER.warning("Volcast direct connection start failed (%s)", type(err).__name__)


async def _async_stop_signals(parts) -> None:
    """Zatrzymuje hub, nadajnik „na żywo” i kanał (w tej kolejności); awaria jednego nie blokuje reszty.

    Hub pierwszy: od tej chwili spóźnione `apply` (odpowiedź telemetrii, pobranie w toku) nie
    wskrzesza nadawania ani kanału, a pobranie w toku kończy się bez ucinania zapisu wykonawcy.
    """
    for part in parts:
        if part is None:
            continue
        try:
            await part.async_stop()
        except Exception as err:  # noqa: BLE001 — CancelledError przechodzi dalej
            _LOGGER.warning("Volcast control: stopping the signals failed (%s)", type(err).__name__)


async def _async_abort_setup(hass, entry, executor, telemetry, rt, signals=()) -> None:
    """Sprzątanie po błędzie za `executor.async_start()`; samo nigdy nie rzuca."""
    for unsub in (rt.unsubs if rt is not None else ()):
        try:
            unsub()
        except Exception:  # noqa: BLE001
            pass
    if rt is not None:
        rt.unsubs.clear()
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if isinstance(entry_data, dict) and rt is not None and entry_data.get("control") is rt:
        entry_data["control"] = None
    if rt is not None and not signals:
        signals = (rt.hub, rt.live, rt.channel)
    await _async_stop_signals(signals)
    for stop in ((telemetry.async_stop,) if telemetry is not None else ()) + (executor.async_stop,):
        try:
            await stop()
        except Exception as err:  # noqa: BLE001 — pierwotny błąd jest ważniejszy
            _LOGGER.warning("Volcast control: cleanup after a failed setup failed (%s)", type(err).__name__)


def _maybe_start_onboarding(hass, entry, report, profiles=()) -> None:
    from ..onboarding import Onboarding   # późny import: onboarding nie jest potrzebny po oknie 30 min
    p = entry.data.get(CONF_PAIRING)
    if not isinstance(p, dict):
        return
    live_until = dt_util.parse_datetime(str(p.get("live_until") or ""))
    if live_until is None or live_until <= dt_util.utcnow():
        return
    running = hass.data.setdefault(ONBOARDING_KEY, {})
    task = running.get(entry.entry_id)
    if task is not None and not task.done():
        return      # przeładowanie wpisu (zmiana opcji) nie startuje drugiego onboardingu
    client = PairingClient(async_get_clientsession(hass), p.get("url") or BETA_PAIRING_URL)
    session = PairingSession(str(p.get("session_id")), str(p.get("poll_token")), "", "")

    def runtime():
        return (hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}).get("control")

    async def import_history():
        rt, e = runtime(), hass.config_entries.async_get_entry(entry.entry_id)
        if rt is None or e is None:
            return None
        if rt.executor.history_imported_at:
            return {"accepted": 0, "already": True}
        return await async_import_history_once(hass, rt.cloud, rt.executor,
                                               load_entity=e.options.get(OPT_LOAD_ENERGY),
                                               pv_entity=e.options.get(CONF_PV_ENERGY_ENTITY) or None,
                                               now_utc=dt_util.utcnow())

    async def search():
        e = hass.config_entries.async_get_entry(entry.entry_id) or entry
        return await async_direct_search(hass, e)

    ob = Onboarding(hass, entry.entry_id, client=client, session=session, live_until=live_until,
                    runtime=runtime, report=report, import_history=import_history, search=search,
                    profiles=list(profiles))
    running[entry.entry_id] = hass.async_create_background_task(ob.async_run(), "volcast_onboarding")


def freeze_control(rt: ControlRuntime) -> None:
    """Przed powrotem i przeładowaniem po zmianie opcji sterowania: koniec cykli i pobierania.

    Powrót do trybu bazowego (`async_restore_now`) nadal działa.
    """
    for unsub in rt.unsubs:
        unsub()
    rt.unsubs.clear()
    # Pingi kanału nie planują już pobrań (czekające anulowane; pobranie w toku dobiega końca,
    # a jego cykl i tak zatrzyma zamrożony wykonawca). Resztę sygnałów zamyka rozładunek.
    hub_freeze = getattr(rt.hub, "freeze", None)
    if hub_freeze is not None:
        hub_freeze()
    freeze = getattr(rt.executor, "freeze", None)
    if freeze is not None:
        freeze()


async def async_neutral_at_shutdown(rt: ControlRuntime) -> None:
    """Zatrzymanie HA: koniec cykli i pobierania planu, potem tryb neutralny zamiast NASZEGO trybu
    wymuszonego (`VolcastExecutor.async_neutral_at_stop`, budżet `STOP_WRITE_TIMEOUT_S`). Własność
    zostaje — po restarcie wykonawca przejmuje sterowanie. Nigdy nie rzuca."""
    try:
        freeze_control(rt)
        await rt.executor.async_neutral_at_stop()
    except Exception as err:  # noqa: BLE001 — zatrzymanie HA nie może się przez to wywrócić
        _LOGGER.warning("Volcast control: neutral mode at Home Assistant stop failed (%s)", type(err).__name__)


def track_ha_stop(hass, rt: ControlRuntime) -> None:
    """Nasłuch zatrzymania HA (`EVENT_HOMEASSISTANT_STOP`, etap 1 — HA czeka na zadania z niego).

    Zdjęcie przy rozładunku przez `rt.unsubs`; po wystrzeleniu zdjęcie jest puste (HA usuwa nasłuch
    jednorazowy sam i loguje błąd przy drugim usunięciu). Zadanie zakładane od razu w callbacku."""
    fired = False

    @callback
    def _on_stop(_event) -> None:
        nonlocal fired
        fired = True
        hass.async_create_task(async_neutral_at_shutdown(rt), "volcast_stop_neutral")

    remove = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop)

    def _unsub() -> None:
        if not fired:
            remove()
    rt.unsubs.append(_unsub)


async def async_unload_control(hass, rt: ControlRuntime, *, restore: bool = False) -> None:
    """Rozładowanie wpisu (przeładowanie, restart integracji): falownik nie zostaje w NASZYM trybie
    wymuszonym na czas przerwy — sam tryb neutralny, własność zostaje (`restore=False`, domyślnie):
    nowy wykonawca przejmuje sterowanie z zachowaną migawką, bez pełnego powrotu i ponownego zapisu
    wszystkich nastaw (NVM).

    `restore=True` jest dla jawnej decyzji właściciela wyłączyć wpis (`entry.disabled_by`
    ustawione przy rozładunku) — wtedy oddajemy falownik w tryb bazowy, zanim wykonawca
    się zatrzyma. Błąd przywrócenia nigdy nie blokuje rozładunku.
    """
    for unsub in rt.unsubs:
        unsub()
    rt.unsubs.clear()
    if rt.verification is not None:
        await rt.verification.async_stop()
    # Sygnały przed powrotem: cykl z pingu po powrocie do trybu bazowego zapisałby plan z powrotem.
    await _async_stop_signals((rt.hub, rt.live, rt.channel))
    if restore:
        try:
            await rt.executor.async_restore_now()
        except Exception as err:  # noqa: BLE001 — rozładunek wpisu nie może się przez to wywrócić
            _LOGGER.warning("Volcast control: restore before disabling the entry failed (%s)",
                            type(err).__name__)
    else:
        try:
            await rt.executor.async_neutral_at_stop()
        except Exception as err:  # noqa: BLE001 — rozładunek wpisu nie może się przez to wywrócić
            _LOGGER.warning("Volcast control: neutral mode before unloading failed (%s)", type(err).__name__)
    await rt.telemetry.async_stop()
    await rt.executor.async_stop()
    if rt.direct is not None:
        # Po wykonawcy (powrót przy wyłączeniu wpisu szedł jeszcze tym połączeniem).
        await rt.direct.async_stop()


def _removal_issue(hass, entry) -> None:
    """Usunięcie wpisu bez powrotu do nastaw sprzed sterowania: zgłoszenie w Naprawach (tekst „nie można
    oddać nastaw"); nigdy nie rzuca."""
    try:
        ir.async_create_issue(hass, DOMAIN, f"control_removal_failed_{entry.entry_id}", is_fixable=False,
                              severity=_ISSUE_WARNING, translation_key="control_record_dropped")
    except Exception as err:  # noqa: BLE001 — usunięcie wpisu nie może się wywrócić
        _LOGGER.warning("Volcast control: repair issue on removal not created (%s)", type(err).__name__)


async def async_remove_control(hass, entry) -> None:
    """Usunięcie wpisu: przywróć tryb bazowy, jeśli to my zmienialiśmy nastawy; skasuj stan."""
    if Backend.from_dict(entry.data.get(CONF_BACKEND)) is None:
        return
    task = hass.data.get(ONBOARDING_KEY, {}).pop(entry.entry_id, None)
    if task is not None:
        task.cancel()
    store = ControlStore(hass, entry.entry_id)
    try:
        state = await store.async_load()
    except Exception as err:  # noqa: BLE001 — zły format magazynu: nie ma czego przywrócić
        _LOGGER.warning("Volcast control: saved state unreadable on removal (%s) — the inverter may keep the "
                        "last settings Volcast applied; check its mode", type(err).__name__)
        _removal_issue(hass, entry)            # nie wiemy, czy falownik ma nasz tryb — właściciel musi wiedzieć
        state = None
    if state is not None and state.owned:
        profiles = await hass.async_add_executor_job(ds.load_profiles)
        composed = await _async_compose(hass, entry, profiles, store)
        choice, mapped, io, conn = composed.choice, composed.mapped, composed.io, composed.conn
        executor = VolcastExecutor(hass, entry, choice=choice, mapped=mapped,
                                   rated_power_w=None, store=store, writer=EntityServiceWriter(hass),
                                   lock=_entry_lock(hass, entry.entry_id),
                                   mode_unique_id=mode_unique_id(hass, mapped), io=io)
        await executor.async_start()
        if conn is not None:
            conn.allow_conflicted_restore = True        # usuwamy wpis przy własności: tylko powrót
            try:
                await conn.async_start()
            except Exception as err:  # noqa: BLE001 — usunięcie wpisu nie może się wywrócić
                _LOGGER.warning("Volcast direct connection failed on removal (%s)", type(err).__name__)
        await executor.async_restore_now()
        if executor.owned:
            # Still owned after the attempt: no profile/integration, a read-only profile,
            # or the write itself failed (already logged by the executor in that case) —
            # the store is wiped next regardless, so this is the last chance to say so.
            _LOGGER.warning("Volcast control: could not return the inverter to its settings from "
                            "before control — check its mode")
            # Stan sterowania znika razem z wpisem — zgłoszenie w Naprawach zostaje (sam log to za mało).
            _removal_issue(hass, entry)
        await executor.async_stop()
        if conn is not None:
            await conn.async_stop(forget=True)
    await store.async_remove()
    hass.data.get(_LOCKS_KEY, {}).pop(entry.entry_id, None)
    try:
        ir.async_delete_issue(hass, DOMAIN, f"control_record_dropped_{entry.entry_id}")
    except Exception:  # noqa: BLE001 — usunięcie wpisu nie może się wywrócić
        pass
