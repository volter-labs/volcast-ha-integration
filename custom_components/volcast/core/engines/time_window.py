"""Silnik okien czasowych: plan 24–48 h → N programów TOU na dobę.

Falownik wykonuje programy SAM, także przy awarii HA/Boxa/chmury — dlatego
program pokrywa całą dobę, a każde uproszczenie planu raportujemy w złotówkach
(`lost_value_pln`), żeby było widać w aplikacji, ile kosztuje ograniczenie sprzętu.

Model wartości (świadome uproszczenie):
- `E_own` — energia, którą plan chciał przesunąć w slocie (kWh, + ładowanie, − rozładowanie);
- `E_exec` — co zrobi program okna, do którego slot trafił;
- strata slotu = |E_own − E_exec| × |cena − cena średnia| (średnia ważona czasem).
Brak ceny = strata 0 i licznik `unpriced_slots`, nigdy wyjątek.

Znane ograniczenia:
- scalenie `standby` z samokonsumpcją bez mocy w slocie wycenia się na 0
  (nie znamy naturalnego rozładowania) — raport kompresji liczy je osobno;
- „program 1" to okno obejmujące „teraz" (`windows[0]`), a `programs` są
  posortowane wg godziny doby, bo tak je przyjmuje falownik; z kotwicą dobową
  (`anchor="day"`) horyzont to stała siatka doby lokalnej [00:00, 24:00) — pora doby bierze
  slot z najbliższego wystąpienia od początku bieżącego slotu (późniejsze pory — dziś,
  wcześniejsze — jutro), więc `windows[0]` zaczyna się o północy, a starty programów
  zmieniają się tylko ze zmianą planu, nie z upływem „teraz"; siatka leży na zegarze
  ściennym, więc zmiana czasu nie daje zdublowanych startów (jesienią powtórzona godzina ma
  jeden program — ten bez ładowania z sieci, gdy dwie godziny planu się różnią);
- z kotwicą „teraz" w dniu zmiany czasu jesienią dwa okna mogą zacząć się o tej samej
  godzinie lokalnej — guard I-10 odrzuca wtedy całą komendę (fail-closed).
"""
from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta, timezone, tzinfo
from typing import Callable

from ..params import Params, TouProgram
from ..profile import Profile
from ..slot import Schedule, Slot
from .mode_setpoint import slot_intent

_NATURAL = ("self_consume", "charge_pv")
_TIE_DIGITS = 9     # remisy przyrostów liczone po zaokrągleniu — szum float nie zmienia wyboru


@dataclass(frozen=True)
class ProgramSpec:
    grid_charge: bool
    soc: float
    power_w: float


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    intent: str
    program: ProgramSpec


@dataclass(frozen=True)
class Merge:
    kept_intent: str
    absorbed_intent: str
    start: datetime
    end: datetime
    lost_value_pln: float


@dataclass(frozen=True)
class CompressionResult:
    programs: tuple[TouProgram, ...]      # dokładnie N, rosnąco wg czasu lokalnego
    windows: tuple[Window, ...]           # w kolejności horyzontu; windows[0] obejmuje „teraz"
    lost_value_pln: float
    merge_loss_pln: float
    degrade_loss_pln: float
    merges: tuple[Merge, ...]
    degraded: dict[str, int]
    unpriced_slots: int
    fallback_slots: int

    def params(self) -> Params:
        return Params(tou=self.programs)


@dataclass
class _H:
    slot: Slot
    own: str            # intencja z planu
    run: str            # intencja wykonywana (po degradacji)
    prog: ProgramSpec
    fallback: bool = False


@dataclass
class _W:
    start: datetime
    end: datetime
    intent: str
    prog: ProgramSpec
    members: list[_H] = field(default_factory=list)


def _local_min(dt: datetime, tz: tzinfo) -> int:
    loc = dt.astimezone(tz)
    return loc.hour * 60 + loc.minute


def _soc_floor(soc: float, reserve: float) -> float:
    # Program jest podłogą, którą falownik utrzymuje sam przez całą dobę:
    # nigdy poniżej rezerwy (to samo robi guard I-1) i nigdy ponad 100 %.
    return min(max(soc, reserve), 100.0)


def _program(intent: str, slot: Slot, profile: Profile, reserve: float, rated: float) -> ProgramSpec:
    spec = profile.intent(intent)
    target = slot.soc_target if slot.soc_target is not None else 100.0
    soc = {"target_or_max": target, "reserve": reserve, "hold": target}[spec["soc"]]
    if spec["power"] == "max" or slot.power_w is None:
        power = rated
    else:
        power = min(max(slot.power_w, 0.0), rated)
    return ProgramSpec(bool(spec["grid_charge"]), float(_soc_floor(soc, reserve)), float(power))


def _own_energy(h: _H) -> float:
    e = (h.slot.power_w or 0.0) * h.slot.hours / 1000.0
    if h.own in ("charge_grid", "charge_pv"):
        return e
    if h.own in ("sell", "discharge_forced"):
        return -e
    if h.own == "self_consume" and h.slot.discharge_purpose == "self":
        return -e
    return 0.0


def _exec_energy(h: _H, intent: str, prog: ProgramSpec) -> float:
    natural = _own_energy(h) if h.own in _NATURAL else 0.0
    if intent == "charge_grid":
        return prog.power_w * h.slot.hours / 1000.0
    if intent in ("sell", "discharge_forced"):
        return -prog.power_w * h.slot.hours / 1000.0
    if intent == "standby":
        return max(natural, 0.0)        # program „stój" blokuje rozładowanie, PV dalej ładuje
    return natural


def _loss(h: _H, intent: str, prog: ProgramSpec, p_mean: float | None) -> float:
    if p_mean is None or h.slot.price_pln_kwh is None:
        return 0.0
    return abs(_own_energy(h) - _exec_energy(h, intent, prog)) * abs(h.slot.price_pln_kwh - p_mean)


def _cost(w: _W, p_mean: float | None) -> float:
    return sum(_loss(h, w.intent, w.prog, p_mean) for h in w.members)


def _horizon(schedule: Schedule, now: datetime, tz: tzinfo, step: int) -> tuple[list[Slot], list[bool]]:
    # Krok liczony w czasie LOKALNYM — strefy z przesunięciem półgodzinnym też trafiają w siatkę.
    loc = now.astimezone(tz)
    now_step = loc.replace(minute=loc.minute - loc.minute % step, second=0,
                           microsecond=0).astimezone(timezone.utc)
    covering = schedule.slot_for(now)
    t0 = covering.start if covering is not None else now_step
    if (t0.astimezone(tz) + timedelta(days=1)).astimezone(timezone.utc) <= now:
        # Slot dłuższy niż doba zaczął się ponad dobę temu — okno 1 i tak musi objąć „teraz".
        t0 = now_step
    # Ta sama godzina lokalna następnego dnia (w dniu zmiany czasu 23 albo 25 h).
    end = (t0.astimezone(tz) + timedelta(days=1)).astimezone(timezone.utc)
    slots: list[Slot] = []
    flags: list[bool] = []
    cursor = t0
    for s in schedule.slots:
        if s.end <= cursor or s.start >= end:
            continue
        if s.start > cursor:
            slots.append(schedule.fallback.as_slot(cursor, s.start))
            flags.append(True)
            cursor = s.start
        piece_end = min(s.end, end)
        slots.append(replace(s, start=cursor, end=piece_end))
        flags.append(False)
        cursor = piece_end
        if cursor >= end:
            break
    if cursor < end:
        slots.append(schedule.fallback.as_slot(cursor, end))
        flags.append(True)
    for s in slots:
        for edge in (s.start, s.end):
            if _local_min(edge, tz) % step or edge.second or edge.microsecond:
                raise ValueError(f"granica slotu {edge.isoformat()} poza krokiem {step} min")
    return slots, flags


def _day_grid(schedule: Schedule, now: datetime, tz: tzinfo, step: int,
              grid_charge: Callable[[Slot], bool]) -> tuple[list[Slot], list[bool], tzinfo]:
    """Horyzont [t0, t0+doba) jako siatka doby na ZEGARZE ŚCIENNYM, od północy.

    Pora doby `m` (co krok) bierze slot planu z najbliższego wystąpienia tej pory od początku
    bieżącego slotu `t0`: pory późniejsze niż t0 — dziś, wcześniejsze — jutro. Pora jest
    rozwiązywana w SWOJEJ dobie (nie przesuwana o dobę), więc zmiana czasu jutro nie zniekształca
    dzisiejszych godzin i odwrotnie. W dobie zmiany czasu:
    * pora nieistniejąca (wiosną 02:00–03:00) — falownik jej nie przeżyje; dostaje slot sąsiedniej
      pory (ciągłość okna), bez okna zerowej długości;
    * pora powtórzona (jesienią 02:00–03:00 dwa razy) — JEDEN program na obie godziny planu;
      gdy się różnią, wygrywa ten bez ładowania z sieci. Starty programów są więc zawsze różne —
      zmiana czasu nigdy nie kończy się odrzuceniem I-10 ani zamrożeniem programów.

    Sloty siatki leżą na osi o stałym przesunięciu (`gtz` = przesunięcie strefy o północy doby
    siatki): minuta od północy tej osi = pora doby programu.
    """
    slots, flags = _horizon(schedule, now, tz, step)       # walidacja kroku i granice horyzontu
    t0, end = slots[0].start, slots[-1].end
    starts = [sl.start for sl in slots]
    loc0 = t0.astimezone(tz)
    m0 = loc0.hour * 60 + loc0.minute
    d0 = loc0.date()
    grid_date = d0 if m0 == 0 else d0 + timedelta(days=1)
    gtz = timezone(datetime.combine(grid_date, time(0), tzinfo=tz).utcoffset())
    base = datetime.combine(grid_date, time(0), tzinfo=gtz)

    def at(inst: datetime) -> int:
        return bisect_right(starts, inst) - 1

    chosen: list[int | None] = []
    for m in range(0, 1440, step):
        day = d0 if m >= m0 else d0 + timedelta(days=1)
        wall = datetime.combine(day, time(m // 60, m % 60))
        cands: list[int] = []
        for fold in (0, 1):
            inst = wall.replace(tzinfo=tz, fold=fold).astimezone(timezone.utc)
            if inst.astimezone(tz).replace(tzinfo=None) != wall or not t0 <= inst < end:
                continue                                    # pora nieistniejąca albo poza horyzontem
            i = at(inst)
            if i not in cands:
                cands.append(i)
        if len(cands) > 1:
            safe = [i for i in cands if not grid_charge(slots[i])]
            cands = safe or cands
        chosen.append(cands[0] if cands else None)
    for k in range(len(chosen)):                            # pora nieistniejąca → sąsiednia
        if chosen[k] is None:
            chosen[k] = chosen[k - 1] if k > 0 and chosen[k - 1] is not None else next(
                (c for c in chosen[k:] if c is not None), 0)

    out: list[Slot] = []
    out_flags: list[bool] = []
    k = 0
    while k < len(chosen):
        j = k
        while j + 1 < len(chosen) and chosen[j + 1] == chosen[k]:
            j += 1
        src = slots[chosen[k]]
        out.append(replace(src, start=base + timedelta(minutes=k * step),
                           end=base + timedelta(minutes=(j + 1) * step)))
        out_flags.append(flags[chosen[k]])
        k = j + 1
    return out, out_flags, gtz


def _within(a: ProgramSpec, b: ProgramSpec, profile: Profile) -> bool:
    return (a.grid_charge == b.grid_charge and abs(a.soc - b.soc) <= profile.soc_tolerance_pp
            and abs(a.power_w - b.power_w) <= profile.power_tolerance_w)


def _check_inputs(now: datetime, soc_reserve: float, rated_power_w: float) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("`now` musi mieć strefę czasową")
    if not math.isfinite(soc_reserve) or not 0.0 <= soc_reserve <= 100.0:
        raise ValueError(f"rezerwa SoC {soc_reserve!r} poza 0..100")
    if not math.isfinite(rated_power_w) or rated_power_w <= 0.0:
        # Programy „moc maksymalna" potrzebują liczby — nieznana moc to błąd, nie zero.
        raise ValueError(f"moc znamionowa {rated_power_w!r} nieznana albo niedodatnia")


def _split(w: _W, mid: datetime) -> tuple[_W, _W]:
    left: list[_H] = []
    right: list[_H] = []
    for h in w.members:
        if h.slot.end <= mid:
            left.append(h)
        elif h.slot.start >= mid:
            right.append(h)
        else:
            # Slot przecięty granicą — dzielimy go, żeby energia i strata się nie zdublowały.
            left.append(replace(h, slot=replace(h.slot, end=mid)))
            right.append(replace(h, slot=replace(h.slot, start=mid)))
    return _W(w.start, mid, w.intent, w.prog, left), _W(mid, w.end, w.intent, w.prog, right)


def compress(schedule: Schedule, now: datetime, profile: Profile, *, soc_reserve: float,
             rated_power_w: float, tz: tzinfo, anchor: str = "now") -> CompressionResult:
    if profile.control_model != "time_window":
        raise ValueError(f"profil {profile.id} nie jest modelu time_window")
    if anchor not in ("now", "day"):
        raise ValueError(f"nieznana kotwica {anchor!r}")
    _check_inputs(now, soc_reserve, rated_power_w)
    step, n = profile.time_step_min, profile.tou_programs
    ptz = tz                                   # strefa, w której liczona jest pora doby programu
    if anchor == "day":
        def grid_charge(s: Slot) -> bool:
            own, _ = slot_intent(s)
            run = own if profile.intent(own) is not None else "self_consume"
            return _program(run, s, profile, soc_reserve, rated_power_w).grid_charge
        slots, flags, ptz = _day_grid(schedule, now, tz, step, grid_charge)
    else:
        slots, flags = _horizon(schedule, now, tz, step)

    hs: list[_H] = []
    degraded: dict[str, int] = {}
    for s, fb in zip(slots, flags):
        own, _note = slot_intent(s)
        run = own
        if profile.intent(own) is None:
            run = "self_consume"          # profil tej intencji nie obsługuje
            degraded[own] = degraded.get(own, 0) + 1
        hs.append(_H(s, own, run, _program(run, s, profile, soc_reserve, rated_power_w), fb))

    priced = [h for h in hs if h.slot.price_pln_kwh is not None]
    hours = sum(h.slot.hours for h in priced)
    p_mean = (sum(h.slot.price_pln_kwh * h.slot.hours for h in priced) / hours) if hours else None

    # 1) sąsiednie sloty tej samej intencji i programu w tolerancji → jedno okno
    wins: list[_W] = []
    for h in hs:
        last = wins[-1] if wins else None
        if last is not None and last.intent == h.run and _within(last.prog, h.prog, profile):
            last.prog = ProgramSpec(last.prog.grid_charge, max(last.prog.soc, h.prog.soc),
                                    max(last.prog.power_w, h.prog.power_w))
            last.end = h.slot.end
            last.members.append(h)
        else:
            wins.append(_W(h.slot.start, h.slot.end, h.run, h.prog, [h]))

    degrade_loss = sum(_loss(h, h.run, h.prog, p_mean) for h in hs if h.own != h.run)
    start_total = sum(_cost(w, p_mean) for w in wins)

    # 2) zachłanne scalanie par o najmniejszym przyroście straty
    #    (remis → wcześniejsza para, program lewego)
    merges: list[Merge] = []
    while len(wins) > n:
        best: tuple[float, int, int, float] | None = None
        for i in range(len(wins) - 1):
            a, b = wins[i], wins[i + 1]
            base = _cost(a, p_mean) + _cost(b, p_mean)
            for k, keep in enumerate((a, b)):
                new = sum(_loss(h, keep.intent, keep.prog, p_mean) for h in a.members + b.members)
                inc = new - base
                cand = (round(inc, _TIE_DIGITS), i, k, inc)
                if best is None or cand[:3] < best[:3]:
                    best = cand
        _key, i, k, inc = best
        a, b = wins[i], wins[i + 1]
        keep, lost = (a, b) if k == 0 else (b, a)
        merges.append(Merge(keep.intent, lost.intent, a.start, b.end, round(inc, 6)))
        wins[i:i + 2] = [_W(a.start, b.end, keep.intent, keep.prog, a.members + b.members)]

    # 3) mniej okien niż N → dzielimy najdłuższe (ten sam program, zero straty).
    #    Doba ma >= 23 kroki, a N <= 12, więc zawsze jest co dzielić.
    step_td = timedelta(minutes=step)
    while len(wins) < n:
        idx = max(range(len(wins)), key=lambda j: (wins[j].end - wins[j].start, -j))
        w = wins[idx]
        steps = int((w.end - w.start) / step_td)
        if steps < 2:
            raise ValueError("horyzont za krótki, żeby wypełnić wszystkie programy")
        wins[idx:idx + 1] = list(_split(w, w.start + step_td * (steps // 2)))

    total = sum(_cost(w, p_mean) for w in wins)
    programs = tuple(sorted(
        (TouProgram(_local_min(w.start, ptz), w.prog.power_w, w.prog.soc, w.prog.grid_charge) for w in wins),
        key=lambda p: p.start_min))
    return CompressionResult(
        programs=programs,
        windows=tuple(Window(w.start, w.end, w.intent, w.prog) for w in wins),
        lost_value_pln=round(float(total), 4),
        merge_loss_pln=round(float(total - start_total), 4),
        degrade_loss_pln=round(float(degrade_loss), 4),
        merges=tuple(merges),
        degraded=degraded,
        unpriced_slots=sum(1 for h in hs if not h.fallback and h.slot.price_pln_kwh is None),
        fallback_slots=sum(1 for h in hs if h.fallback),
    )


def baseline_programs(profile: Profile, soc_reserve: float, rated_power_w: float) -> tuple[TouProgram, ...]:
    """Tryb bazowy po cofnięciu zgody: N programów samokonsumpcji równo w dobie."""
    n, step = profile.tou_programs, profile.time_step_min
    spec = profile.intent("self_consume")
    soc = soc_reserve if spec["soc"] == "reserve" else 100.0
    every = (1440 // n) // step * step
    return tuple(TouProgram(i * every, float(rated_power_w), float(_soc_floor(soc, soc_reserve)),
                            bool(spec["grid_charge"]))
                 for i in range(n))


def program_diff(new: tuple[TouProgram, ...], current: tuple[TouProgram, ...] | None) -> set[str]:
    """Klucze pól do przepisania (pamięć nieulotna: tylko to, co się zmieniło)."""
    new_flat = Params(tou=new).flatten()
    if current is None:
        return set(new_flat)
    cur_flat = Params(tou=current).flatten()
    return {k for k, v in new_flat.items() if cur_flat.get(k) != v}
