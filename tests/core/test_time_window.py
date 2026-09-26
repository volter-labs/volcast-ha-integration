import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.volcast.core.engines.time_window import (
    baseline_programs, compress, program_diff,
)
from custom_components.volcast.core.guards import STATUS_OK, GuardContext, apply_guards
from custom_components.volcast.core.params import Params, TouProgram
from custom_components.volcast.core.profile import profile_from_dict
from custom_components.volcast.core.slot import Action, parse_schedule
from tests.core.profile_fixtures import ms_profile, tw_profile

WAW = ZoneInfo("Europe/Warsaw")
TW = profile_from_dict(tw_profile())
# 2026-09-02 00:00 czasu warszawskiego = 2026-09-01 22:00 UTC
DAY0 = datetime(2026, 9, 1, 22, tzinfo=timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _plan(hours, fallback_reserve=15):
    """hours: lista (godzina_lokalna_0..23, dict pól slotu)."""
    slots = [{"from": _iso(DAY0 + timedelta(hours=h)), "to": _iso(DAY0 + timedelta(hours=h + 1)), **f}
             for h, f in hours]
    return parse_schedule({"schedule_id": "t", "slots": slots,
                           "fallback": {"mode": "self_consume", "soc_reserve": fallback_reserve}})


SELF = {"mode": "self_consume", "price_pln_kwh": 0.5}


def _run(sch, now=DAY0, profile=TW):
    return compress(sch, now, profile, soc_reserve=10.0, rated_power_w=5000.0, tz=WAW)


def test_flat_day_pads_to_n_programs_without_loss():
    r = _run(_plan([(h, SELF) for h in range(24)]))
    assert len(r.programs) == 6 and r.lost_value_pln == 0.0 and not r.merges
    assert sorted(p.start_min for p in r.programs) == [p.start_min for p in r.programs]
    assert all((p.grid_charge, p.soc, p.power_w) == (False, 10.0, 5000.0) for p in r.programs)
    assert r.programs[0].start_min == 0


def test_cheapest_merge_first():
    # Średnia 10.99/24 ≈ 0.458. Przyrosty pierwszej iteracji (ładowanie 1 kWh/h):
    # porzucenie ładowania o 7 (0.49) = 0.032; o 13 (0.40) = 0.058; o 4 (0.30) = 0.158;
    # o 1 (0.20) = 0.258; o 10 (0.10) = 0.358; wymuszenie ładowania w godzinach
    # samokonsumpcji (0.5) kosztuje 0.042 za godzinę — więc najpierw znika okno o 7.
    prices = {1: 0.20, 4: 0.30, 7: 0.49, 10: 0.10, 13: 0.40}
    hours = [(h, {"mode": "charge", "charge_source": "grid", "power_w": 1000, "soc_target": 90,
                  "price_pln_kwh": prices[h]} if h in prices else SELF) for h in range(24)]
    r = _run(_plan(hours))
    assert len(r.programs) == 6
    first = r.merges[0]
    assert (first.kept_intent, first.absorbed_intent) == ("self_consume", "charge_grid")
    assert first.lost_value_pln == pytest.approx(abs(0.49 - 10.99 / 24), abs=1e-6)
    charge_starts = {p.start_min for p in r.programs if p.grid_charge}
    assert 7 * 60 not in charge_starts                      # najtańsze do porzucenia okno zniknęło
    assert r.lost_value_pln == pytest.approx(sum(m.lost_value_pln for m in r.merges), abs=1e-4)


def test_unsupported_sell_is_degraded_and_costed():
    hours = [(h, SELF) for h in range(24)]
    hours[19] = (19, {"mode": "discharge", "discharge_purpose": "sell", "power_w": 3000, "price_pln_kwh": 1.5})
    r = _run(_plan(hours))
    assert r.degraded == {"sell": 1}
    assert r.degrade_loss_pln == pytest.approx(3.0 * abs(1.5 - (23 * 0.5 + 1.5) / 24), abs=1e-6)
    assert not any(p.grid_charge for p in r.programs)


def test_short_plan_fills_with_fallback_and_counts_unpriced():
    r = _run(_plan([(h, {"mode": "self_consume"}) for h in range(6)]))
    assert r.fallback_slots == 1 and r.unpriced_slots == 6 and r.lost_value_pln == 0.0
    assert len(r.programs) == 6


def test_program_one_starts_at_current_slot():
    r = _run(_plan([(h, SELF) for h in range(24)]), now=DAY0 + timedelta(hours=14, minutes=23))
    assert r.windows[0].start == DAY0 + timedelta(hours=14)
    assert (r.windows[-1].end - r.windows[0].start) == timedelta(hours=24)
    assert 14 * 60 in {p.start_min for p in r.programs}


def test_misaligned_slot_boundary_is_error():
    sch = parse_schedule({"schedule_id": "t", "slots": [
        {"from": _iso(DAY0 + timedelta(minutes=3)), "to": _iso(DAY0 + timedelta(hours=1)), **SELF}]})
    with pytest.raises(ValueError):
        _run(sch, now=DAY0 + timedelta(minutes=10))


def test_baseline_and_diff():
    base = baseline_programs(TW, 10.0, 5000.0)
    assert [p.start_min for p in base] == [0, 240, 480, 720, 960, 1200]
    moved = base[:5] + (TouProgram(1200, 5000.0, 30.0, False),)
    assert program_diff(moved, base) == {"tou.6.soc"}
    assert program_diff(base, None) == {f"tou.{i}.{f}" for i in range(1, 7)
                                        for f in ("start", "power_w", "soc", "grid_charge")}


# ── przypadki brzegowe: nigdy wyjątek, zawsze N okien na pełną dobę ──

def _check_shape(r, sch, now, profile=TW):
    n, step = profile.tou_programs, profile.time_step_min
    assert len(r.programs) == n and len(r.windows) == n
    starts = [p.start_min for p in r.programs]
    assert starts == sorted(set(starts))
    assert all(0 <= s < 1440 and s % step == 0 for s in starts)
    w0 = r.windows[0]
    assert w0.start <= now < w0.end
    for a, b in zip(r.windows, r.windows[1:]):
        assert a.end == b.start and a.start < a.end
    assert r.windows[-1].end.astimezone(WAW) == w0.start.astimezone(WAW) + timedelta(days=1)
    assert r.params() == Params(tou=r.programs)
    assert r.lost_value_pln >= 0.0 and r.degrade_loss_pln >= 0.0
    assert r.unpriced_slots >= 0 and r.fallback_slots >= 0


def test_empty_plan_is_all_fallback():
    sch = parse_schedule({"schedule_id": "t", "slots": [],
                          "fallback": {"mode": "self_consume", "soc_reserve": 15}})
    now = DAY0 + timedelta(hours=9, minutes=7)
    r = _run(sch, now=now)
    _check_shape(r, sch, now)
    assert r.fallback_slots == 1 and r.unpriced_slots == 0 and r.lost_value_pln == 0.0
    # bez slotu „teraz" program 1 startuje od kroku profilu obejmującego „teraz"
    assert r.windows[0].start == DAY0 + timedelta(hours=9, minutes=5)


def test_gaps_are_filled_with_fallback():
    hours = [(h, SELF) for h in (0, 1, 5, 6, 12)]
    r = _run(_plan(hours))
    _check_shape(r, None, DAY0)
    assert r.fallback_slots == 3


def test_all_prices_missing_costs_nothing():
    hours = [(h, {"mode": "charge", "charge_source": "grid", "power_w": 2000}) if h % 2 else
             (h, {"mode": "self_consume"}) for h in range(24)]
    r = _run(_plan(hours))
    _check_shape(r, None, DAY0)
    assert r.lost_value_pln == 0.0 and r.unpriced_slots == 24
    assert r.merges and all(m.lost_value_pln == 0.0 for m in r.merges)


def test_partially_missing_price_counts_unpriced_and_zero_loss_for_slot():
    hours = [(h, SELF) for h in range(24)]
    hours[3] = (3, {"mode": "charge", "charge_source": "grid", "power_w": 2000})
    r = _run(_plan(hours))
    _check_shape(r, None, DAY0)
    assert r.unpriced_slots == 1


def test_identical_charge_slots_form_one_window_with_max_program():
    hours = [(h, SELF) for h in range(24)]
    for h, (p, t) in zip((2, 3, 4), ((1000, 80), (1300, 83), (1200, 81))):
        hours[h] = (h, {"mode": "charge", "charge_source": "grid", "power_w": p, "soc_target": t,
                        "price_pln_kwh": 0.5})
    r = _run(_plan(hours))
    charge = [w for w in r.windows if w.intent == "charge_grid"]
    assert len(charge) == 1
    assert (charge[0].start, charge[0].end) == (DAY0 + timedelta(hours=2), DAY0 + timedelta(hours=5))
    assert (charge[0].program.soc, charge[0].program.power_w) == (83.0, 1300.0)


def test_quarter_hour_slots_and_48h_plan_are_cut_to_one_day():
    rng = random.Random(7)
    slots = []
    for q in range(48 * 4):
        s = DAY0 + timedelta(minutes=15 * q)
        slots.append({"from": _iso(s), "to": _iso(s + timedelta(minutes=15)),
                      "mode": rng.choice(["self_consume", "charge"]),
                      "price_pln_kwh": round(rng.uniform(-0.2, 1.2), 3)})
        if slots[-1]["mode"] == "charge":
            slots[-1].update(charge_source="grid", power_w=rng.choice([500, 2500, 6000]))
    sch = parse_schedule({"schedule_id": "t", "slots": slots})
    now = DAY0 + timedelta(hours=30, minutes=40)
    r = _run(sch, now=now)
    _check_shape(r, sch, now)
    assert r.windows[0].start == DAY0 + timedelta(hours=30, minutes=30)
    # plan kończy się po 48 h, horyzont po 54.5 h — koniec doby dopełnia fallback
    assert r.fallback_slots == 1
    assert r.windows[-1].end == DAY0 + timedelta(hours=54, minutes=30)


def test_long_slot_is_split_in_its_members():
    # jeden slot przez całą dobę: podział okien nie może zgubić ani zdublować energii
    ch = {"mode": "charge", "charge_source": "grid", "power_w": 2000, "price_pln_kwh": 0.3}
    sch = parse_schedule({"schedule_id": "t", "slots": [
        {"from": _iso(DAY0), "to": _iso(DAY0 + timedelta(days=1)), **ch}]})
    r = _run(sch)
    _check_shape(r, sch, DAY0)
    # dzielenie najdłuższego w połowie, remis → wcześniejsze: 24 → 12+12 → … → 3,3,3,3,6,6
    assert [p.start_min for p in r.programs] == [0, 180, 360, 540, 720, 1080]
    assert all(p.grid_charge and p.power_w == 2000.0 for p in r.programs)
    assert r.lost_value_pln == 0.0 and r.unpriced_slots == 0
    assert all(isinstance(x, float) for x in (r.lost_value_pln, r.merge_loss_pln, r.degrade_loss_pln))


def test_program_floor_never_below_reserve():
    hours = [(h, SELF) for h in range(24)]
    hours[2] = (2, {"mode": "charge", "charge_source": "grid", "power_w": 1000, "soc_target": 3,
                    "price_pln_kwh": 0.1})
    hours[5] = (5, {"mode": "idle", "soc_target": 4, "price_pln_kwh": 0.5})
    r = _run(_plan(hours))
    assert all(p.soc >= 10.0 for p in r.programs)


def test_program_clipped_to_rated_and_percent_range():
    hours = [(h, SELF) for h in range(24)]
    hours[2] = (2, {"mode": "charge", "charge_source": "grid", "power_w": 99000, "soc_target": 140,
                    "price_pln_kwh": 0.1})
    r = _run(_plan(hours))
    ch = [p for p in r.programs if p.grid_charge]
    assert ch and all(p.power_w == 5000.0 and p.soc == 100.0 for p in ch)


def test_merge_ties_pick_earlier_pair_and_left_program():
    # bez cen każde scalenie kosztuje 0 → zawsze pierwsza para i program lewego okna
    prof = tw_profile()
    prof["tou"]["programs"] = 4
    prof["write"]["tou_program"]["count"] = 4
    prof["capabilities"]["time_windows"] = 4
    p4 = profile_from_dict(prof)
    hours = [(h, {"mode": "charge", "charge_source": "grid", "power_w": 2000}) if h % 2 else
             (h, {"mode": "self_consume"}) for h in range(24)]
    r1 = _run(_plan(hours), profile=p4)
    r2 = _run(_plan(hours), profile=p4)
    assert r1 == r2                                           # deterministycznie
    _check_shape(r1, None, DAY0, profile=p4)
    first = r1.merges[0]
    assert (first.start, first.end) == (DAY0, DAY0 + timedelta(hours=2))
    assert (first.kept_intent, first.absorbed_intent) == ("self_consume", "charge_grid")
    assert [w.intent for w in r1.windows] == ["self_consume", "charge_grid", "self_consume", "charge_grid"]
    assert r1.windows[0].end == DAY0 + timedelta(hours=21)


def test_priced_merge_keeps_cheaper_program():
    # ładowanie o 4 (0.2) obok 4 h samokonsumpcji (0.5): taniej narzucić ładowanie
    # sąsiadowi (4 × 0.025) niż je porzucić (0.275)
    prof = tw_profile()
    prof["tou"]["programs"] = 4
    prof["write"]["tou_program"]["count"] = 4
    prof["capabilities"]["time_windows"] = 4
    p4 = profile_from_dict(prof)
    ch = {"mode": "charge", "charge_source": "grid", "power_w": 1000, "price_pln_kwh": 0.2}
    hours = [(h, ch if h in (4, 16) else SELF) for h in range(24)]
    r = _run(_plan(hours), profile=p4)
    first = r.merges[0]
    assert (first.start, first.end, first.kept_intent) == (DAY0, DAY0 + timedelta(hours=5), "charge_grid")
    mean = (22 * 0.5 + 2 * 0.2) / 24
    assert first.lost_value_pln == pytest.approx(4 * abs(0.5 - mean), abs=1e-6)


def test_rejects_mode_setpoint_profile_and_naive_now():
    sch = _plan([(h, SELF) for h in range(24)])
    with pytest.raises(ValueError):
        _run(sch, profile=profile_from_dict(ms_profile()))
    with pytest.raises(ValueError):
        _run(sch, now=datetime(2026, 9, 2, 0, 0))


@pytest.mark.parametrize("rated", [0.0, -1.0, float("nan"), float("inf")])
def test_rejects_unusable_rated_power(rated):
    with pytest.raises(ValueError):
        compress(_plan([(0, SELF)]), DAY0, TW, soc_reserve=10.0, rated_power_w=rated, tz=WAW)


@pytest.mark.parametrize("reserve", [-1.0, 101.0, float("nan")])
def test_rejects_unusable_reserve(reserve):
    with pytest.raises(ValueError):
        compress(_plan([(0, SELF)]), DAY0, TW, soc_reserve=reserve, rated_power_w=5000.0, tz=WAW)


@pytest.mark.parametrize("day", [datetime(2026, 3, 29, tzinfo=WAW), datetime(2026, 10, 25, tzinfo=WAW)])
def test_dst_days_keep_full_local_day(day):
    t0 = day.astimezone(timezone.utc)
    end = (day + timedelta(days=1)).astimezone(timezone.utc)
    slots, t = [], t0
    while t < end:
        slots.append({"from": _iso(t), "to": _iso(t + timedelta(hours=1)), **SELF})
        t += timedelta(hours=1)
    sch = parse_schedule({"schedule_id": "t", "slots": slots})
    r = _run(sch, now=t0)
    _check_shape(r, sch, t0)


def test_baseline_respects_reserve_and_uneven_n():
    prof = tw_profile()
    prof["tou"]["programs"] = 7
    prof["tou"]["time_step_min"] = 60
    prof["write"]["tou_program"]["count"] = 7
    prof["capabilities"]["time_windows"] = 7
    p7 = profile_from_dict(prof)
    base = baseline_programs(p7, 12.0, 4000.0)
    starts = [p.start_min for p in base]
    assert len(base) == 7 and starts == sorted(set(starts)) and all(s % 60 == 0 for s in starts)
    assert all(p.soc == 12.0 and p.power_w == 4000.0 and not p.grid_charge for p in base)


def test_program_diff_detects_every_field():
    base = baseline_programs(TW, 10.0, 5000.0)
    changed = (TouProgram(5, 4000.0, 11.0, True),) + base[1:]
    assert program_diff(changed, base) == {"tou.1.start", "tou.1.power_w", "tou.1.soc", "tou.1.grid_charge"}
    assert program_diff(base, base) == set()


# ── własności na losowych planach ──

_MODES = [
    {"mode": "self_consume"},
    {"mode": "self_consume", "discharge_purpose": "self"},
    {"mode": "charge", "charge_source": "grid"},
    {"mode": "charge", "charge_source": "pv"},
    {"mode": "charge"},
    {"mode": "discharge", "discharge_purpose": "sell"},
    {"mode": "discharge"},
    {"mode": "idle"},
    {"mode": "hold"},
]


def _random_plan(rng):
    step = TW.time_step_min
    start = DAY0 + timedelta(minutes=step * rng.randrange(0, 24 * 60 // step))
    t = start
    slots = []
    for _ in range(rng.randrange(0, 60)):
        if rng.random() < 0.15:
            t += timedelta(minutes=step * rng.randrange(1, 24))          # dziura
        dur = timedelta(minutes=step * rng.choice([1, 3, 6, 12, 24]))
        f = dict(rng.choice(_MODES))
        if rng.random() < 0.8:
            f["power_w"] = rng.choice([0, 300, 1500, 4000, 9000])
        if rng.random() < 0.7:
            f["soc_target"] = rng.choice([0, 5, 30, 80, 100])
        if rng.random() < 0.8:
            f["price_pln_kwh"] = round(rng.uniform(-0.5, 2.0), 3)
        slots.append({"from": _iso(t), "to": _iso(t + dur), **f})
        t += dur
    sch = parse_schedule({"schedule_id": "r", "slots": slots,
                          "fallback": {"mode": rng.choice(["self_consume", "idle"]),
                                       "soc_reserve": rng.choice([5, 20])}})
    now = start + timedelta(minutes=rng.randrange(0, 48 * 60))
    return sch, now


@pytest.mark.parametrize("seed", range(150))
def test_random_plans_hold_invariants(seed):
    rng = random.Random(seed)
    sch, now = _random_plan(rng)
    r = _run(sch, now=now)
    _check_shape(r, sch, now)
    assert r.lost_value_pln >= r.degrade_loss_pln - 1e-6 or r.merges
    # strata scaleń to suma przyrostów z kolejnych scaleń
    assert r.merge_loss_pln == pytest.approx(sum(m.lost_value_pln for m in r.merges), abs=1e-3)
    assert all(v > 0 for v in r.degraded.values())
    assert set(r.degraded) <= {"sell", "discharge_forced"}
    # wynik silnika przechodzi guardy bez poprawek (podłoga = rezerwa, moc <= znamionowa)
    ctx = GuardContext(soc=60.0, soc_age_s=0.0, temperature_ok=True, soc_reserve=10.0,
                       action=Action.SELF_CONSUME, max_charge_w=5000.0)
    g = apply_guards(r.params(), ctx, TW)
    assert g.status == STATUS_OK and g.write_allowed and g.params == r.params()
    # deterministycznie
    assert _run(sch, now=now) == r
    # po zmianie niczego zero różnic, program nigdy nie jest częściowy
    assert program_diff(r.programs, r.programs) == set()
    assert len(program_diff(r.programs, None)) == 4 * TW.tou_programs


def test_slot_longer_than_a_day_still_covers_now():
    sch = parse_schedule({"schedule_id": "t", "slots": [
        {"from": _iso(DAY0), "to": _iso(DAY0 + timedelta(hours=48)), **SELF}]})
    now = DAY0 + timedelta(hours=30, minutes=7)
    r = _run(sch, now=now)
    _check_shape(r, sch, now)
    assert r.windows[0].start == DAY0 + timedelta(hours=30, minutes=5)
