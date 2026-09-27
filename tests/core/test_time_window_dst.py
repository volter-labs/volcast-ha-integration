"""Kotwica dobowa w dniach zmiany czasu: siatka doby na zegarze ściennym.

Własność: w każdej chwili [teraz, teraz + 23 h) program aktywny według pory doby (tak wybiera go
falownik) daje to, co plan w tej chwili. Wyjątek: jesienią powtórzona godzina ma JEDEN program
na dwie godziny planu — wybierany ten bez ładowania z sieci.
"""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.volcast.core.engines import time_window as tw
from custom_components.volcast.core.engines.mode_setpoint import slot_intent
from custom_components.volcast.core.profile import load_builtin
from custom_components.volcast.core.slot import parse_schedule

DEYE = load_builtin("deye-sg")
WAW = ZoneInfo("Europe/Warsaw")
CH = {"mode": "charge", "charge_source": "grid", "power_w": 3000, "soc_target": 90, "price_pln_kwh": 0.2}
IDLE = {"mode": "idle", "price_pln_kwh": 0.9}
SELF = {"mode": "self_consume", "price_pln_kwh": 0.5}


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def plan_from(pattern, first_day, days=6):
    """Sloty godzinowe na zegarze rzeczywistym (doba zmiany czasu ma 23 albo 25 slotów)."""
    slots = []
    t = datetime.combine(first_day, datetime.min.time(), tzinfo=WAW).astimezone(timezone.utc)
    end = datetime.combine(first_day + timedelta(days=days), datetime.min.time(), tzinfo=WAW).astimezone(timezone.utc)
    while t < end:
        slots.append({"from": _iso(t), "to": _iso(t + timedelta(hours=1)), **pattern(t.astimezone(WAW))})
        t += timedelta(hours=1)
    return parse_schedule({"schedule_id": "dst", "slots": slots, "fallback": {"mode": "self_consume", "soc_reserve": 10}})


def _active(programs, tod):
    cur = programs[-1]
    for p in programs:
        if p.start_min <= tod:
            cur = p
    return cur


def _expected(plan, t):
    s = plan.slot_for(t)
    own, _ = slot_intent(s)
    run = own if DEYE.intent(own) is not None else "self_consume"
    return tw._program(run, s, DEYE, 10.0, 10000.0)


def _ambiguous(t):
    loc = t.astimezone(WAW)
    other = (t + timedelta(hours=1) if loc.fold == 0 else t - timedelta(hours=1)).astimezone(WAW)
    return other.replace(tzinfo=None, fold=0) == loc.replace(tzinfo=None, fold=0)


def _check(plan, start, hours):
    for i in range(int(hours * 12)):
        now = start + timedelta(minutes=5 * i)
        r = tw.compress(plan, now, DEYE, soc_reserve=10.0, rated_power_w=10000.0, tz=WAW, anchor="day")
        starts = [p.start_min for p in r.programs]
        assert starts == sorted(set(starts)) and starts[0] == 0, (now, starts)
        t = now
        while t < now + timedelta(hours=23):
            loc = t.astimezone(WAW)
            p = _active(r.programs, loc.hour * 60 + loc.minute)
            exp = _expected(plan, t)
            got = (p.grid_charge, p.soc, p.power_w)
            if got != (exp.grid_charge, exp.soc, exp.power_w):
                assert _ambiguous(t) and not p.grid_charge, (now.isoformat(), loc.isoformat(), got, exp)
            t += timedelta(minutes=5)


def _same(loc):
    return CH if 2 <= loc.hour < 5 else IDLE if 17 <= loc.hour < 19 else SELF


def _spring(loc):
    return CH if loc.hour == 2 else IDLE if loc.hour == 3 else SELF


def _autumn(loc):
    if loc.hour == 2:
        return CH if loc.utcoffset() == timedelta(hours=2) else IDLE
    return IDLE if 17 <= loc.hour < 19 else SELF


@pytest.mark.parametrize("pattern", [_same, _spring])
def test_spring_forward_day_grid_follows_plan(pattern):
    # 2026-03-29: 02:00 → 03:00. Przejście przez dobę przed zmianą i przez samą zmianę.
    _check(plan_from(pattern, date(2026, 3, 26)), datetime(2026, 3, 28, 0, tzinfo=timezone.utc), 36)


@pytest.mark.parametrize("pattern", [_same, _autumn])
def test_autumn_day_grid_follows_plan_and_merges_repeated_hour(pattern):
    # 2026-10-25: 03:00 → 02:00 (godzina 02:00–03:00 dwa razy).
    _check(plan_from(pattern, date(2026, 10, 22)), datetime(2026, 10, 24, 0, tzinfo=timezone.utc), 36)


def test_spring_eve_keeps_charge_hour_without_duplicate_start():
    plan = plan_from(_spring, date(2026, 3, 26))
    now = datetime(2026, 3, 28, 0, 30, tzinfo=timezone.utc)             # 01:30 w dobie przed zmianą
    r = tw.compress(plan, now, DEYE, soc_reserve=10.0, rated_power_w=10000.0, tz=WAW, anchor="day")
    by_start = {p.start_min: p for p in r.programs}
    assert by_start[120].grid_charge is True and by_start[180].grid_charge is False


def test_autumn_repeated_hour_prefers_program_without_grid_charge():
    plan = plan_from(_autumn, date(2026, 10, 22))
    now = datetime(2026, 10, 24, 10, tzinfo=timezone.utc)
    r = tw.compress(plan, now, DEYE, soc_reserve=10.0, rated_power_w=10000.0, tz=WAW, anchor="day")
    assert _active(r.programs, 150).grid_charge is False
    assert len({p.start_min for p in r.programs}) == len(r.programs)
