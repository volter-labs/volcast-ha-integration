"""Cykl okien czasowych (programy TOU, Deye) w trybie bezpośrednim.

plan → `compress(anchor="day")` (stała siatka doby: starty zmieniają się tylko ze zmianą planu)
→ strażnicy (I-9 świeżość, I-10 kształt programów — m.in. zdublowane starty w dniu zmiany
czasu: fail-closed, programy w falowniku jadą dalej) → porównanie z odczytem (tylko zmienione
pola) → interwał I-6 i budżet NVM (pola wstrzymane idą do sekwencji jako `pre_held`) →
sekwencja: włącznik OFF (gdy ON) → programy → włącznik ON na końcu.

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
from .cycle import BLOCKED, DRY_RUN, ERROR, IDLE, WRITE, ControlMemory, _gate_reason, same_value
from .tou_writes import ENABLE, TouReport


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
    if "tou" in memory.unsupported:
        return TouDecision(BLOCKED, "tou_unsupported")
    if schedule is None:
        return TouDecision(IDLE, "no_plan")
    if reading.programs is None or reading.tou_enabled is None:
        return TouDecision(BLOCKED, "tou_unreadable")
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
    common.update(programs=programs, guard=guard)
    flat = Params(tou=programs).flatten()
    device = {k: reading.device[k] for k in flat if k in reading.device}
    memory.throttle.reconcile(device)
    memory.uncertain -= {k for k in memory.uncertain if k in reading.device}
    settled = {k for k in flat if k in device and same_value(device[k], flat[k])}
    changed = set(flat) - settled
    due = memory.throttle.filter(flat, now_mono) - settled
    held = changed - due
    if held:
        notes.append("I-6")
    enable_off = not reading.tou_enabled
    if memory.budget is not None:
        blocked = memory.budget.exhausted(changed | ({ENABLE} if changed or enable_off else set()),
                                          now_utc.timestamp())
        if blocked:
            held |= blocked
            notes.append("nvm_budget")
    if not changed and not enable_off:
        return TouDecision(IDLE, "nothing_to_write", flat=flat, notes=tuple(notes), **common)
    if changed and not (changed - held):
        # Nic z programów nie może iść w tym cyklu — nie wyłączamy harmonogramu na darmo.
        return TouDecision(IDLE, "held", flat=flat, notes=tuple(notes), pre_held=tuple(sorted(held)), **common)
    writes: list[RegisterWrite] = []
    after = word
    program_writes = encode_writes(Params(tou=programs), profile, keys=changed, current=reading.image)
    if program_writes and reading.tou_enabled:
        off = encode_tou_enable(False, word, profile, owner_word)
        writes.append(off)
        after = off.value
    writes.extend(program_writes)
    writes.append(encode_tou_enable(True, after, profile, owner_word))
    reason = _gate_reason(gates, memory, now_mono)
    status = WRITE if reason is None else DRY_RUN
    return TouDecision(status, reason or "ok", writes=writes, flat=flat, notes=tuple(notes),
                       pre_held=tuple(k for k in (w.key for w in writes) if k in held), **common)


def commit_tou(decision: TouDecision, report: TouReport, memory: ControlMemory, now_mono: float, *,
               now_wall: float | None = None) -> None:
    """Pamięć po sekwencji: throttling tylko dla zapisanych pól (wartość rzeczywista przy
    OK_ADJUSTED), niepewność dla ERROR, `tou` nieobsługiwane do końca sesji po wyjątku 2.

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
    memory.uncertain -= set(report.written)
    memory.uncertain |= set(report.ambiguous)
    memory.throttle.mark_unknown([k for k in report.ambiguous if k in flat], now_mono)
    if report.unsupported:
        memory.unsupported.add("tou")
    if now_wall is not None and memory.budget is not None:
        for key in report.frames:
            memory.budget.note(key, now_wall)
