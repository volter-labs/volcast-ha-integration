"""Cykl okien czasowych (Deye) w trybie bezpośrednim."""
import pytest
from datetime import date, datetime, timedelta, timezone

from custom_components.volcast.core.control.cycle import ControlMemory, Gates
from custom_components.volcast.core.control.tou_cycle import commit_tou
from custom_components.volcast.core.control.tou_writes import TouReport, run_tou_writes
from custom_components.volcast.core.engines.time_window import compress, program_diff
from custom_components.volcast.core.write_sequence import OK, UNSUPPORTED
from tests.sim.fixtures import deye_words

from .tou_helpers import (DEYE, NOW, SELF, WAW, apply, daily_plan, decide, iso, reading)
from custom_components.volcast.core.slot import parse_schedule


def _settled_words(plan):
    d, _ = decide(plan, reading())
    return apply(deye_words(), d.writes)


def test_first_takeover_writes_all_changed_fields_then_enable():
    plan = daily_plan()
    d, _ = decide(plan, reading())
    assert d.status == "write"
    expected = compress(plan, NOW, DEYE, soc_reserve=10.0, rated_power_w=10000.0, tz=WAW, anchor="day").programs
    assert d.programs == expected
    keys = [w.key for w in d.writes]
    assert keys[0] == keys[-1] == "tou_enable"
    assert d.writes[0].value & 1 == 0 and d.writes[-1].value & 1 == 1
    assert set(keys[1:-1]) == program_diff(expected, reading().programs)
    order = [(int(k.split(".")[1]), DEYE.tou_field_order.index(k.split(".")[2])) for k in keys[1:-1]]
    assert order == sorted(order)                        # programy po kolei, pola wg profilu


def test_unchanged_programs_write_nothing():
    plan = daily_plan()
    d, _ = decide(plan, reading(_settled_words(plan)))
    assert (d.status, d.reason, d.writes) == ("idle", "nothing_to_write", [])


def test_enable_on_rewrite_disables_first():
    plan = daily_plan()
    words = _settled_words(plan)
    words[166] = 55                                      # właściciel zmienił SoC programu 1
    d, _ = decide(plan, reading(words))
    assert [w.key for w in d.writes] == ["tou_enable", "tou.1.soc", "tou_enable"]
    assert d.writes[0].value == 0xFE and d.writes[-1].value == 0xFF


def test_enable_off_rewrite_does_not_touch_enable_until_end():
    plan = daily_plan()
    words = _settled_words(plan)
    words[166], words[146] = 55, 0xFE
    d, _ = decide(plan, reading(words))
    assert [w.key for w in d.writes] == ["tou.1.soc", "tou_enable"] and d.writes[-1].value == 0xFF


def test_enable_off_with_settled_programs_turns_it_on():
    plan = daily_plan()
    words = _settled_words(plan)
    words[146] = 0xFE
    d, _ = decide(plan, reading(words))
    assert [w.key for w in d.writes] == ["tou_enable"] and d.writes[0].value == 0xFF


def test_owner_days_used_when_enabling():
    plan = daily_plan()
    words = _settled_words(plan)
    words[146] = 0                                       # OFF, bez dni
    d, _ = decide(plan, reading(words), owner_word=0b0111110)
    assert d.writes[-1].value == 0b0111111


def _hourly_plan(pattern, first_local_day, days=6):
    t = datetime.combine(first_local_day, datetime.min.time(), tzinfo=WAW).astimezone(timezone.utc)
    end = datetime.combine(first_local_day + timedelta(days=days), datetime.min.time(),
                           tzinfo=WAW).astimezone(timezone.utc)
    slots = []
    while t < end:
        slots.append({"from": iso(t), "to": iso(t + timedelta(hours=1)), **pattern(t.astimezone(WAW))})
        t += timedelta(hours=1)
    return parse_schedule({"schedule_id": "dst", "slots": slots, "fallback": {"mode": "self_consume", "soc_reserve": 10}})


_CH = {"mode": "charge", "charge_source": "grid", "power_w": 3000, "soc_target": 90, "price_pln_kwh": 0.2}
_IDLE = {"mode": "idle", "price_pln_kwh": 0.9}


@pytest.mark.parametrize("first_day,start,pattern", [
    # jesień 2026-10-25: obie godziny 02:00 różnią się intencją (pierwsza ładuje z sieci)
    (date(2026, 10, 22), datetime(2026, 10, 23, 12, tzinfo=timezone.utc),
     lambda loc: (_CH if loc.utcoffset() == timedelta(hours=2) else _IDLE) if loc.hour == 2
     else _IDLE if 17 <= loc.hour < 19 else SELF),
    # wiosna 2026-03-29: ładowanie o 02:00, postój o 03:00 (doba przed zmianą to zwykła doba)
    (date(2026, 3, 26), datetime(2026, 3, 27, 12, tzinfo=timezone.utc),
     lambda loc: _CH if loc.hour == 2 else _IDLE if loc.hour == 3 else SELF),
])
def test_dst_never_freezes_decisions(first_day, start, pattern):
    plan = _hourly_plan(pattern, first_day)
    for i in range(0, 60 * 12, 3):                       # co 15 min przez 60 h
        d, _ = decide(plan, reading(), now=start + timedelta(minutes=5 * i))
        assert d.status == "write", (start + timedelta(minutes=5 * i), d.reason)


def test_programs_unreadable_blocks():
    words = deye_words()
    del words[150]
    d, _ = decide(daily_plan(), reading(words))
    assert (d.status, d.reason, d.writes) == ("blocked", "tou_unreadable", [])


def test_stale_reading_blocks_i9():
    d, _ = decide(daily_plan(), reading(), age=400.0)
    assert (d.status, d.reason) == ("blocked", "guard:I-9") and d.writes == []


