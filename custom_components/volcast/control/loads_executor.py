"""Wykonawca slotów ładowarki EV (`loads[]` z planu) przez encje innej integracji.

Osobny od wykonawcy falownika: własny magazyn (`LoadsStore`), własny pisarz i własny odstęp
zapisów. Zapisuje WYŁĄCZNIE encje ról potwierdzonej ładowarki (`ev_chargers` w opcjach):
nastawę (`number`) i start/stop (`select` albo `switch`). Encja falownika nigdy: każda encja do
zapisu musi w rejestrze encji należeć do urządzenia ładowarki (`device_id == load_key`) i nie może
być na liście zakazanej (encje falownika); inaczej — albo przy innej domenie — ładowarka jest
nieważna w całości (brak zapisów).

Bramki (wszystkie naraz): zgoda konta ∧ przełącznik sterowania w HA ∧ NIE tryb „tylko plan”
(własny sterownik właściciela — zero zapisów, także do ładowarki) ∧ `loads_meta.control_enabled`
(z `plan_generated_at`) ∧ status ładowarki czytelny. Wartość bramki inna niż oczekiwana = zamknięta.
Slot dla „teraz” wybiera `slot_for` ([from, to)) spośród slotów tej ładowarki i tej sesji; brak
slotu = brak zapisu. `forced`: nastawa PRZED startem, start tylko przy nastawie zgodnej z planem w
ODCZYCIE encji (obca zmiana nastawy = najpierw ponowny zapis); start raz na plan i fazę slotów
(`last_action`), więc ładowarka, która nie rusza (auto pełne, kabel), nie dostaje komendy w kółko.
`off`: stop tylko, gdy ładuje, też raz — ręcznego ładowania nie blokujemy; ładowanie, które ruszyło
samo (autostart po podłączeniu), zatrzymujemy dopiero po `OFF_AUTOSTART_GRACE_S`. Nowy plan
(`plan_generated_at`) i zmiana trybu slotu w trakcie pracy zerują `last_action`. Cofnięcie bramki
w trakcie ładowania niczego nie zatrzymuje (ładowarka zostaje, jak jest). `pv_surplus` nie jest
jeszcze wykonywany (nic nie zapisujemy).

Odstęp: najwyżej jeden zapis na encję na `LOADS_MIN_WRITE_INTERVAL_S` (liczony od próby, także
nieudanej). Powtórek tej samej wartości pilnują `last_setpoint_a` / `last_action`, nie odstęp —
po obcym „stop” ten sam „start” musi dać się wysłać ponownie. Odczyt kontrolny po
`LOADS_READBACK_S` tylko loguje rozjazd (bez ponownego zapisu). Encja nastawy, która trzyma inną
wartość niż plan (własny limit ładowarki, kabel, ręczna zmiana), dostaje najwyżej JEDEN ponowny
zapis na plan i slot; potem start idzie tylko, gdy odczyt jest NIŻSZY niż plan (wyższa obca
nastawa mogłaby przekroczyć limit przyłącza).

Reguła awaryjna (`core/ev/fallback.py`): wiek planu = czas od ostatniej odpowiedzi chmury
(`note_plan_confirmed`, nowy plan) — przed pierwszą odpowiedzią od startu: od wygenerowania planu,
po `STARTUP_GRACE_S`. Tylko przy `loads_meta.fallback_allowed` (chmura: nie przy pauzie i „tylko PV”).
Gdy reguła działa: pełna moc i start jak w slocie `forced` (także bez slotu), nigdy stop;
`fallback_since` w magazynie; `fallback: true` w raporcie do świeżego planu i zatrzaśnięte do
przyjęcia raportu (`async_ack_report`), żeby chmura zobaczyła także epizod zakończony przed wysyłką.

Wstrzymanie czasowe (`held_since`, trwałe w magazynie): zero zapisów, dopóki nie przyjdzie plan
wygenerowany PO początku wstrzymania (`plan_generated_at` > `held_since`; także reguła awaryjna czeka).
Zaczyna je:
- zmiana z aktorem HA (użytkownik albo automatyzacja — `is_foreign_change`) na encji `status` albo
  `start_stop`: „stop” w slocie `forced` (albo w trybie awaryjnym), „start” w slocie `off`; w trakcie
  wstrzymania każda taka zmiana — wygrywa ostatnia decyzja. Zdarzenie `manual_event` + `manual_at`.
- KAŻDE przejście statusu „ładuje” → „nie ładuje” bez aktora w slocie `forced` (albo w trybie
  awaryjnym), poza oknem echa NASZEGO stopu (`OWN_ECHO_S`): pauza, koniec ładowania („charged” —
  część ładowarek tak pokazuje każdy stop), błąd, niedostępność, odłączenie. Zdarzenie `stop` tylko
  przy pauzie/końcu, poza oknem echa każdego naszego zapisu, gdy świeży plan (odpowiedź chmury
  ≤ `FALLBACK_PLAN_AGE_S`) ma > `END_REMAINING_KWH` do celu; inaczej samo `held_since`. Limit
  ładowania w aucie (np. 80 %) wygląda tak samo — fałszywa pauza do następnego planu (świadomie).
- start bez aktora (status → ładuje) w slocie `off`, tylko po NASZYM stopie w tej fazie.
Encja `start_stop` bez aktora NIE jest sygnałem: bywa impulsowa (po każdej zmianie wraca do „No
operation”) i urządzenie samo ją przestawia (np. „Stop charging” w chwili, gdy ładowanie rusza,
„Start charging” przy odłączeniu kabla). Nasz zapis (kontekst pisarza) nigdy nie jest ręczny.
Zdjęcie wstrzymania: tylko plan nowszy niż `held_since` (wtedy slot `forced` = nastawa i start —
„Wznów” w aplikacji i start po przesunięciu slotu przez serwer) albo nowa sesja (inne
`session_id`). Zmiana slotu i przeliczenie sprzed wstrzymania go nie zdejmują. Zdarzenie trafia do
stanu od razu w obsłudze zdarzenia, telemetria wysyła je raz (`async_ack_manual` po przyjęciu);
`held_since` idzie w KAŻDEJ paczce, dopóki wstrzymanie trwa — chmura przelicza plan po nim.
Wstrzymanie dłuższe niż `HOLD_WARN_S`: jedno ostrzeżenie, bez samoczynnego wznowienia. Wszystko tylko
przy otwartych bramkach.

Status „ładuje” = słowo z jawnej listy (`charging`) w stanie encji statusu i żadne słowo z listy
wykluczeń (przeczenia, pauza, koniec, błąd, oczekiwanie). Stany w innych językach albo CamelCase
nie są rozpoznawane jako ładowanie (kierunek bezpieczny: brak stopu i wstrzymania, start raz na plan).
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Mapping

from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.util import dt as dt_util

from ..const import (LOADS_EXECUTOR_INTERVAL_S, LOADS_MIN_WRITE_INTERVAL_S, LOADS_READBACK_S,
                     SIGNAL_CONTROL_STATE_UPDATED)
from ..core.control.takeover import is_foreign_change
from ..core.entity_map import EntityWrite
from ..core.ev.convert import slot_for, watts_to_amps
from ..core.ev.fallback import FALLBACK_PLAN_AGE_S, should_force
from ..core.write_sequence import OK
from .loads_store import LoadsState

_LOGGER = logging.getLogger(__name__)

_UNAVAILABLE = ("unavailable", "unknown")
_TOKEN_RE = re.compile(r"[^a-z0-9]+")
_HINTS = {"start": ("start", "resume"), "stop": ("stop", "pause")}
# Słowa stanu „ładuje” i słowa, które to znaczenie odbierają („Charging paused”, „charging_completed”).
_CHARGING_WORDS = frozenset({"charging"})
_NOT_CHARGING_WORDS = frozenset({
    "not", "no", "non", "pause", "paused", "suspend", "suspended", "stop", "stopped", "finish", "finished",
    "complete", "completed", "done", "end", "ended", "error", "fault", "failed", "interrupted", "wait",
    "waiting", "scheduled", "ready", "idle", "standby"})
_SETPOINT_UNITS = ("A", "W")
# Po starcie bez odpowiedzi chmury: przez ten czas wiek planu jest nieznany (sieć często wstaje po HA).
STARTUP_GRACE_S = 300
# Wstrzymanie bez nowego planu dłużej niż to — jedno ostrzeżenie (bez wznowienia).
HOLD_WARN_S = 6 * 3600
# Zmiana stanu ładowarki do tylu sekund po NASZYM zapisie to echo tego zapisu (odczyt kontrolny jest po 20 s).
OWN_ECHO_S = 30.0
# Pauza/koniec ładowania bez aktora przy więcej niż tylu kWh do celu (świeży plan) = zdarzenie „stop”.
END_REMAINING_KWH = 0.5
# Slot `off`: ładowanie, które ruszyło samo (autostart po podłączeniu), zatrzymujemy dopiero po tym czasie.
OFF_AUTOSTART_GRACE_S = 180.0
# Słowa stanu statusu (zmiana bez aktora HA: aplikacja producenta, przycisk, karta RFID).
_END_WORDS = frozenset({"finish", "finished", "complete", "completed", "full", "done", "charged", "end", "ended"})
_ERROR_WORDS = frozenset({"error", "fault", "failed", "failure", "offline", "alarm", "unavailable", "unknown"})
_UNPLUGGED_WORDS = frozenset({"available", "disconnected", "unplugged"})
_CAR_SIDE_WORDS = frozenset({"suspendedev", "waiting", "wait"})
_PAUSE_PREFIXES = ("pause", "stop", "suspend")
_START_STOP_DOMAINS = ("select", "switch")


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.split(text.lower()) if t]


def pick_option(options: Any, action: str) -> str | None:
    """Opcja encji `select` dla `start`/`stop`: początek słowa („Restart” to nie „start”),
    bez opcji pasujących też do akcji przeciwnej."""
    if not isinstance(options, (list, tuple)):
        return None
    want = _HINTS[action]
    other = _HINTS["stop" if action == "start" else "start"]
    for opt in options:
        tokens = _tokens(str(opt))
        if any(t.startswith(want) for t in tokens) and not any(t.startswith(other) for t in tokens):
            return str(opt)
    return None


def is_charging(state: Any) -> bool:
    if not isinstance(state, str):
        return False
    tokens = _tokens(state)
    return bool(_CHARGING_WORDS.intersection(tokens)) and not _NOT_CHARGING_WORDS.intersection(tokens)


def charger_roles(options: Mapping, device_id: str) -> dict | None:
    """Role zapisanej ładowarki (`{rola: entity_id}`); None, gdy ładowarki nie ma w opcjach."""
    saved = options.get("ev_chargers") if isinstance(options, Mapping) else None
    for c in saved if isinstance(saved, list) else ():
        if isinstance(c, dict) and c.get("device_id") == device_id:
            roles = c.get("roles")
            return {k: v for k, v in roles.items() if isinstance(v, str) and v} if isinstance(roles, dict) else {}
    return None


def _num(v) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _when(text: Any) -> datetime | None:
    """Czas ISO-8601 ze strefą z magazynu; zły albo bez strefy = None."""
    if not isinstance(text, str):
        return None
    try:
        d = datetime.fromisoformat(text)
    except ValueError:
        return None
    return d if d.tzinfo is not None else None


def _status_word(state) -> str | None:
    """Stan encji statusu jako `start` (ładuje) / `stop` (nie ładuje); nieczytelny = None."""
    if state is None or state.state in _UNAVAILABLE:
        return None
    return "start" if is_charging(state.state) else "stop"


def status_kind(state) -> str | None:
    """Rodzaj stanu statusu: `charging`, `paused` (wstrzymane/zatrzymane po stronie ładowarki), `end` (koniec,
    auto naładowane), `error`, `car` (pauza po stronie auta), `unplugged`, `other`; nieczytelny = None."""
    if state is None or not isinstance(state.state, str) or state.state in _UNAVAILABLE:
        return None
    tokens = _tokens(state.state)
    words = set(tokens)
    if words & _ERROR_WORDS:
        return "error"
    if words & _END_WORDS:
        return "end"
    if is_charging(state.state):
        return "charging"
    if words & _CAR_SIDE_WORDS or ("ev" in words and "evse" not in words):
        return "car"
    if words & _UNPLUGGED_WORDS or ("not" in words and "connected" in words):
        return "unplugged"
    if any(t.startswith(_PAUSE_PREFIXES) for t in tokens):
        return "paused"
    return "other"


def _command_word(eid: str, state) -> str | None:
    """Stan encji start/stop jako komenda; czytelna opcja bez komendy („No operation”) = `idle`."""
    if state is None or state.state in _UNAVAILABLE:
        return None
    if eid.startswith("switch."):
        return {"on": "start", "off": "stop"}.get(state.state, "idle")
    for action in ("start", "stop"):
        if pick_option([state.state], action) is not None:
            return action
    return "idle"


@dataclass(frozen=True)
class _Charger:
    status: str
    setpoint: str | None
    start_stop: str | None


class LoadsExecutor:
    def __init__(self, hass, entry, writer, roles_provider: Callable[[str], dict | None], store,
                 consent_getter: Callable[[], Any], local_switch_getter: Callable[[], Any], *,
                 plan_only_getter: Callable[[], Any],
                 forbidden_entities: Iterable[str] = (), clock: Callable[[], float] = time.monotonic,
                 utcnow=dt_util.utcnow, readback_s: float = LOADS_READBACK_S) -> None:
        self._hass, self._entry = hass, entry
        self._writer = writer
        self._roles = roles_provider
        self._store = store
        self._consent = consent_getter
        self._local_switch = local_switch_getter
        self._plan_only = plan_only_getter
        self._forbidden = frozenset(forbidden_entities)
        self._clock, self._utcnow = clock, utcnow
        self._readback_s = readback_s
        self._loads: tuple = ()
        self._meta = None
        self._state = LoadsState()
        self._last_write: dict[str, float] = {}       # entity_id → czas próby (zegar monotoniczny)
        self._failed: set[str] = set()                # encje z ostrzeżeniem o nieudanym zapisie
        self._lock = asyncio.Lock()
        self._started = self._stopped = False
        self._unsub: list = []
        self._readback: asyncio.Task | None = None
        self._slot_mode: str | None = None            # tryb slotu z ostatniego cyklu (tylko w pamięci)
        self._confirmed_mono: float | None = None     # ostatnia odpowiedź chmury z planem (zegar monotoniczny)
        self._started_mono: float | None = None
        self._own_stop_mono: float | None = None      # próba NASZEGO ostatniego stopu (okno echa)
        self._charging_seen_mono: float | None = None  # status → ładuje bez naszego zapisu (autostart)
        self._fallback_latched = False                # tryb awaryjny był, a raport z nim jeszcze nie przyjęty
        self._hold_warned: str | None = None          # `held_since` z ostrzeżeniem o długim wstrzymaniu
        self._rewrite_key: tuple | None = None        # (plan, slot) zużytego ponownego zapisu nastawy
        self._rewrite_warned: tuple | None = None
        self._watched: tuple[str, ...] = ()           # encje obserwowane pod kątem ręcznej ingerencji
        self._watch_unsub: Callable[[], None] | None = None

    @property
    def state(self) -> LoadsState:
        return self._state

    def note_plan_confirmed(self) -> None:
        """Chmura odpowiedziała planem (zmienionym albo nie) — wiek planu dla reguły awaryjnej od zera."""
        self._confirmed_mono = self._clock()

    def report_for(self, key: str) -> dict | None:
        """Blok `executor` wpisu ładowarki w telemetrii; None dla innej ładowarki albo bez planu.
        `manual_at` idzie tylko razem z niewysłanym zdarzeniem."""
        meta = self._meta
        if meta is None or meta.load_key != key:
            return None
        s = self._state
        event = s.manual_event
        return {"last_write_at": s.last_write_at, "last_action": s.last_action, "manual_event": event,
                "manual_at": s.manual_at if event is not None else None,
                # zatrzask: tryb awaryjny zakończony przed wysyłką i tak trafia do chmury
                "fallback": s.fallback_since is not None or self._fallback_latched,
                # w KAŻDEJ paczce, dopóki trwa wstrzymanie: chmura zamawia przeliczenie, gdy jej
                # najnowszy plan jest starszy (samoleczenie po zgubionym przeliczeniu)
                "held_since": self._held_since()}

    def _held_since(self) -> str | None:
        """Początek trwającego wstrzymania (ISO-8601 ze strefą) albo None."""
        at = _when(self._state.held_since)
        return at.isoformat() if at is not None else None

    async def async_ack_report(self, report: Any) -> None:
        """Telemetria z tym raportem przyjęta: zdarzenie ręczne i zatrzask trybu awaryjnego wysłane."""
        if not isinstance(report, dict):
            return
        if report.get("fallback") is True:
            self._fallback_latched = False
        if report.get("manual_event") is not None:
            await self.async_ack_manual(report.get("manual_at"))

    async def async_ack_manual(self, manual_at: str | None) -> None:
        """Telemetria ze zdarzeniem `manual_at` przyjęta — zdarzenie wysłane raz. Nowsze zostaje.
        Wstrzymanie (`held_since`) trwa niezależnie, do planu wygenerowanego po nim."""
        s = self._state
        if manual_at is None or s.manual_event is None or s.manual_at != manual_at:
            return
        s.manual_event = None
        s.manual_at = None
        await self._async_save()

    # ── cykl życia ────────────────────────────────────────────────────────
    async def async_start(self, schedule=None) -> None:
        """Wczytuje stan; `schedule` — plan z magazynu wykonawcy falownika (praca bez łącza)."""
        if self._started:
            return
        self._started = True
        self._started_mono = self._clock()
        try:
            self._state = await self._store.async_load()
        except Exception as err:  # noqa: BLE001 — zły magazyn: zaczynamy od zera, bez blokady
            _LOGGER.warning("Volcast EV charger state could not be read (%s); starting fresh",
                            type(err).__name__)
            self._state = LoadsState()
        if schedule is not None:
            self._remember(schedule)
        self._unsub.append(async_track_time_interval(
            self._hass, self._async_timer, timedelta(seconds=LOADS_EXECUTOR_INTERVAL_S)))

    async def async_stop(self) -> None:
        """Bez zapisów: ładowarka zostaje w stanie, w jakim jest (ręczne ładowanie nietknięte)."""
        self._stopped = True
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()
        self._unwatch()
        if self._readback is not None and not self._readback.done():
            self._readback.cancel()

    async def _async_timer(self, _now=None) -> None:
        await self.async_tick()

    # ── wejścia ───────────────────────────────────────────────────────────
    async def async_on_plan(self, schedule) -> None:
        if self._stopped:
            return
        self.note_plan_confirmed()
        async with self._lock:
            if self._remember(schedule):
                await self._async_save()
        await self.async_tick()

    def _remember(self, schedule) -> bool:
        """Sloty i metadane z planu; True, gdy to nowy plan ładowarki (stan do zapisania)."""
        self._loads = tuple(getattr(schedule, "loads", None) or ())
        meta = getattr(schedule, "loads_meta", None)
        self._meta = meta
        if meta is not None:
            self._watch(meta.load_key)
        if meta is None or meta.plan_generated_at is None:
            return False                         # bez znacznika planu cykl i tak nic nie zapisze
        released = self._release_hold(meta)
        gen = meta.plan_generated_at.isoformat()
        if gen == self._state.plan_generated_at:
            return released
        self._state.plan_generated_at = gen
        self._state.last_action = None          # nowy plan może znowu wystartować/zatrzymać
        return True

    def _release_hold(self, meta) -> bool:
        """Koniec wstrzymania: plan wygenerowany PO `held_since` albo nowa sesja ładowania. True = zdjęte."""
        s = self._state
        at = _when(s.held_since)
        if s.held_since is None:
            return False
        newer = at is None or meta.plan_generated_at > at
        new_session = s.held_session is not None and meta.session_id != s.held_session
        if not (newer or new_session):
            return False
        s.held_since = s.held_session = None
        _LOGGER.info("Volcast EV charger: %s — commands resumed",
                     "new plan after the pause" if newer else "new charging session")
        return True

    async def async_tick(self) -> None:
        if self._stopped or not self._started:
            return
        try:
            async with self._lock:
                await self._async_tick()
        except Exception as err:  # noqa: BLE001 — cykl ładowarki nie może wywrócić wołającego
            _LOGGER.warning("Volcast EV charger cycle failed (%s)", type(err).__name__)

    # ── cykl ──────────────────────────────────────────────────────────────
    def _gates_open(self, meta) -> bool:
        if meta is None or meta.control_enabled is not True or meta.plan_generated_at is None:
            return False
        return self._consent() is True and self._local_switch() is True and self._plan_only() is False

    def _held(self) -> bool:
        """Wstrzymanie: bez własnych zapisów do planu wygenerowanego po `held_since` (`_release_hold`)."""
        return self._state.held_since is not None

    def _warn_long_hold(self) -> None:
        """Bezpiecznik: chmura przelicza plan po każdym przyjętym zdarzeniu, więc wstrzymanie dłuższe niż
        `HOLD_WARN_S` znaczy, że plan nie dotarł — jedno ostrzeżenie, zapisy NIE wracają same."""
        at = _when(self._state.held_since)
        if at is None or self._hold_warned == self._state.held_since:
            return
        if (self._utcnow() - at).total_seconds() > HOLD_WARN_S:
            self._hold_warned = self._state.held_since
            _LOGGER.warning("Volcast EV charger: commands paused for over %d h after the charger stopped "
                            "outside Volcast and no new plan has arrived; the charger stays as it is",
                            HOLD_WARN_S // 3600)

    def _current_slot(self, meta, now: datetime):
        mine = [s for s in self._loads if s.device_id == meta.load_key
                and s.session_id == meta.session_id]
        return slot_for(now, mine)

    def _plan_age_s(self, now: datetime) -> float | None:
        """Czas od ostatniej odpowiedzi chmury; przed pierwszą od startu — od wygenerowania planu, ale
        dopiero po `STARTUP_GRACE_S` (wcześniej nieznany: bez zbędnego start/stop, gdy sieć wstaje po HA)."""
        if self._confirmed_mono is not None:
            return max(0.0, self._clock() - self._confirmed_mono)
        if self._started_mono is None or self._clock() - self._started_mono < STARTUP_GRACE_S:
            return None
        gen = getattr(self._meta, "plan_generated_at", None)
        return (now - gen).total_seconds() if gen is not None else None

    def _phase(self, mode: str) -> None:
        if self._slot_mode is not None and mode != self._slot_mode:
            self._state.last_action = None       # nowa faza planu: start/stop znowu raz
        self._slot_mode = mode

    async def _async_set_fallback(self, now: datetime | None) -> None:
        """`fallback_since` = początek trybu awaryjnego (None = poza nim); zapis tylko przy zmianie."""
        if (now is None) == (self._state.fallback_since is None):
            return
        self._state.fallback_since = now.isoformat() if now is not None else None
        if now is not None:
            self._fallback_latched = True
            _LOGGER.warning("Volcast EV charger: no fresh plan from the cloud and the deadline is near — "
                            "charging at full power until a fresh plan arrives")
        else:
            _LOGGER.info("Volcast EV charger: fallback charging ended")
        await self._async_save()
        self._signal()

    async def _async_tick(self) -> None:
        meta = self._meta
        charger = self._charger(meta.load_key) if self._gates_open(meta) else None
        status = self._hass.states.get(charger.status) if charger is not None else None
        if status is None or status.state in _UNAVAILABLE or self._held():
            # bramka zamknięta, ładowarka nieczytelna albo ręczna ingerencja: bez zapisów i bez trybu awaryjnego
            if self._held():
                self._warn_long_hold()
            await self._async_set_fallback(None)
            return
        now = self._utcnow()
        charging = is_charging(status.state)
        if should_force(now, meta, self._plan_age_s(now), status.state):
            await self._async_set_fallback(now)
            self._phase("fallback")
            # reguła awaryjna: pełna moc i start, nigdy stop
            await self._async_forced(None, meta, charger, charging, (meta.plan_generated_at, "fallback"))
            return
        await self._async_set_fallback(None)
        slot = self._current_slot(meta, now)
        if slot is None:
            return
        self._phase(slot.mode)
        if slot.mode == "forced":
            await self._async_forced(slot.power_w, meta, charger, charging, (meta.plan_generated_at, slot.start))
        elif slot.mode == "off" and charging and self._state.last_action != "stop" and not self._autostart_grace():
            if await self._async_command(charger, "stop"):
                await self._async_after_write(charger, setpoint=None, action="stop")

    # ── ręczna ingerencja ─────────────────────────────────────────────────
    def _watch(self, key: str) -> None:
        """Obserwacja encji `status` i `start_stop` ładowarki z planu (ponowna tylko przy zmianie encji)."""
        roles = self._roles(key) or {}
        ents = tuple(e for e in (roles.get("status"), roles.get("start_stop")) if isinstance(e, str) and e)
        if ents == self._watched or self._stopped:
            return
        self._unwatch()
        self._watched = ents
        if ents:
            self._watch_unsub = async_track_state_change_event(self._hass, list(ents), self.on_state_event)

    def _unwatch(self) -> None:
        if self._watch_unsub is not None:
            self._watch_unsub()
        self._watch_unsub = None
        self._watched = ()

    @callback
    def on_state_event(self, event) -> None:
        """Zmiana encji ładowarki: wstrzymanie i zdarzenie trafiają do stanu OD RAZU (przed następnym odczytem)."""
        try:
            change = self._classify(event)
        except Exception as err:  # noqa: BLE001 — obserwator nie może wywrócić pętli zdarzeń HA
            _LOGGER.warning("Volcast EV charger: manual change check failed (%s)", type(err).__name__)
            return
        if change is None:
            return
        action, session = change
        s = self._state
        now = self._utcnow().isoformat()
        # zdarzenie z aktorem w trakcie wstrzymania przesuwa jego początek (wygrywa ostatnia decyzja);
        # przejście bez aktora w trakcie wstrzymania nie dociera tutaj (`_unattended`)
        s.held_since, s.held_session = now, session
        if action is not None:
            s.manual_event, s.manual_at = action, now
            _LOGGER.warning("Volcast EV charger: manual %s outside Volcast — own commands paused until "
                            "the next plan", action)
        else:
            _LOGGER.info("Volcast EV charger stopped charging outside Volcast — own commands paused until "
                         "the next plan")
        self._signal()                           # pilna telemetria z `held_since` (i zdarzeniem)
        self._hass.async_create_background_task(self._async_save(), "volcast_ev_manual_save")

    def _classify(self, event) -> tuple[str | None, str] | None:
        """(zdarzenie `start`/`stop` albo None = samo wstrzymanie, sesja) albo None = bez zmian."""
        if self._stopped or not self._started:
            return None
        meta = self._meta
        if not self._gates_open(meta):
            return None
        charger = self._charger(meta.load_key)
        if charger is None:
            return None
        data = getattr(event, "data", None) or {}
        eid, new, old = data.get("entity_id"), data.get("new_state"), data.get("old_state")
        if new is None or eid is None or eid not in (charger.status, charger.start_stop):
            return None
        is_status = eid == charger.status
        ctx = getattr(new, "context", None) or getattr(event, "context", None)
        ours = self._writer.is_ours(getattr(ctx, "id", None))
        if is_status:
            self._note_charging(old, new, ours)
        if ours:
            return None
        if self._state.fallback_since is not None:
            mode = "forced"                      # tryb awaryjny działa jak slot `forced`
        else:
            slot = self._current_slot(meta, self._utcnow())
            mode = slot.mode if slot is not None else None
        if bool(getattr(ctx, "user_id", None) or getattr(ctx, "parent_id", None)):
            # użytkownik albo automatyzacja w HA
            if is_status:
                new_v, old_v = _status_word(new), _status_word(old)
            else:
                new_v, old_v = _command_word(eid, new), _command_word(eid, old)
            if new_v in ("start", "stop") and is_foreign_change(
                    ours=False, has_actor=True, new_value=new_v, expected=old_v) and (
                    self._held() or (new_v, mode) in (("stop", "forced"), ("start", "off"))):
                return new_v, meta.session_id    # w trakcie wstrzymania wygrywa ostatnia decyzja
        if not is_status:
            return None                          # encja start/stop bez aktora: impuls urządzenia, nie sygnał
        action = self._unattended(old, new, mode, meta)
        return None if action is False else (action, meta.session_id)

    def _note_charging(self, old, new, ours: bool) -> None:
        """Początek ładowania bez naszego zapisu (autostart po podłączeniu) — tolerancja w slocie `off`."""
        if status_kind(new) != "charging":
            self._charging_seen_mono = None
        elif status_kind(old) != "charging":
            self._charging_seen_mono = None if ours or self._recent_own_write() else self._clock()

    def _autostart_grace(self) -> bool:
        seen = self._charging_seen_mono
        return seen is not None and 0 <= self._clock() - seen < OFF_AUTOSTART_GRACE_S

    def _unattended(self, old, new, mode: str | None, meta) -> str | None | bool:
        """Przejście statusu bez aktora HA (aplikacja producenta, przycisk, karta RFID, samo urządzenie).
        Zwraca `stop`/`start` (zdarzenie i wstrzymanie), None (samo wstrzymanie) albo False (nic)."""
        before, after = status_kind(old), status_kind(new)
        if before == "charging" and after != "charging":
            if mode != "forced" or self._held() or self._recent_own_stop():
                return False
            # zdarzenie tylko przy pauzie/końcu, gdy plan świeży i do celu brakuje energii; błąd,
            # niedostępność, pauza auta, odłączenie albo możliwe echo naszego zapisu = samo wstrzymanie
            if after in ("paused", "end") and not self._recent_own_write() and self._energy_left(meta):
                return "stop"
            return None
        if after == "charging" and before not in (None, "charging", "error") and not self._recent_own_write() \
                and mode == "off" and self._state.last_action == "stop":
            return "start"                       # ponowny start po NASZYM stopie w tej fazie
        return False

    def _energy_left(self, meta) -> bool:
        """Świeży plan (odpowiedź chmury ≤ `FALLBACK_PLAN_AGE_S`) z więcej niż `END_REMAINING_KWH` do celu."""
        remaining = _num(meta.remaining_kwh)
        age = self._plan_age_s(self._utcnow())
        return remaining is not None and remaining > END_REMAINING_KWH \
            and age is not None and age <= FALLBACK_PLAN_AGE_S

    def _recent_own_write(self) -> bool:
        now = self._clock()
        return any(at <= now and now - at < OWN_ECHO_S for at in self._last_write.values())

    def _recent_own_stop(self) -> bool:
        at, now = self._own_stop_mono, self._clock()
        return at is not None and at <= now and now - at < OWN_ECHO_S

    def _signal(self) -> None:
        async_dispatcher_send(self._hass, SIGNAL_CONTROL_STATE_UPDATED.format(entry_id=self._entry.entry_id))

    def _charger(self, key: str) -> _Charger | None:
        """Role ładowarki; None, gdy brak statusu albo rola do zapisu jest spoza ładowarki."""
        roles = self._roles(key)
        if not roles or not isinstance(roles.get("status"), str):
            return None
        setpoint, start_stop = roles.get("setpoint"), roles.get("start_stop")
        writable = [e for e in (setpoint, start_stop) if e is not None]
        if any(e in self._forbidden or self._device_of(e) != key for e in writable):
            return None
        if setpoint is not None and not setpoint.startswith("number."):
            return None
        if start_stop is not None and start_stop.split(".", 1)[0] not in _START_STOP_DOMAINS:
            return None
        return _Charger(roles["status"], setpoint, start_stop)

    def _device_of(self, eid: str) -> str | None:
        """Urządzenie encji w rejestrze encji; brak wpisu albo błąd = None (ładowarka nieważna)."""
        try:
            ent = er.async_get(self._hass).entities.get(eid)
        except Exception:  # noqa: BLE001 — rejestr niedostępny: nie zapisujemy
            return None
        device = getattr(ent, "device_id", None) if ent is not None else None
        return device if isinstance(device, str) and device else None

    async def _async_forced(self, power_w, meta, charger: _Charger, charging: bool, rewrite_key: tuple) -> None:
        """Nastawa (moc slotu; None = pełna moc) i start. `rewrite_key` = (plan, slot) — jeden ponowny
        zapis nastawy przy rozjeździe odczytu na tę parę. Start raz na plan i fazę (`last_action`);
        ładowanie, które ustało bez naszego stopu, zatrzymuje wstrzymanie (`held_since`), nie ten cykl."""
        value = None
        tol = 0.0
        if charger.setpoint is not None:
            spec = self._setpoint_spec(meta, charger.setpoint)
            if spec is None:
                return
            value = self._setpoint_value(power_w, meta, spec)
            if value is None:
                _LOGGER.debug("Volcast EV charger: planned power below the charger minimum, skipping")
                return
            tol = max(spec[3] / 2, 0.01)
        written_value = None
        in_sync = True
        if value is not None:
            write = EntityWrite("ev_setpoint", charger.setpoint, "number", "set_value", {"value": value})
            actual = self._setpoint_actual(charger.setpoint)
            wrote = False
            if value != self._state.last_setpoint_a:
                wrote = in_sync = await self._async_write(write)
            elif actual is not None and abs(actual - value) <= tol:
                in_sync = True
            elif self._rewrite_key != rewrite_key:
                # inna wartość encji przy zgodnym cache: jeden ponowny zapis na plan i slot
                if self._throttled(charger.setpoint):
                    in_sync = False
                else:
                    self._rewrite_key = rewrite_key
                    wrote = in_sync = await self._async_write(write)
            else:
                if self._rewrite_warned != rewrite_key:
                    self._rewrite_warned = rewrite_key
                    _LOGGER.warning("Volcast EV charger keeps a different setpoint than planned; "
                                    "not rewriting it again in this slot")
                # start tylko przy nastawie NIŻSZEJ niż plan (wyższa mogłaby przekroczyć limit przyłącza)
                in_sync = actual is not None and actual <= value + tol
            if wrote:
                self._state.last_setpoint_a = value
                written_value = value
        action = None
        # Bez potwierdzonej nastawy nie startujemy — ładowanie na starej, wyższej nastawie to ryzyko.
        if in_sync and not charging and self._state.last_action != "start" and charger.start_stop is not None:
            if await self._async_command(charger, "start"):
                action = "start"
        if written_value is not None or action is not None:
            await self._async_after_write(charger, setpoint=written_value, action=action)

    def _setpoint_spec(self, meta, eid: str) -> tuple[str, float, float, float] | None:
        """(jednostka, min, max, krok) nastawy; None przy nieznanej jednostce albo sprzeczności z planem."""
        st = self._hass.states.get(eid)
        if st is None:
            return None
        unit = st.attributes.get("unit_of_measurement")
        unit = unit.strip() if isinstance(unit, str) else None
        if unit not in _SETPOINT_UNITS or (meta.control_unit is not None and meta.control_unit != unit):
            return None
        # Granice z planu tylko ZAWĘŻAJĄ granice encji — zapis spoza zakresu encji odrzuci usługa HA.
        los = [v for v in (meta.min, _num(st.attributes.get("min"))) if v is not None]
        his = [v for v in (meta.max, _num(st.attributes.get("max"))) if v is not None]
        step = meta.step if meta.step is not None and meta.step > 0 else _num(st.attributes.get("step"))
        return (unit, max(los) if los else 0.0, min(his) if his else math.inf,
                step if step is not None and step > 0 else 1.0)

    def _setpoint_actual(self, eid: str) -> float | None:
        """Bieżący odczyt encji nastawy (nieczytelna = None)."""
        st = self._hass.states.get(eid)
        return _num(st.state) if st is not None else None

    @staticmethod
    def _setpoint_value(power_w, meta, spec) -> float | None:
        unit, lo, hi, step = spec
        power = power_w if power_w is not None else meta.max_power_w
        if power is None:
            return hi if math.isfinite(hi) and hi > 0 else None     # „wykonawca decyduje” = pełna moc
        if unit == "A":
            return watts_to_amps(power, meta.phases, step, lo, hi)
        if power <= 0:
            return None
        floored = min(math.floor(power / step + 1e-9) * step, hi)
        return round(floored, 6) if floored > 0 and floored + 1e-9 >= lo else None

    async def _async_command(self, charger: _Charger, action: str) -> bool:
        eid = charger.start_stop
        if eid is None:
            return False
        domain = eid.split(".", 1)[0]
        if domain == "switch":
            write = EntityWrite(f"ev_{action}", eid, "switch", "turn_on" if action == "start" else "turn_off", {})
        else:
            st = self._hass.states.get(eid)
            option = pick_option(st.attributes.get("options") if st is not None else None, action)
            if option is None:
                _LOGGER.debug("Volcast EV charger: no %s option on the start/stop entity", action)
                return False
            write = EntityWrite(f"ev_{action}", eid, "select", "select_option", {"option": option})
        if not await self._async_write(write):
            return False
        self._state.last_action = action
        return True

    def _throttled(self, eid: str) -> bool:
        now = self._clock()
        at = self._last_write.get(eid)
        return at is not None and at <= now and now - at < LOADS_MIN_WRITE_INTERVAL_S

    async def _async_write(self, w: EntityWrite) -> bool:
        if w.entity_id in self._forbidden or w.domain not in ("number", "select", "switch"):
            return False          # bezpiecznik: nigdy encja falownika ani obca domena
        if self._held():
            return False          # ręczna ingerencja w trakcie cyklu: od teraz bez zapisów
        if self._throttled(w.entity_id):
            return False
        self._last_write[w.entity_id] = self._clock()
        if w.key == "ev_stop":
            self._own_stop_mono = self._last_write[w.entity_id]
        result = await self._writer.async_write(w)
        if result != OK:
            if w.entity_id not in self._failed:
                self._failed.add(w.entity_id)
                _LOGGER.warning("Volcast EV charger write failed (%s, %s)", w.key, result)
            return False
        self._failed.discard(w.entity_id)
        return True

    async def _async_after_write(self, charger: _Charger, *, setpoint: float | None, action: str | None) -> None:
        self._state.last_write_at = self._utcnow().isoformat()
        await self._async_save()
        if self._readback is not None and not self._readback.done():
            self._readback.cancel()
        self._readback = self._hass.async_create_background_task(
            self._async_readback(charger, setpoint, action), "volcast_ev_readback")

    async def _async_save(self) -> None:
        try:
            await self._store.async_save(self._state)
        except Exception as err:  # noqa: BLE001 — stan w pamięci zostaje, następny zapis spróbuje znowu
            _LOGGER.warning("Volcast EV charger state could not be saved (%s)", type(err).__name__)

    # ── odczyt kontrolny ──────────────────────────────────────────────────
    async def _async_readback(self, charger: _Charger, setpoint: float | None, action: str | None) -> None:
        await asyncio.sleep(self._readback_s)
        if self._stopped:
            return
        if setpoint is not None and charger.setpoint is not None:
            st = self._hass.states.get(charger.setpoint)
            actual = _num(st.state) if st is not None else None
            step = _num(st.attributes.get("step")) if st is not None else None
            tol = max((step or 0) / 2, 0.01)
            if actual is not None and abs(actual - setpoint) > tol:
                _LOGGER.warning("Volcast EV charger setpoint differs from the written value")
        if action is not None:
            st = self._hass.states.get(charger.status)
            if st is None or st.state in _UNAVAILABLE:
                return
            if action == "start" and not is_charging(st.state):
                _LOGGER.warning("Volcast EV charger did not start charging after the start command")
            elif action == "stop" and is_charging(st.state):
                _LOGGER.warning("Volcast EV charger did not stop charging after the stop command")

    async def async_readback_done(self) -> None:
        """Czeka na odczyt kontrolny w toku (testy, rozładunek)."""
        task = self._readback
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass
