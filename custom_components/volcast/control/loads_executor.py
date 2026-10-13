"""Wykonawca slotów ładowarki EV (`loads[]` z planu) przez encje innej integracji.

Osobny od wykonawcy falownika: własny magazyn (`LoadsStore`), własny pisarz i własny odstęp
zapisów. Zapisuje WYŁĄCZNIE encje ról potwierdzonej ładowarki (`ev_chargers` w opcjach):
nastawę (`number`) i start/stop (`select` albo `switch`). Encja falownika nigdy — rola wskazująca
encję z listy zakazanej albo z innej domeny unieważnia ładowarkę w całości (brak zapisów).

Bramki (wszystkie naraz): zgoda konta ∧ przełącznik sterowania w HA ∧ `loads_meta.control_enabled`
∧ status ładowarki czytelny. Slot dla „teraz” wybiera `slot_for` ([from, to)); brak slotu = brak
zapisu. `forced`: nastawa PRZED startem; start tylko raz na plan (`last_action`), więc ładowarka,
która nie rusza (auto pełne, kabel), nie dostaje komendy w kółko. `off`: stop tylko, gdy ładuje,
też raz — ręcznego ładowania nie blokujemy. Nowy plan (`plan_generated_at`) zeruje `last_action`.
`pv_surplus` nie jest jeszcze wykonywany (nic nie zapisujemy).

Odstęp: najwyżej jeden zapis na encję na `LOADS_MIN_WRITE_INTERVAL_S` (liczony od próby, także
nieudanej). Powtórek tej samej wartości pilnują `last_setpoint_a` / `last_action`, nie odstęp —
po obcym „stop” ten sam „start” musi dać się wysłać ponownie. Odczyt kontrolny po
`LOADS_READBACK_S` tylko loguje rozjazd (bez ponownego zapisu).

Status „ładuje” = słowo `charging` w stanie encji statusu (bez przeczenia „not”/„no”); stany
w innych językach albo CamelCase nie są rozpoznawane jako ładowanie.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable, Iterable, Mapping

from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from ..const import LOADS_EXECUTOR_INTERVAL_S, LOADS_MIN_WRITE_INTERVAL_S, LOADS_READBACK_S
from ..core.entity_map import EntityWrite
from ..core.ev.convert import slot_for, watts_to_amps
from ..core.write_sequence import OK
from .loads_store import LoadsState

_LOGGER = logging.getLogger(__name__)

_UNAVAILABLE = ("unavailable", "unknown")
_TOKEN_RE = re.compile(r"[^a-z0-9]+")
_HINTS = {"start": ("start", "resume"), "stop": ("stop", "pause")}
_NEGATIONS = frozenset({"not", "no", "non"})
_SETPOINT_UNITS = ("A", "W")
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
    return "charging" in tokens and not _NEGATIONS.intersection(tokens)


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


@dataclass(frozen=True)
class _Charger:
    status: str
    setpoint: str | None
    start_stop: str | None


class LoadsExecutor:
    def __init__(self, hass, entry, writer, roles_provider: Callable[[str], dict | None], store,
                 consent_getter: Callable[[], Any], local_switch_getter: Callable[[], Any], *,
                 forbidden_entities: Iterable[str] = (), clock: Callable[[], float] = time.monotonic,
                 utcnow=dt_util.utcnow, readback_s: float = LOADS_READBACK_S) -> None:
        self._hass, self._entry = hass, entry
        self._writer = writer
        self._roles = roles_provider
        self._store = store
        self._consent = consent_getter
        self._local_switch = local_switch_getter
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

    @property
    def state(self) -> LoadsState:
        return self._state

    # ── cykl życia ────────────────────────────────────────────────────────
    async def async_start(self, schedule=None) -> None:
        """Wczytuje stan; `schedule` — plan z magazynu wykonawcy falownika (praca bez łącza)."""
        if self._started:
            return
        self._started = True
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
        if self._readback is not None and not self._readback.done():
            self._readback.cancel()

    async def _async_timer(self, _now=None) -> None:
        await self.async_tick()

    # ── wejścia ───────────────────────────────────────────────────────────
    async def async_on_plan(self, schedule) -> None:
        if self._stopped:
            return
        async with self._lock:
            if self._remember(schedule):
                await self._async_save()
        await self.async_tick()

    def _remember(self, schedule) -> bool:
        """Sloty i metadane z planu; True, gdy to nowy plan ładowarki (stan do zapisania)."""
        self._loads = tuple(getattr(schedule, "loads", None) or ())
        meta = getattr(schedule, "loads_meta", None)
        self._meta = meta
        if meta is None:
            return False
        gen = meta.plan_generated_at.isoformat() if meta.plan_generated_at is not None else None
        if gen == self._state.plan_generated_at:
            return False
        self._state.plan_generated_at = gen
        self._state.last_action = None          # nowy plan może znowu wystartować/zatrzymać
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
    async def _async_tick(self) -> None:
        meta = self._meta
        if meta is None or not meta.control_enabled:
            return
        if self._consent() is not True or self._local_switch() is not True:
            return
        charger = self._charger(meta.load_key)
        if charger is None:
            return
        status = self._hass.states.get(charger.status)
        if status is None or status.state in _UNAVAILABLE:
            return
        mine = [s for s in self._loads if s.device_id == meta.load_key
                and (s.session_id is None or s.session_id == meta.session_id)]
        slot = slot_for(self._utcnow(), mine)
        if slot is None:
            return
        charging = is_charging(status.state)
        if slot.mode == "forced":
            await self._async_forced(slot, meta, charger, charging)
        elif slot.mode == "off" and charging and self._state.last_action != "stop":
            if await self._async_command(charger, "stop"):
                await self._async_after_write(charger, setpoint=None, action="stop")

    def _charger(self, key: str) -> _Charger | None:
        """Role ładowarki; None, gdy brak statusu albo rola do zapisu jest spoza ładowarki."""
        roles = self._roles(key)
        if not roles or not isinstance(roles.get("status"), str):
            return None
        setpoint, start_stop = roles.get("setpoint"), roles.get("start_stop")
        writable = [e for e in (setpoint, start_stop) if e is not None]
        if any(e in self._forbidden for e in writable):
            return None
        if setpoint is not None and not setpoint.startswith("number."):
            return None
        if start_stop is not None and start_stop.split(".", 1)[0] not in _START_STOP_DOMAINS:
            return None
        return _Charger(roles["status"], setpoint, start_stop)

    async def _async_forced(self, slot, meta, charger: _Charger, charging: bool) -> None:
        value = None
        if charger.setpoint is not None:
            spec = self._setpoint_spec(meta, charger.setpoint)
            if spec is None:
                return
            value = self._setpoint_value(slot, meta, spec)
            if value is None:
                _LOGGER.debug("Volcast EV charger: planned power below the charger minimum, skipping")
                return
        written_value = None
        in_sync = True
        if value is not None and value != self._state.last_setpoint_a:
            ok = await self._async_write(EntityWrite("ev_setpoint", charger.setpoint, "number", "set_value",
                                                     {"value": value}))
            if ok:
                self._state.last_setpoint_a = value
                written_value = value
            in_sync = ok
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
        lo = meta.min if meta.min is not None else _num(st.attributes.get("min"))
        hi = meta.max if meta.max is not None else _num(st.attributes.get("max"))
        step = meta.step if meta.step is not None and meta.step > 0 else _num(st.attributes.get("step"))
        return (unit, lo if lo is not None else 0.0, hi if hi is not None else math.inf,
                step if step is not None and step > 0 else 1.0)

    @staticmethod
    def _setpoint_value(slot, meta, spec) -> float | None:
        unit, lo, hi, step = spec
        power = slot.power_w if slot.power_w is not None else meta.max_power_w
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

    async def _async_write(self, w: EntityWrite) -> bool:
        if w.entity_id in self._forbidden or w.domain not in ("number", "select", "switch"):
            return False          # bezpiecznik: nigdy encja falownika ani obca domena
        now = self._clock()
        at = self._last_write.get(w.entity_id)
        if at is not None and at <= now and now - at < LOADS_MIN_WRITE_INTERVAL_S:
            return False
        self._last_write[w.entity_id] = now
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