def test_tou_power_from_slot_power():
    pattern = {h: ({"mode": "charge", "charge_source": "grid", "power_w": 3000, "soc_target": 90,
                    "battery_ac_w": 9999, "price_pln_kwh": 0.2} if 2 <= h < 5 else SELF) for h in range(24)}
    d, _ = decide(daily_plan(pattern), reading())
    charge = [p for p in d.programs if p.grid_charge]
    assert charge and all(p.power_w == 3000.0 for p in charge)


def test_sell_intent_degraded_reported():
    pattern = {h: ({"mode": "discharge", "discharge_purpose": "sell", "power_w": 2000, "price_pln_kwh": 1.2}
                   if 18 <= h < 20 else SELF) for h in range(24)}
    d, _ = decide(daily_plan(pattern), reading())
    assert "degraded:sell" in d.notes and d.lost_value_pln is not None


def test_min_interval_throttles_rewrites():
    plan = daily_plan()
    d, memory = decide(plan, reading())
    rep = run_tou_writes(d.writes, lambda w: OK, pre_held=d.pre_held)
    commit_tou(d, rep, memory, 1000.0)
    words = apply(deye_words(), d.writes)
    # nowy plan tuż po zapisie: inny cel SoC ładowania
    pattern = {h: ({"mode": "charge", "charge_source": "grid", "power_w": 3000, "soc_target": 85,
                    "price_pln_kwh": 0.2} if 2 <= h < 5 else SELF) for h in range(24)}
    plan2 = daily_plan(pattern)
    held, _ = decide(plan2, reading(words), memory=memory, now_mono=1100.0)
    assert (held.status, held.reason, held.writes) == ("idle", "held", []) and "I-6" in held.notes
    later, _ = decide(plan2, reading(words), memory=memory, now_mono=1400.0)
    keys = [w.key for w in later.writes]
    assert keys[0] == keys[-1] == "tou_enable" and all(k.endswith(".soc") for k in keys[1:-1])


def test_budget_exhausted_holds_enable():
    memory = ControlMemory.for_profile(DEYE)
    for _ in range(memory.budget.per_key):
        memory.budget.note("tou_enable", NOW.timestamp() - 60)
    d, _ = decide(daily_plan(), reading(), memory=memory)
    assert "tou_enable" in d.pre_held and "nvm_budget" in d.notes
    rep = run_tou_writes(d.writes, lambda w: OK, pre_held=d.pre_held)
    assert rep.held == ["tou_enable"] and rep.enable_written is False and rep.restore_needed is False


def test_budget_exhausted_field_holds_rest():
    memory = ControlMemory.for_profile(DEYE)
    d0, _ = decide(daily_plan(), reading())
    first_field = d0.writes[1].key
    for _ in range(memory.budget.per_key):
        memory.budget.note(first_field, NOW.timestamp() - 60)
    d, _ = decide(daily_plan(), reading(), memory=memory)
    assert first_field in d.pre_held
    rep = run_tou_writes(d.writes, lambda w: OK, pre_held=d.pre_held)
    assert rep.held[0] == first_field and rep.held[-1] == "tou_enable"


def test_unverified_is_dry_run_with_programs_in_summary():
    gates = Gates(consent=True, local_switch=True, control_mode="direct", verified=False)
    d, _ = decide(daily_plan(), reading(), gates=gates)
    assert (d.status, d.reason) == ("dry_run", "unverified_profile")
    s = d.summary()
    assert s["would_write"] and len(s["programs"]) == 6 and "lost_value_pln" in s
    assert set(s["programs"][0]) == {"start_min", "power_w", "soc", "grid_charge"}


def test_other_control_mode_is_idle():
    d, _ = decide(daily_plan(), reading(), gates=Gates(consent=True, local_switch=True,
                                                        control_mode="entities", verified=True))
    assert (d.status, d.reason) == ("idle", "no_mode_chosen")


def test_no_plan_is_idle():
    d, _ = decide(None, reading())
    assert (d.status, d.reason) == ("idle", "no_plan")


def test_unsupported_field_disables_tou_for_session():
    d, memory = decide(daily_plan(), reading())
    rep = run_tou_writes(d.writes, lambda w: UNSUPPORTED if w.key.startswith("tou.2") else OK)
    commit_tou(d, rep, memory, 1000.0)
    assert "tou" in memory.unsupported
    again, _ = decide(daily_plan(), reading(), memory=memory, now_mono=5000.0)
    assert (again.status, again.reason) == ("blocked", "tou_unsupported")


def test_commit_records_written_fields_and_uncertain():
    d, memory = decide(daily_plan(), reading())
    field = d.writes[1].key
    rep = TouReport(written=[field], failed=[d.writes[2].key], ambiguous=[d.writes[2].key])
    commit_tou(d, rep, memory, 1000.0)
    assert memory.last_written[field] == d.flat[field]
    assert memory.uncertain == {d.writes[2].key}


def test_commit_counts_frames_only_with_now_wall():
    d, memory = decide(daily_plan(), reading())
    rep = run_tou_writes(d.writes, lambda w: OK, pre_held=d.pre_held)
    commit_tou(d, rep, memory, 1000.0)
    assert memory.budget.to_list() == []
    commit_tou(d, rep, memory, 1000.0, now_wall=NOW.timestamp())
    counted = [k for k, _ in memory.budget.to_list()]
    assert counted.count("tou_enable") == 2 and len(counted) == len(d.writes)


def test_decide_tou_cycle_fail_closed_on_exception():
    d, _ = decide(daily_plan(), reading(), rated=None)
    assert d.status == "error" and d.reason.startswith("exception:") and d.writes == []
