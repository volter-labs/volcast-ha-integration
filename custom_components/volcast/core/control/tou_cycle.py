"""Cykl okien czasowych (programy TOU, Deye) w trybie bezpośrednim.

plan → `compress(anchor="day")` (stała siatka doby na zegarze ściennym: starty zmieniają się
tylko ze zmianą planu) → strażnicy (I-9 świeżość, I-10 kształt programów) → porównanie z
odczytem (tylko zmienione pola; moc i SoC w tolerancji profilu, wartość przycięta przez
urządzenie po OK_ADJUSTED to wartość osiągnięta) → wstrzymania → sekwencja:
włącznik OFF → programy → włącznik ON na końcu.

Zasady oszczędzania harmonogramu właściciela i pamięci nieulotnej:
* pole wstrzymane (interwał I-6, budżet NVM, pamięć prawdziwej odmowy) = ŻADNEJ sekwencji —
  harmonogram nie jest wyłączany na darmo, a programy w falowniku jadą dalej;
* przepisanie działającego harmonogramu najwyżej raz na `REWRITE_INTERVAL_S`, chyba że zmiana
  idzie w stronę bezpieczną (zdjęcie ładowania z sieci, mniejsza moc);
* OFF jest w sekwencji zawsze, gdy zmieniają się programy — stan włącznika rozstrzyga świeży
  odczyt pisarza (bez ramki, gdy już wyłączony), nie odczyt z cyklu, który mógł być sprzed
  naszego ostatniego zapisu;
* włącznik jest kluczem pamięci (`tou_enabled`): ostatni zapis, interwał, niepewność, rozjazd.

Czysta decyzja; każdy wyjątek = decyzja `error` bez zapisów (fail-closed).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, tzinfo

from ..engines.time_window import compress
from ..guards import GuardContext, GuardResult, apply_guards, temperature_ok
from ..params import Params, TouProgram
from ..registers import RegisterWrite, encode_tou_enable, encode_writes
from ..slot import effective_action
from .cycle import (
    BLOCKED, DRY_RUN, ERROR, IDLE, WRITE, ControlMemory, _adjusted_reached, _denied_again, _gate_reason,
    note_denied, same_value)
from .tou_writes import ENABLE, TouReport

EN_KEY = "tou_enabled"                     # włącznik w widoku urządzenia i w pamięci (1.0 / 0.0)
REWRITE_INTERVAL_S = 3600.0
# prawdziwa odmowa pola programu: odwrót do 6 h (każda próba to OFF i powrót do programów właściciela)
TOU_DENIED_MAX_S = 6 * 3600.0


@dataclass
class TouDecision:
    status: str
    reason: str
    writes: list[RegisterWrite] = field(default_factory=list)
    flat: dict[str, float | str] = field(default_factory=dict)
    programs: tuple[TouProgram, ...] = ()
    lost_value_pln: float | None = None
    notes: tuple[str, ...] = ()
    guard: GuardResult | None = None
    # pola wstrzymane z góry (interwał I-6, budżet NVM) — dla `run_tou_writes(pre_held=…)`
    pre_held: tuple[str, ...] = ()
    # widok urządzenia użyty w decyzji (pamięć odmowy w `commit_tou`)
    device: dict[str, float | str] = field(default_factory=dict)

    def summary(self) -> dict:
        return {
            "status": self.status, "reason": self.reason,
            "guard": None if self.guard is None else {"status": self.guard.status,
                                                      "invariant": self.guard.invariant},
            "would_write": [w.key for w in self.writes], "held": list(self.pre_held),
            "notes": list(self.notes), "lost_value_pln": self.lost_value_pln,
            "programs": [{"start_min": p.start_min, "power_w": p.power_w, "soc": p.soc,
                          "grid_charge": p.grid_charge} for p in self.programs],
        }


def decide_tou_cycle(*, profile, schedule, now_utc: datetime, now_mono: float, tz: tzinfo, tele, limits,
                     reading, gates, memory: ControlMemory, owner_word: int | None) -> TouDecision:
    try:
        return _decide(profile, schedule, now_utc, now_mono, tz, tele, limits, reading, gates, memory,
                       owner_word)
    except Exception as err:  # noqa: BLE001 — każdy błąd decyzji = brak zapisów
        return TouDecision(ERROR, f"exception:{type(err).__name__}")


def _decide(profile, schedule, now_utc, now_mono, tz, tele, limits, reading, gates, memory,
            owner_word) -> TouDecision:
    if gates.control_mode != "direct":
        return TouDecision(IDLE, "no_mode_chosen")
    if profile.control_model != "time_window":
        return TouDecision(IDLE, "not_time_window")
    unsupported = "tou" in memory.unsupported
    if schedule is None:
        return TouDecision(BLOCKED, "tou_unsupported") if unsupported else TouDecision(IDLE, "no_plan")
    if reading.programs is None or reading.tou_enabled is None:
        return TouDecision(BLOCKED, "tou_unsupported" if unsupported else "tou_unreadable")
    if memory.tou_write_end is not None and reading.at_mono < memory.tou_write_end:
        # Odczyt sprzed końca naszego zapisu: ani decyzja, ani uzgadnianie pamięci na jego podstawie.
        return TouDecision(IDLE, "stale_reading")
    en = profile.raw["write"]["tou_enable"]
    word = reading.image.words(en["addr"], 1)[0]

    reserve = schedule.fallback.soc_reserve
    cr = compress(schedule, now_utc, profile, soc_reserve=reserve, rated_power_w=limits.rated_power_w,
                  tz=tz, anchor="day")
    notes = [f"degraded:{k}" for k in sorted(cr.degraded)]
    common = dict(lost_value_pln=cr.lost_value_pln)
    if "battery_temp_c" in reading.values and tele.battery_temp_c is None:
        return TouDecision(BLOCKED, "temperature_unknown", notes=tuple(notes), **common)
    slot, _ = schedule.effective_slot(now_utc)
    ctx = GuardContext(
        soc=tele.soc, soc_age_s=tele.soc_age_s, temperature_ok=temperature_ok(tele.battery_temp_c, profile),
        soc_reserve=reserve, action=effective_action(slot), price_pln_kwh=slot.price_pln_kwh,
        max_charge_w=limits.max_charge_w, max_export_w=limits.max_export_w,
        max_state_age_s=profile.max_state_age_s, previous_soc=tele.previous_soc,
        previous_soc_gap_s=tele.previous_soc_gap_s, reserve_engaged=True)
    guard = apply_guards(Params(tou=cr.programs), ctx, profile)
    if not guard.write_allowed:
        return TouDecision(BLOCKED, f"guard:{guard.invariant}", guard=guard, notes=tuple(notes), **common)
    programs = guard.params.tou
    flat = Params(tou=programs).flatten()
    device = {k: reading.device[k] for k in (*flat, EN_KEY) if k in reading.device}
    common.update(programs=programs, guard=guard, device=device)
    # Działający harmonogram robi gdzieś w dobie więcej niż plan (ładuje z sieci, mocniej, wyżej,
    # rozładowuje mocniej/głębiej) — zmiana w stronę bezpieczną, której nic nie może wstrzymać.
    safety = bool(reading.tou_enabled) and _toward_safety(reading.programs, programs, profile)
    if unsupported:
        if safety:
            return _safety_off(word, profile, gates, memory, now_mono, notes, flat, common)
        return TouDecision(BLOCKED, "tou_unsupported", notes=tuple(notes), **common)
    memory.throttle.reconcile(device)
    memory.uncertain -= {k for k in memory.uncertain if k in reading.device}
    settled = {k for k in flat if k in device and (_within_tolerance(k, flat[k], device[k], profile)
                                                   or _adjusted_reached(memory, k, flat[k], device[k]))}
    changed = set(flat) - settled
    enable_off = not reading.tou_enabled
    if not changed and not enable_off:
        return TouDecision(IDLE, "nothing_to_write", flat=flat, notes=tuple(notes), **common)
    held: set[str] = set()
    if changed:
        waiting = changed - (memory.throttle.filter(flat, now_mono) - settled)
        if waiting:
            held |= waiting
            notes.append("I-6")
        refused = {k for k in changed if _denied_again(memory, k, flat[k], device.get(k), now_mono)}
        if refused:
            held |= refused
            notes.append("denied_hold")
    elif memory.tou_enable_at is not None and 0.0 <= now_mono - memory.tou_enable_at < profile.min_interval_s:
        held.add(ENABLE)                        # ponowne włączenie po wyłączeniu z zewnątrz: po I-6
        notes.append("I-6")
    if memory.budget is not None:
        blocked = memory.budget.exhausted(changed | {ENABLE}, now_utc.timestamp())
        if blocked:
            held |= blocked
            notes.append("nvm_budget")
    if held:
        if safety:
            # Przepisania w stronę bezpieczną nie da się zrobić w całości: harmonogram OFF (samokonsumpcja),
            # poza interwałem, pamięcią odmowy i budżetem; zostaje OFF do udanego pełnego przepisania.
            return _safety_off(word, profile, gates, memory, now_mono, notes, flat, common)
        # Sekwencja przerwana w połowie zostawiłaby harmonogram wyłączony — nic nie idzie.
        return TouDecision(IDLE, "held", flat=flat, notes=tuple(notes), pre_held=tuple(sorted(held)), **common)
    if changed and not enable_off and not safety \
            and memory.tou_rewrite_at is not None and 0.0 <= now_mono - memory.tou_rewrite_at < REWRITE_INTERVAL_S:
        notes.append("tou_rewrite_interval")
        return TouDecision(IDLE, "held", flat=flat, notes=tuple(notes), **common)
    writes: list[RegisterWrite] = []
    after = word
    if changed:
        off = encode_tou_enable(False, word, profile)
        writes.append(off)
        after = off.value
        writes.extend(encode_writes(Params(tou=programs), profile, keys=changed, current=reading.image))
    writes.append(encode_tou_enable(True, after, profile))
    reason = _gate_reason(gates, memory, now_mono)
    status = WRITE if reason is None else DRY_RUN
    return TouDecision(status, reason or "ok", writes=writes, flat=flat, notes=tuple(notes), **common)


def _field(key: str) -> str:
    return key.rsplit(".", 1)[-1]


def _within_tolerance(key: str, planned, current, profile) -> bool:
    """Pole programu zgodne z planem: moc w tolerancji mocy profilu, SoC nie niżej niż plan
    (podłoga rezerwy) i najwyżej o tolerancję SoC wyżej; start i ładowanie z sieci dokładnie."""
    if isinstance(planned, str) or isinstance(current, str):
        return planned == current
    name = _field(key)
    if name == "power_w":
        return abs(float(current) - float(planned)) <= profile.power_tolerance_w
    if name == "soc":
        return float(planned) - 1.0 < float(current) <= float(planned) + profile.soc_tolerance_pp
    return same_value(planned, current)


def _active(programs, minute: int):
    ordered = sorted(programs, key=lambda p: p.start_min)
    cur = ordered[-1]
    for p in ordered:
        if p.start_min <= minute:
            cur = p
    return cur


def _toward_safety(device_programs, planned, profile) -> bool:
    """Po pokryciu doby (co krok profilu, programy cykliczne wg startu): czy gdziekolwiek urządzenie
    ładuje z sieci, gdzie plan nie ładuje, albo mocniej / do wyższego SoC — albo (bez ładowania z
    sieci) rozładowuje mocniej lub do niższej podłogi niż plan. Skrócenie lub zdjęcie okna ładowania
    też tu wpada (zmiana startu)."""
    if not device_programs or not planned:
        return False
    tol_p, tol_s = profile.power_tolerance_w, profile.soc_tolerance_pp
    for minute in range(0, 1440, max(1, profile.time_step_min)):
        d, p = _active(device_programs, minute), _active(planned, minute)
        if d.grid_charge:
            if not p.grid_charge or p.power_w < d.power_w - tol_p or p.soc < d.soc - tol_s:
                return True
        elif not p.grid_charge and (p.power_w < d.power_w - tol_p or p.soc > d.soc + tol_s):
            return True
    return False


SAFETY_OFF_CAP = 24                        # wyłączeń bezpieczeństwa na dobę (poza budżetem NVM)
_DAY_S = 86400.0


def _safety_off(word, profile, gates, memory: ControlMemory, now_mono: float, notes: list[str], flat, common):
    memory.tou_safety_offs = [t for t in memory.tou_safety_offs if 0.0 <= now_mono - t < _DAY_S]
    if len(memory.tou_safety_offs) >= SAFETY_OFF_CAP:
        notes.append("tou_safety_off_cap")
        return TouDecision(IDLE, "held", flat=flat, notes=tuple(notes), **common)
    notes.append("tou_safety_off")
    reason = _gate_reason(gates, memory, now_mono)
    status = WRITE if reason is None else DRY_RUN
    return TouDecision(status, reason or "tou_safety_off", writes=[encode_tou_enable(False, word, profile)],
                       flat=flat, notes=tuple(notes), **common)


def commit_tou(decision: TouDecision, report: TouReport, memory: ControlMemory, now_mono: float, *,
               now_wall: float | None = None) -> None:
    """Pamięć po sekwencji: throttling i ostatni zapis tylko dla zapisanych pól (wartość rzeczywista
    przy OK_ADJUSTED — `adjusted`, żeby nie przepisywać co cykl), prawdziwa odmowa → `denied`
    (odwrót), niepewność dla ERROR, włącznik jako `tou_enabled`, `tou` nieobsługiwane do końca
    sesji po wyjątku 2.

    `now_wall` — liczenie ramek w budżecie NVM tutaj (pisarz bez własnego licznika); przy
    pisarzu rejestrów ramki liczy jego `on_send`, więc wtedy `None`.
    """
    if decision.status != WRITE:
        return
    actual = {k: v for k, v in report.actual.items() if k in decision.flat}
    flat = {**decision.flat, **actual}
    fields = [k for k in report.written if k in flat]
    memory.throttle.record(flat, fields, now_mono)
    for key in fields:
        memory.last_written[key] = flat[key]
        if key in actual:
            memory.adjusted[key] = (decision.flat[key], actual[key])
        else:
            memory.adjusted.pop(key, None)
        memory.denied.pop(key, None)
    if fields:
        memory.tou_rewrite_at = now_mono
    ambiguous = set(report.ambiguous)
    for key in set(report.failed) - ambiguous:
        if key in decision.flat and key in decision.device:
            note_denied(memory, key, decision.flat[key], decision.device[key], now_mono,
                        max_hold_s=TOU_DENIED_MAX_S)
    if report.frames:
        memory.tou_write_end = now_mono
    safety_off = "tou_safety_off" in decision.notes
    if safety_off and report.frames:
        memory.tou_safety_offs.append(now_mono)
    # Włącznik: stan końcowy po sekwencji (ON, samo OFF albo nieznany po ERROR).
    if ENABLE in report.frames:
        memory.tou_enable_at = now_mono
    if ENABLE in ambiguous:
        memory.last_written.pop(EN_KEY, None)
    elif report.enable_written or ENABLE in report.written:
        state = 1.0 if report.enable_written and not safety_off else 0.0
        memory.throttle.record({EN_KEY: state}, [EN_KEY], now_mono)
        memory.last_written[EN_KEY] = state
    unknown = [EN_KEY if k == ENABLE else k for k in report.ambiguous]
    memory.uncertain -= {EN_KEY if k == ENABLE else k for k in report.written}
    memory.uncertain |= set(unknown)
    memory.throttle.mark_unknown([k for k in unknown if k in flat or k == EN_KEY], now_mono)
    if report.unsupported:
        memory.unsupported.add("tou")
    if now_wall is not None and memory.budget is not None:
        for key in report.frames:
            memory.budget.note(key, now_wall)
