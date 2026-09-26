"""Jeden cykl sterowania: plan → intencja → guardy → dopasowanie → throttling → zapisy.

Czysta decyzja; wywołania usług robi warstwa HA. Każdy wyjątek = cykl bez zapisów
(fail-closed). Bramki (zgoda, przełącznik, wybrany tryb, weryfikacja, pauza) nie
skracają obliczeń — decyzja jest liczona zawsze, żeby „próba na sucho" pokazywała,
co by poszło; zapis wykonuje się tylko przy statusie WRITE.

Tryb i jego nastawa mocy to JEDNA GRUPA: idą razem albo wcale (zmierzone: standby
honoruje Xset jako nastawę ładowania, więc tryb na starej mocy albo moc w starym
trybie ładuje z sieci). Zasady, które trzymają grupę:
* każdy klucz zapisu profilu musi mieć encję — inaczej sterowania nie ma wcale;
* zmieniony parametr, który w tym cyklu nie pójdzie (interwał I-6, jednostka encji
  się zmieniła), wstrzymuje zmianę trybu, a z nią moc;
* zmiana trybu wstrzymana (I-6, I-8) wstrzymuje moc — chyba że falownik już ma tryb
  z planu, wtedy sama moc jest zwykłą korektą nastawy;
* tryb albo moc, których falownik nie obsługuje, wyłączają intencję na całą sesję;
* parametr niedopasowalny do zakresu encji blokuje cały cykl.
Awaria zapisu w trakcie cyklu to już sprawa wykonawcy grupowego (`group_writes`):
cykl układa grupę w bezpiecznej kolejności i podaje zapisy cofające (`restore`).

Zatrzask rezerwy dostaje WYŁĄCZNIE odczyt, który przeszedł sanityzację i świeżość
strażników (I-10, I-9) — strażnicy liczą najpierw próbę z założonym zatrzaskiem;
blokada sprzed I-1 nie zależy od zatrzasku, więc próba rozstrzyga ją bez karmienia
zatrzasku nieużywalnym SoC. Czas `now_mono` pochodzi z zegara monotonicznego.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Mapping

from ..engines.mode_setpoint import map_slot
from ..entity_map import EntityWrite, entity_value
from ..guard_state import DirectionLimiter, WriteThrottle
from ..guards import GuardContext, GuardResult, apply_guards, temperature_ok
from ..slot import Schedule, effective_action
from ..write_sequence import WriteReport
from .caps import missing_write_keys
from ..params import Params
from .entity_fit import control_writes, fit_params
from .group_writes import order_group, power_first
from .latch import ReserveLatch

WRITE, DRY_RUN, IDLE, BLOCKED, ERROR = "write", "dry_run", "idle", "blocked", "error"

# Tryb i nastawa, która nadaje mu znaczenie — zapisywane razem albo wcale.
_MODE_GROUP = frozenset({"mode", "power_w"})
_NO_READING = ("unavailable", "unknown", "")
# Kwant rejestru: plan niesie ułamki (625,6 W), falownik pokaże 626 — to nie rozjazd.
_QUANTUM = 1.0


@dataclass
class ControlMemory:
    throttle: WriteThrottle
    limiter: DirectionLimiter
    latch: ReserveLatch
    unsupported: set[str] = field(default_factory=set)
    paused_until: float | None = None
    last_written: dict[str, float | str] = field(default_factory=dict)

    @classmethod
    def for_profile(cls, profile) -> "ControlMemory":
        return cls(WriteThrottle(profile.min_interval_s),
                   DirectionLimiter(max(1, profile.max_direction_changes_per_hour)), ReserveLatch())


@dataclass(frozen=True)
class Gates:
    consent: bool | None
    local_switch: bool
    control_mode: str | None
    verified: bool


@dataclass(frozen=True)
class Telemetry:
    soc: float | None
    soc_age_s: float
    battery_temp_c: float | None
    previous_soc: float | None = None
    previous_soc_gap_s: float | None = None


@dataclass(frozen=True)
class Limits:
    rated_power_w: float
    max_charge_w: float = 0.0
    max_export_w: float = 0.0


@dataclass(frozen=True)
class EntityContext:
    domain: str
    mapped: Mapping[str, str]
    units: Mapping[str, str | None]
    attrs: Mapping[str, Mapping[str, Any]]
    readings: Mapping[str, float | str]


@dataclass
class CycleDecision:
    status: str
    reason: str
    writes: list[EntityWrite] = field(default_factory=list)
    flat: dict[str, float | str] = field(default_factory=dict)
    direction: str | None = None
    intent: str | None = None
    fallback: bool = False
    guard: GuardResult | None = None
    adjusted: tuple[str, ...] = ()
    unmapped: tuple[str, ...] = ()
    dropped_unsupported: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    # klucz grupy → zapis przywracający poprzednią wartość (dla wykonawcy grupowego)
    restore: dict[str, EntityWrite] = field(default_factory=dict)

    def summary(self) -> dict:
        """Mały, JSON-owalny obraz decyzji (telemetria, atrybuty encji) — bez nastaw i notatek strażnika."""
        return {
            "status": self.status, "reason": self.reason, "intent": self.intent,
            "fallback": self.fallback,
            "guard": None if self.guard is None else {"status": self.guard.status,
                                                      "invariant": self.guard.invariant},
            "would_write": [w.key for w in self.writes], "adjusted": list(self.adjusted),
            "unmapped": list(self.unmapped), "dropped_unsupported": list(self.dropped_unsupported),
            "notes": list(self.notes),
        }


def same_value(a: float | str, b: float | str) -> bool:
    """Czy odczyt z urządzenia równa się nastawie (tryb po nazwie, liczby z kwantem rejestru)."""
    if isinstance(a, str) or isinstance(b, str):
        return a == b
    return abs(float(a) - float(b)) < _QUANTUM


def _gate_reason(g: Gates, memory: ControlMemory, now_mono: float) -> str | None:
    if g.consent is not True:
        return "no_consent"
    if not g.local_switch:
        return "local_off"
    if memory.paused_until is not None and now_mono < memory.paused_until:
        return "paused"
    if not g.verified:
        return "unverified_profile"
    return None


def _device_view(readings: Mapping[str, Any], flat: Mapping[str, float | str], profile,
                 ents: EntityContext) -> dict[str, float | str]:
    """Odczyty kluczy planu w postaci `Params.flatten()`; nieczytelne znikają (brak ≠ rozjazd).

    Wykonawca podaje odczyty już znormalizowane; surowy stan encji (`unavailable`,
    `on`/`off`, opcja wyboru, liczba jako tekst) przechodzi przez ten sam przekład co
    odczyt encji. Liczba tam, gdzie tryb jest nazwą, zostaje — uzgadnianie odrzuci ją
    jako błąd wołającego (cykl bez zapisów). Czytelna opcja spoza profilu to odczyt
    RÓŻNY od każdego trybu (znacznik `?opcja`), nie brak odczytu — nasz tryb wraca.
    """
    out: dict[str, float | str] = {}
    for key in flat:
        if key not in readings:
            continue
        value = readings[key]
        if value is None:
            continue
        if isinstance(value, bool):
            value = 1.0 if value else 0.0
        elif isinstance(value, (int, float)):
            if not math.isfinite(value):
                continue
            value = float(value)
        elif isinstance(value, str) and not (key == "mode" and value in profile.modes):
            try:
                value = entity_value(key, value, profile, ents.domain, unit=ents.units.get(key))
            except (KeyError, ValueError, TypeError):
                value = None
            if value is None:
                if key != "mode" or readings[key] in _NO_READING:
                    continue
                value = "?" + readings[key]
        out[key] = value
    return out


def decide_cycle(*, profile, schedule: Schedule | None, now_utc: datetime, now_mono: float,
                 tele: Telemetry, limits: Limits, ents: EntityContext, gates: Gates,
                 memory: ControlMemory) -> CycleDecision:
    try:
        return _decide(profile, schedule, now_utc, now_mono, tele, limits, ents, gates, memory)
    except Exception as err:  # noqa: BLE001 — każdy błąd decyzji = brak zapisów
        return CycleDecision(ERROR, f"exception:{type(err).__name__}")


def _decide(profile, schedule, now_utc, now_mono, tele, limits, ents, gates, memory) -> CycleDecision:
    if gates.control_mode != "entities":
        return CycleDecision(IDLE, "no_mode_chosen")
    if profile.control_model != "mode_setpoint":
        return CycleDecision(IDLE, "tou_preview_only")
    missing = missing_write_keys(profile, ents.mapped)
    if missing:
        return CycleDecision(IDLE, "missing_entities", unmapped=missing)
    if schedule is None:
        return CycleDecision(IDLE, "no_plan")

    slot, is_fallback = schedule.effective_slot(now_utc)
    mapped_slot = map_slot(slot, profile, limits.rated_power_w)
    common: dict[str, Any] = dict(intent=mapped_slot.intent, fallback=is_fallback)
    # SoC i temperatura to osobne encje: świeży SoC nic nie mówi o temperaturze.
    if "battery_temp_c" in ents.mapped and tele.battery_temp_c is None:
        return CycleDecision(BLOCKED, "temperature_unknown", **common)

    reserve = schedule.fallback.soc_reserve
    ctx = GuardContext(
        soc=tele.soc, soc_age_s=tele.soc_age_s,
        temperature_ok=temperature_ok(tele.battery_temp_c, profile),
        soc_reserve=reserve, action=effective_action(slot), price_pln_kwh=slot.price_pln_kwh,
        max_charge_w=limits.max_charge_w, max_export_w=limits.max_export_w,
        max_state_age_s=profile.max_state_age_s, previous_soc=tele.previous_soc,
        previous_soc_gap_s=tele.previous_soc_gap_s, reserve_engaged=True)
    # Próba z założonym zatrzaskiem: odrzucenie sprzed I-1 (I-10, I-9, I-3, I-7) nie zależy
    # od zatrzasku, a zatrzask nie może zobaczyć odczytu, którego strażnicy nie przyjęli.
    probe = apply_guards(mapped_slot.params, ctx, profile)
    if not probe.write_allowed:
        return CycleDecision(BLOCKED, f"guard:{probe.invariant}", guard=probe, **common)
    engaged = memory.latch.engaged(tele.soc, reserve, now_mono)
    guard = apply_guards(mapped_slot.params, replace(ctx, reserve_engaged=engaged), profile)
    common["guard"] = guard
    if not guard.write_allowed:
        return CycleDecision(BLOCKED, f"guard:{guard.invariant}", **common)

    params, adjusted, unfit = fit_params(guard.params, profile, ents.domain, ents.mapped,
                                         ents.units, ents.attrs)
    if unfit:
        # Klucz, którego encja nie przyjmie, to warunek trybu — tryb nie idzie, nic nie idzie.
        return CycleDecision(BLOCKED, "entity_range_unknown", unmapped=unfit, **common)
    flat = params.flatten()
    unsupported = _unsupported_group(flat, profile, ents, memory)
    if unsupported:
        reason = "mode_unsupported" if unsupported[0].startswith("mode:") else "power_unsupported"
        return CycleDecision(BLOCKED, reason, flat=flat,
                             dropped_unsupported=unsupported, **common)
    device = _device_view(ents.readings, flat, profile, ents)
    # Uzgodnienie z tym, co falownik naprawdę ma (tylko klucze planu, tylko czytelne).
    # Zły typ odczytu rzuca TypeError po drodze (pamięć wcześniejszych kluczy mogła już
    # zniknąć) — cykl kończy się bez zapisów, następny też, więc to bezpieczne.
    memory.throttle.reconcile(device)
    # To, co falownik już ma, nie jedzie wcale — także po restarcie, gdy pamięć
    # throttlingu jest pusta (inaczej każdy reload = zapis wszystkich nastaw do NVM).
    settled = {k for k in flat if k in device and same_value(device[k], flat[k])}
    due = memory.throttle.filter(flat, now_mono) - settled
    # Do zmiany na falowniku: to, co pójdzie teraz, i to, co czeka w interwale I-6.
    need = due | (memory.throttle.pending(flat, now_mono) - settled)
    _, runtime_unmapped = control_writes(params, profile, ents.domain, ents.mapped,
                                         keys=None, units=ents.units)
    allowed = due - memory.unsupported - set(runtime_unmapped)

    notes: list[str] = []
    held_by = [k for k in runtime_unmapped if k != "mode"]
    # Zmieniony warunek, który w tym cyklu nie dojdzie („nieobsługiwany" nie dojdzie nigdy).
    unsettled = (need - allowed - memory.unsupported) - {"mode"}
    if held_by or ("mode" in need and unsettled):
        allowed.discard("mode")
        notes.append("mode_held")
    direction = profile.mode_direction(params.mode) if params.mode is not None else None
    if "mode" in allowed and direction is not None and not memory.limiter.allows(direction, now_mono):
        allowed.discard("mode")
        notes.append("I-8")
    # Grupa: członek, który musi się zmienić, a nie pójdzie → nie idzie żaden.
    if _MODE_GROUP & (need - allowed) and _MODE_GROUP & allowed:
        allowed -= _MODE_GROUP
        notes.append("group_held")
    writes, unmapped = control_writes(params, profile, ents.domain, ents.mapped,
                                      keys=allowed, units=ents.units)
    writes, restore = _group_layout(writes, params, device, profile, ents, memory)
    reason = _gate_reason(gates, memory, now_mono)
    status = WRITE if reason is None else DRY_RUN
    if status == WRITE and not writes:
        status, reason = IDLE, "nothing_to_write"
    return CycleDecision(
        status, reason or "ok", writes=writes, flat=flat,
        direction=direction if any(w.key == "mode" for w in writes) else None,
        adjusted=adjusted, unmapped=tuple(dict.fromkeys([*held_by, *unmapped])),
        dropped_unsupported=tuple(sorted(memory.unsupported & set(flat))), notes=tuple(notes),
        restore=restore, **common)


def _group_layout(writes: list[EntityWrite], params: Params, device: Mapping[str, float | str],
                  profile, ents: EntityContext, memory: ControlMemory
                  ) -> tuple[list[EntityWrite], dict[str, EntityWrite]]:
    """Grupa na końcu, w bezpiecznej kolejności, i zapisy cofające do stanu z urządzenia.

    Poprzednia wartość: odczyt z urządzenia, a bez odczytu — nasz ostatni zapis. Tryb
    spoza profilu (znacznik `?opcja`) nie ma zapisu cofającego — nie odtwarzamy obcego trybu.
    """
    prev_power = device.get("power_w")
    if not isinstance(prev_power, float):
        prev_power = memory.last_written.get("power_w")
        prev_power = prev_power if isinstance(prev_power, float) else None
    prev_mode = device.get("mode")
    if prev_mode not in profile.modes:
        prev_mode = memory.last_written.get("mode")
        prev_mode = prev_mode if prev_mode in profile.modes else None
    new_power = params.power_w
    first = power_first(new_power, prev_power) if new_power is not None else False
    ordered = order_group(writes, power_first=first)
    keys = {w.key for w in ordered}
    if not {"mode", "power_w"} <= keys:
        return ordered, {}
    back, _ = control_writes(Params(mode=prev_mode, power_w=prev_power), profile, ents.domain,
                             ents.mapped, keys=None, units=ents.units)
    return ordered, {w.key: w for w in back}


def _unsupported_group(flat: Mapping[str, float | str], profile, ents: EntityContext,
                       memory: ControlMemory) -> tuple[str, ...]:
    """Członkowie grupy, których falownik nie obsługuje: tryb per opcja, moc per klucz.

    Opcję trybu sprawdzamy też z góry na liście `options` encji — inaczej pierwszy cykl
    zapisałby moc przed trybem, którego falownik i tak nie przyjmie.
    """
    out: list[str] = []
    mode = flat.get("mode")
    if isinstance(mode, str):
        options = (ents.attrs.get(ents.mapped.get("mode", "")) or {}).get("options")
        known = not isinstance(options, (list, tuple)) or profile.modes[mode].ha_option in options
        if f"mode:{mode}" in memory.unsupported or not known:
            out.append(f"mode:{mode}")
    if "power_w" in flat and "power_w" in memory.unsupported:
        out.append("power_w")
    return tuple(out)


def commit(decision: CycleDecision, report: WriteReport, memory: ControlMemory, now_mono: float) -> None:
    """Pamięć po wykonaniu: throttling tylko dla zapisów udanych, I-8 tylko gdy tryb poszedł.

    Decyzja inna niż WRITE niczego nie wykonała — nie zostawia śladu w pamięci.
    """
    if decision.status != WRITE:
        return
    memory.throttle.record(decision.flat, report.written, now_mono)
    for key in report.written:
        if key in decision.flat:
            memory.last_written[key] = decision.flat[key]
    # Tryb nieobsługiwany zapamiętujemy per opcja: inne tryby dalej działają.
    mode = decision.flat.get("mode")
    memory.unsupported |= {f"mode:{mode}" if key == "mode" and isinstance(mode, str) else key
                           for key in report.unsupported}
    if decision.direction is not None and not report.mode_held:
        memory.limiter.record(decision.direction, now_mono)
