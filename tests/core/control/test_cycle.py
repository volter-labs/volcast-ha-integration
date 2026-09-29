import json
import math
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from custom_components.volcast.core.control import cycle as cyc
from custom_components.volcast.core.control.cycle import (BLOCKED, DRY_RUN, ERROR, IDLE, WRITE,
                                                          ControlMemory, CycleDecision, EntityContext,
                                                          Gates, Limits, Telemetry, commit, decide_cycle,
                                                          same_value)
from custom_components.volcast.core.profile import load_builtin, profile_from_dict
from custom_components.volcast.core.slot import parse_schedule
from custom_components.volcast.core.write_sequence import WriteReport
from custom_components.volcast.core.control.group_writes import GroupReport

from ..profile_fixtures import tw_profile

GW = load_builtin("goodwe-et")
NOW = datetime(2026, 9, 27, 10, 30, tzinfo=timezone.utc)
MAPPED = {"mode": "select.ems_mode", "power_w": "number.ems_power", "soc_min": "number.dod",
          "soc_max": "number.soc_upper", "export_limit_w": "number.export_limit",
          "export_limit_enabled": "switch.export_limit"}
UNITS = {"power_w": "W", "soc_min": "%", "soc_max": "%", "export_limit_w": "W"}
ATTRS = {"number.ems_power": {"min": 0, "max": 10000, "step": 1},
         "number.dod": {"min": 0, "max": 99, "step": 1},
         "number.soc_upper": {"min": 10, "max": 100, "step": 1},
         "number.export_limit": {"min": 0, "max": 10000, "step": 1}}
OPEN = Gates(consent=True, local_switch=True, control_mode="entities", verified=True)


def sched(**slot_over):
    slot = {"from": "2026-09-27T10:00:00Z", "to": "2026-09-27T11:00:00Z", "mode": "discharge",
            "discharge_purpose": "sell", "power_w": 2000, "price_pln_kwh": 0.8}
    slot.update(slot_over)
    return parse_schedule({"schedule_id": "s1", "slots": [slot],
                           "fallback": {"mode": "self_consume", "soc_reserve": 10},
                           "control_enabled": True})


SELL = {"mode": "discharge", "discharge_purpose": "sell", "price_pln_kwh": 0.8}
STANDBY = {"mode": "idle", "price_pln_kwh": 0.8}


def at(hms):
    return datetime.fromisoformat(f"2026-09-27T{hms}+00:00")


def slot(frm, to, **kw):
    return {"from": f"2026-09-27T{frm}:00Z", "to": f"2026-09-27T{to}:00Z", **kw}


def plan(*slots):
    return parse_schedule({"schedule_id": "s2", "slots": list(slots),
                           "fallback": {"mode": "self_consume", "soc_reserve": 10},
                           "control_enabled": True})


def run(memory=None, *, schedule="default", gates=OPEN, soc=60.0, age=10.0, temp=25.0, mapped=MAPPED,
        units=UNITS, attrs=ATTRS, readings=None, now_mono=1000.0, now_utc=NOW, profile=GW, owner_values=None):
    memory = memory or ControlMemory.for_profile(profile)
    return decide_cycle(
        profile=profile, schedule=sched() if schedule == "default" else schedule,
        now_utc=now_utc, now_mono=now_mono,
        tele=Telemetry(soc=soc, soc_age_s=age, battery_temp_c=temp),
        limits=Limits(rated_power_w=8000.0),
        ents=EntityContext(domain="goodwe", mapped=mapped, units=units, attrs=attrs, readings=readings or {},
                           owner_values=owner_values or {}),
        gates=gates, memory=memory), memory


def _written(d):
    return WriteReport(written=[w.key for w in d.writes])


# ── Ścieżka podstawowa ──

def test_sell_slot_writes_conditions_then_group_in_safe_order():
    d, _ = run(schedule=sched(export_allowed=False))
    assert d.status == WRITE and d.intent == "sell" and d.direction == "discharge"
    # Moc nieznana → tryb przed mocą (postój z dużym Xset to znana pułapka).
    assert [w.key for w in d.writes] == ["export_limit_w", "export_limit_enabled", "mode", "power_w"]
    assert d.writes[2].data == {"option": "sell_power"}


def test_no_mode_chosen_is_idle_and_does_not_touch_latch():
    d, mem = run(gates=replace(OPEN, control_mode=None), soc=5.0)
    assert (d.status, d.reason) == (IDLE, "no_mode_chosen") and mem.latch.is_engaged is False


@pytest.mark.parametrize("gates,reason", [
    (replace(OPEN, consent=None), "no_consent"),
    (replace(OPEN, consent=False), "no_consent"),
    (replace(OPEN, local_switch=False), "local_off"),
    (replace(OPEN, verified=False), "unverified_profile"),
])
def test_gates_make_dry_run_with_visible_writes(gates, reason):
    d, _ = run(gates=gates)
    assert (d.status, d.reason) == (DRY_RUN, reason) and d.writes


def test_paused_is_dry_run():
    mem = ControlMemory.for_profile(GW)
    mem.paused_until = 5000.0
    d, _ = run(mem, now_mono=1000.0)
    assert (d.status, d.reason) == (DRY_RUN, "paused")


def test_pause_over_writes_again():
    mem = ControlMemory.for_profile(GW)
    mem.paused_until = 5000.0
    d, _ = run(mem, now_mono=5000.0)
    assert d.status == WRITE


def test_time_window_profile_never_writes():
    d, _ = run(profile=profile_from_dict(tw_profile()))
    assert (d.status, d.reason, d.writes) == (IDLE, "tou_preview_only", [])


def test_missing_mode_entity_is_idle():
    d, _ = run(mapped={k: v for k, v in MAPPED.items() if k != "mode"})
    assert (d.status, d.reason, d.unmapped) == (IDLE, "missing_entities", ("mode",))


@pytest.mark.parametrize("key", [k for k in GW.raw["write_policy"]["order"] if k != "mode"])
def test_missing_setting_entity_is_dropped_not_blocking(key):
    # Nastawa bez encji (niedostępna, wyłączona, bez mapowania) = nieobsługiwana: wypada
    # z zapisów, reszta sterowania działa dalej.
    d, _ = run(mapped={k: v for k, v in MAPPED.items() if k != key},
               schedule=plan(slot("10:00", "11:00", mode="self_consume", export_allowed=False)))
    assert d.status == WRITE, (d.reason, d.unmapped)
    assert key not in [w.key for w in d.writes] and key not in d.flat


def test_missing_ceiling_entity_drops_plan_ceiling_and_still_charges():
    sched_ = plan(slot("10:00", "11:00", mode="charge", charge_source="grid", power_w=3000, soc_target=90))
    d, _ = run(mapped={k: v for k, v in MAPPED.items() if k != "soc_max"}, schedule=sched_)
    assert d.status == WRITE and "soc_max" not in [w.key for w in d.writes]
    assert "soc_max" in d.dropped_unsupported
    assert {"mode", "power_w"} <= {w.key for w in d.writes}


def test_missing_power_entity_blocks_only_intents_that_need_power():
    mapped = {k: v for k, v in MAPPED.items() if k != "power_w"}
    d, _ = run(mapped=mapped)
    assert (d.status, d.reason, d.writes) == (BLOCKED, "power_unsupported", [])
    d2, _ = run(mapped=mapped, schedule=plan(slot("10:00", "11:00", mode="self_consume")))
    assert d2.status == WRITE and d2.writes[-1].data == {"option": "auto"}


def test_uncapped_export_slot_never_touches_owner_limiter():
    d, _ = run(schedule=plan(slot("10:00", "11:00", mode="self_consume")),
               readings={"mode": "sell_power", "export_limit_enabled": 1.0, "export_limit_w": 8000.0})
    assert d.status == WRITE
    assert not {"export_limit_enabled", "export_limit_w"} & {w.key for w in d.writes}
    assert not {"export_limit_enabled", "export_limit_w"} & set(d.flat)


def test_uncapped_export_after_our_ban_returns_limiter_to_owner_values():
    owner = {"export_limit_enabled": 0.0, "export_limit_w": 4000.0}
    ents_readings = {"mode": "auto", "export_limit_enabled": 1.0, "export_limit_w": 0.0}
    d, _ = run(schedule=plan(slot("10:00", "11:00", mode="self_consume")), readings=ents_readings,
               owner_values=owner)
    writes = {w.key: w for w in d.writes}
    assert d.status == WRITE and writes["export_limit_enabled"].service == "turn_off"
    assert writes["export_limit_w"].data == {"value": 4000.0}


def test_no_plan_is_idle():
    d, _ = run(schedule=None)
    assert (d.status, d.reason) == (IDLE, "no_plan")


# ── Strażnicy i zatrzask ──

def test_stale_soc_is_blocked_by_guard():
    d, _ = run(age=999.0)
    assert (d.status, d.reason, d.writes) == (BLOCKED, "guard:I-9", [])


@pytest.mark.parametrize("soc,age", [(None, 10.0), (5.0, 999.0), (5.0, math.nan), (5.0, -1.0),
                                     (math.nan, 10.0), (150.0, 10.0)])
def test_latch_never_fed_unusable_soc(soc, age):
    d, mem = run(soc=soc, age=age)
    assert d.status == BLOCKED and d.reason == "guard:I-9" and d.writes == []
    assert mem.latch.is_engaged is False


def test_latch_not_fed_on_implausible_soc_jump():
    mem = ControlMemory.for_profile(GW)
    d = decide_cycle(
        profile=GW, schedule=sched(), now_utc=NOW, now_mono=1000.0,
        tele=Telemetry(soc=5.0, soc_age_s=10.0, battery_temp_c=25.0, previous_soc=60.0,
                       previous_soc_gap_s=60.0),
        limits=Limits(rated_power_w=8000.0),
        ents=EntityContext(domain="goodwe", mapped=MAPPED, units=UNITS, attrs=ATTRS, readings={}),
        gates=OPEN, memory=mem)
    assert (d.status, d.reason) == (BLOCKED, "guard:I-9") and mem.latch.is_engaged is False


def test_guard_uses_latch_return_value():
    mem = ControlMemory.for_profile(GW)
    mem.latch.engaged = lambda *_a: True     # zatrzask zwraca „założony", mimo is_engaged False
    d, _ = run(mem, soc=60.0)
    assert d.guard.invariant == "I-1" and "power_w" not in d.flat
    assert d.writes[-1].data == {"option": "auto"}


def test_latch_keeps_discharge_off_just_above_reserve():
    d1, mem = run(soc=9.0)
    assert d1.writes[-1].data == {"option": "auto"} and d1.guard.invariant == "I-1"
    d2, _ = run(mem, soc=12.0, now_mono=1120.0)
    assert d2.guard.invariant == "I-1"


def test_latch_tracks_soc_in_dry_run():
    _, mem = run(gates=replace(OPEN, consent=False), soc=9.0)
    assert mem.latch.is_engaged is True


def test_map_slot_error_fails_closed(monkeypatch):
    def boom(*_a, **_k):
        raise ValueError("bad slot")
    monkeypatch.setattr(cyc, "map_slot", boom)
    d, _ = run()
    assert (d.status, d.reason, d.writes) == (ERROR, "exception:ValueError", [])


def test_guard_exception_fails_closed(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("guard bug")
    monkeypatch.setattr(cyc, "apply_guards", boom)
    d, _ = run()
    assert (d.status, d.reason, d.writes) == (ERROR, "exception:RuntimeError", [])


def test_mapped_temperature_missing_blocks():
    d, mem = run(mapped={**MAPPED, "battery_temp_c": "sensor.battery_temp"}, temp=None, soc=5.0)
    assert (d.status, d.reason, d.writes) == (BLOCKED, "temperature_unknown", [])
    assert mem.latch.is_engaged is False


def test_unmapped_temperature_does_not_block():
    d, _ = run(temp=None)
    assert d.status == WRITE


def test_mapped_temperature_present_writes():
    d, _ = run(mapped={**MAPPED, "battery_temp_c": "sensor.battery_temp"}, temp=25.0)
    assert d.status == WRITE


def test_temperature_outside_window_is_blocked():
    d, _ = run(temp=70.0)
    assert (d.status, d.reason, d.writes) == (BLOCKED, "guard:I-3", [])


# ── Uzgadnianie, throttling, odczyty ──

def test_wrong_type_reading_zero_writes():
    d, mem = run()
    commit(d, _written(d), mem, 1000.0)
    d2, _ = run(mem, readings={"mode": 1.0}, now_mono=1100.0)
    assert (d2.status, d2.writes) == (ERROR, [])


def test_throttle_suppresses_repeat():
    d, mem = run()
    commit(d, _written(d), mem, 1000.0)
    d2, _ = run(mem, now_mono=1030.0)
    assert (d2.status, d2.reason) == (IDLE, "nothing_to_write")


def test_values_already_on_device_are_not_rewritten():
    readings = {"power_w": 2000.0, "export_limit_enabled": 0.0, "mode": "sell_power"}
    d, _ = run(readings=readings)
    assert (d.status, d.reason, d.writes) == (IDLE, "nothing_to_write", [])


def test_raw_switch_state_is_normalised():
    readings = {"power_w": 2000.0, "export_limit_enabled": "off", "mode": "sell_power"}
    d, _ = run(readings=readings)
    assert (d.status, d.reason, d.writes) == (IDLE, "nothing_to_write", [])


@pytest.mark.parametrize("bad", ["unavailable", "unknown", "", None, math.nan, math.inf])
def test_unreadable_values_are_no_reading_not_error(bad):
    d, mem = run()
    commit(d, _written(d), mem, 1000.0)
    d2, _ = run(mem, readings={"power_w": bad, "mode": bad, "export_limit_enabled": bad},
                now_mono=1100.0)
    assert (d2.status, d2.reason) == (IDLE, "nothing_to_write")


def test_foreign_change_resets_throttle_memory():
    d, mem = run()
    commit(d, _written(d), mem, 1000.0)
    d2, _ = run(mem, readings={"power_w": 2000.0, "export_limit_enabled": "off", "mode": "auto"},
                now_mono=1100.0)
    assert d2.status == WRITE and [w.key for w in d2.writes] == ["mode"]


def test_unsupported_dropped_for_session():
    d, mem = run()
    commit(d, WriteReport(written=["power_w", "mode"], unsupported=["export_limit_enabled"]), mem, 1000.0)
    d2, _ = run(mem, schedule=sched(power_w=3000, export_allowed=False), now_mono=2000.0)
    assert "export_limit_enabled" not in [w.key for w in d2.writes]
    assert "export_limit_enabled" in d2.dropped_unsupported


def test_unmapped_keys_reported_not_written():
    d, _ = run(mapped={k: v for k, v in MAPPED.items() if k != "export_limit_enabled"},
               schedule=sched(export_allowed=False))
    assert d.status == WRITE
    assert "export_limit_enabled" in d.dropped_unsupported
    assert "export_limit_enabled" not in [w.key for w in d.writes]


def test_runtime_unmapped_param_holds_mode():
    # Encja limitu eksportu zmieniła jednostkę na % — nie da się jej zapisać dokładnie.
    d, _ = run(schedule=sched(export_allowed=False), units={**UNITS, "export_limit_w": "%"})
    keys = [w.key for w in d.writes]
    assert "mode" not in keys and "power_w" not in keys and "export_limit_w" not in keys
    assert "export_limit_w" in d.unmapped and "mode_held" in d.notes
    assert d.direction is None


def test_direction_budget_blocks_mode_and_power():
    mem = ControlMemory.for_profile(GW)
    mem.limiter.record("charge", 0.0)
    for i, direction in enumerate(["discharge", "charge", "discharge", "charge"]):
        mem.limiter.record(direction, 10.0 * (i + 1))
    d, _ = run(mem, now_mono=100.0)
    keys = [w.key for w in d.writes]
    assert "mode" not in keys and "power_w" not in keys and "I-8" in d.notes


def test_fallback_after_plan_expiry_blocks_export():
    d, _ = run(now_utc=datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc))
    assert d.fallback is True and d.intent == "self_consume"
    assert d.flat["export_limit_enabled"] == 1.0 and d.flat["export_limit_w"] == 0.0


def test_unknown_entity_range_blocks_everything():
    attrs = {**ATTRS, "number.export_limit": {}}
    d, _ = run(schedule=sched(export_allowed=False), attrs=attrs)
    assert (d.status, d.reason, d.writes) == (BLOCKED, "entity_range_unknown", [])


def test_standby_zero_power_below_entity_min_holds_mode():
    # Standby = jawne 0 W; encja mocy z min 100 W tego nie przyjmie → tryb nie może pójść.
    attrs = {**ATTRS, "number.ems_power": {"min": 100, "max": 10000, "step": 1}}
    d, _ = run(schedule=sched(mode="idle", discharge_purpose=None, power_w=None), attrs=attrs)
    assert d.intent == "standby"
    assert (d.status, d.reason, d.writes) == (BLOCKED, "entity_range_unknown", [])
    assert "power_w" in d.unmapped and d.direction is None


def test_entity_fit_adjusts_before_throttle():
    # Plan 625,6 W → encja o kroku 1 W przyjmie 625 (moc w dół); pamięć widzi 625.
    d, _ = run(schedule=sched(power_w=625.6))
    assert d.flat["power_w"] == 625.0 and "power_w" in d.adjusted


# ── Pamięć po wykonaniu ──

def test_commit_records_direction_only_when_mode_attempted():
    d, mem = run()
    calls = []
    mem.limiter.record = lambda direction, now: calls.append(direction)
    commit(d, WriteReport(written=["power_w"], failed=["export_limit_enabled"], mode_held=True), mem, 1000.0)
    assert calls == [] and mem.last_written == {"power_w": 2000.0}
    commit(d, WriteReport(written=["power_w", "export_limit_enabled", "mode"]), mem, 1000.0)
    assert calls == ["discharge"]
    refused = GroupReport(written=["power_w", "export_limit_enabled"], failed=["mode"], ambiguous=[])
    commit(d, refused, mem, 1000.0)
    assert calls == ["discharge"]                  # tryb odrzucony na pewno — kierunek bez zmiany
    maybe = GroupReport(written=["power_w", "export_limit_enabled"], failed=["mode"], ambiguous=["mode"])
    commit(d, maybe, mem, 1000.0)
    assert calls == ["discharge", "discharge"]     # ERROR: tryb mógł dojść — liczy się do I-8
    commit(d, WriteReport(written=["power_w"], failed=["mode"]), mem, 1000.0)
    assert calls == ["discharge"] * 3               # raport bez podziału wyników = niepewny


def test_commit_ignores_non_write_decision():
    d, mem = run(gates=replace(OPEN, consent=False))
    commit(d, _written(d), mem, 1000.0)
    assert mem.last_written == {}
    d2, _ = run(mem, now_mono=1010.0)
    assert d2.status == WRITE and len(d2.writes) == 2


def test_summary_is_small_and_json_safe():
    d, _ = run()
    s = d.summary()
    assert json.loads(json.dumps(s)) == s and len(json.dumps(s)) < 1024


def test_summary_of_bare_decision():
    s = CycleDecision(IDLE, "no_profile").summary()
    assert s["status"] == IDLE and s["guard"] is None and s["would_write"] == []


@pytest.mark.parametrize("a,b,same", [("auto", "auto", True), ("auto", "sell_power", False),
                                      (2000.0, 2000.4, True), (2000.0, 2001.0, False),
                                      ("2000", 2000.0, False), (1.0, "on", False)])
def test_same_value(a, b, same):
    assert same_value(a, b) is same


# ── Tryb i jego nastawa to jedna grupa ──

def _group_keys(d):
    return [w.key for w in d.writes if w.key in ("mode", "power_w")]


def test_standby_never_written_over_pending_power():
    # t=0 sprzedaż 2000 W; t=240 poprawiony plan 2500 W; t=299.9 slot postoju.
    d, mem = run(schedule=plan(slot("10:00", "11:00", power_w=2000, **SELL)), now_mono=0.0)
    commit(d, _written(d), mem, 0.0)
    p2 = plan(slot("10:00", "10:35", power_w=2500, **SELL), slot("10:35", "11:00", **STANDBY))
    d2, _ = run(mem, schedule=p2, now_utc=at("10:34:00"), now_mono=240.0)
    assert [w.key for w in d2.writes] == ["power_w"]          # tryb już ten sam — sama nastawa
    commit(d2, _written(d2), mem, 240.0)
    d3, _ = run(mem, schedule=p2, now_utc=at("10:35:30"), now_mono=299.9)
    assert d3.intent == "standby" and _group_keys(d3) == [] and "mode_held" in d3.notes
    d4, _ = run(mem, schedule=p2, now_utc=at("10:36:00"), now_mono=300.5)
    assert [(w.key, w.data) for w in d4.writes] == [("power_w", {"value": 0.0}),
                                                     ("mode", {"option": "battery_standby"})]


def test_power_never_written_into_pending_standby():
    d, mem = run(now_mono=1000.0)
    commit(d, _written(d), mem, 1000.0)
    dev = {"power_w": 0.0, "export_limit_enabled": 0.0, "mode": "sell_power"}
    d2, _ = run(mem, schedule=plan(slot("10:00", "11:00", **STANDBY)), readings=dev, now_mono=1070.0)
    assert [w.key for w in d2.writes] == ["mode"]             # 0 W falownik już ma
    commit(d2, _written(d2), mem, 1070.0)
    dev2 = {**dev, "mode": "battery_standby"}
    d3, _ = run(mem, schedule=sched(power_w=2000), readings=dev2, now_mono=1100.0)
    assert _group_keys(d3) == [] and d3.status == IDLE
    d4, _ = run(mem, schedule=sched(power_w=2000), readings=dev2, now_mono=1130.5)
    assert _group_keys(d4) == ["mode", "power_w"]                # 0 → 2000 W: tryb najpierw


@pytest.mark.parametrize("gap", [5.0, 30.0, 59.9])
def test_mode_change_with_pending_condition_param_holds_group(gap):
    # Warunek trybu (blokada eksportu) czeka w interwale → tryb i moc też czekają.
    d, mem = run(schedule=sched(export_allowed=False), now_mono=1000.0)
    commit(d, _written(d), mem, 1000.0)
    owner = {"export_limit_enabled": 0.0, "export_limit_w": 4000.0}
    d1, _ = run(mem, now_mono=1070.0, owner_values=owner)          # eksport znów dozwolony → limit właściciela
    assert [w.key for w in d1.writes] == ["export_limit_w", "export_limit_enabled"]
    commit(d1, _written(d1), mem, 1070.0)
    blocked = plan(slot("10:00", "11:00", export_allowed=False, **STANDBY))
    d2, _ = run(mem, schedule=blocked, now_mono=1070.0 + gap)
    assert d2.writes == [] and "mode_held" in d2.notes
    d3, _ = run(mem, schedule=blocked, now_mono=1130.5)
    assert [w.key for w in d3.writes] == ["export_limit_w", "export_limit_enabled", "power_w", "mode"]


def test_power_alone_goes_when_device_has_planned_mode():
    d, mem = run(now_mono=1000.0)
    commit(d, _written(d), mem, 1000.0)
    dev = {"mode": "sell_power", "power_w": 2000.0, "export_limit_enabled": 0.0}
    d2, _ = run(mem, schedule=sched(power_w=2600), readings=dev, now_mono=1070.0)
    assert [w.key for w in d2.writes] == ["power_w"]


def test_runtime_unmapped_param_does_not_stop_power_in_confirmed_mode():
    dev = {"mode": "sell_power", "power_w": 2000.0, "export_limit_enabled": 1.0}
    d, _ = run(schedule=sched(power_w=2600, export_allowed=False), units={**UNITS, "export_limit_w": "%"},
               readings=dev)
    assert [w.key for w in d.writes] == ["power_w"] and "mode_held" in d.notes


def test_unsupported_mode_option_blocks_intent_for_session():
    d, mem = run()
    commit(d, WriteReport(written=["power_w", "export_limit_enabled"], unsupported=["mode"]), mem, 1000.0)
    dev = {"mode": "battery_standby", "power_w": 2000.0, "export_limit_enabled": 0.0}
    d2, _ = run(mem, schedule=sched(power_w=3000), readings=dev, now_mono=2000.0)
    assert (d2.status, d2.reason, d2.writes) == (BLOCKED, "mode_unsupported", [])
    assert d2.dropped_unsupported == ("mode:sell_power",)
    # Inna intencja (postój) jest nadal sterowalna.
    d3, _ = run(mem, schedule=plan(slot("10:00", "11:00", **STANDBY)), readings=dev, now_mono=2100.0)
    assert d3.status == WRITE and [w.key for w in d3.writes] == ["power_w"]


def test_unsupported_power_blocks_powered_intents_for_session():
    d, mem = run()
    commit(d, WriteReport(written=["export_limit_enabled", "mode"], unsupported=["power_w"]), mem, 1000.0)
    d2, _ = run(mem, schedule=plan(slot("10:00", "11:00", **STANDBY)), now_mono=2000.0)
    assert (d2.status, d2.reason, d2.writes) == (BLOCKED, "power_unsupported", [])
    d3, _ = run(mem, schedule=plan(slot("10:00", "11:00", mode="self_consume")), now_mono=2100.0)
    assert d3.status == WRITE and d3.writes[-1].data == {"option": "auto"}


def test_mode_option_missing_on_entity_blocks_before_any_write():
    attrs = {**ATTRS, "select.ems_mode": {"options": ["auto", "battery_standby"]}}
    d, _ = run(attrs=attrs)
    assert (d.status, d.reason, d.writes) == (BLOCKED, "mode_unsupported", [])
    d2, _ = run(attrs=attrs, schedule=plan(slot("10:00", "11:00", **STANDBY)))
    assert d2.status == WRITE


def test_unknown_device_option_blocks_and_signals_takeover():
    d, mem = run()
    commit(d, _written(d), mem, 1000.0)
    dev = {"mode": "export_ac", "power_w": 2000.0, "export_limit_enabled": 0.0}
    d2, _ = run(mem, readings=dev, now_mono=1100.0)
    assert (d2.status, d2.reason, d2.writes, d2.takeover) == (BLOCKED, "foreign_mode", [], True)
    assert d2.summary()["takeover"] is True


def test_unreadable_mode_is_not_a_takeover():
    d, mem = run()
    commit(d, _written(d), mem, 1000.0)
    d2, _ = run(mem, readings={"mode": "unavailable"}, now_mono=1100.0)
    assert d2.takeover is False and d2.reason == "nothing_to_write"


def test_bool_reading_is_a_flag():
    readings = {"power_w": 2000.0, "export_limit_enabled": False, "mode": "sell_power"}
    d, _ = run(readings=readings)
    assert (d.status, d.reason, d.writes) == (IDLE, "nothing_to_write", [])


# ── Układ grupy dla wykonawcy grupowego ──

def test_power_decrease_goes_power_first_with_restore_from_device():
    dev = {"mode": "sell_power", "power_w": 3000.0, "export_limit_enabled": 0.0}
    d, _ = run(schedule=plan(slot("10:00", "11:00", **STANDBY)), readings=dev)
    assert [w.key for w in d.writes] == ["power_w", "mode"]
    assert d.restore["power_w"].data == {"value": 3000.0}
    assert d.restore["mode"].data == {"option": "sell_power"}


def test_restore_falls_back_to_memory_without_reading():
    d, mem = run()
    commit(d, _written(d), mem, 1000.0)
    d2, _ = run(mem, schedule=plan(slot("10:00", "11:00", **STANDBY)), now_mono=1100.0)
    assert [w.key for w in d2.writes] == ["power_w", "mode"]
    assert d2.restore["power_w"].data == {"value": 2000.0}


def test_foreign_option_with_our_mode_in_memory_writes_nothing():
    # Pamięć zna nasz postój; obcy tryb na urządzeniu nie może dać powrotu do postoju.
    d, mem = run(schedule=plan(slot("10:00", "11:00", **STANDBY)))
    commit(d, _written(d), mem, 1000.0)
    dev = {"mode": "export_ac", "power_w": 1000.0, "export_limit_enabled": 0.0}
    d2, _ = run(mem, schedule=plan(slot("10:00", "11:00", mode="charge", charge_source="grid",
                                        power_w=3000)), readings=dev, now_mono=2000.0)
    assert (d2.status, d2.writes, d2.restore, d2.takeover) == (BLOCKED, [], {}, True)


def test_no_mode_restore_into_idle_mode_with_power():
    dev = {"mode": "battery_standby", "power_w": 1000.0, "export_limit_enabled": 0.0}
    d, _ = run(schedule=plan(slot("10:00", "11:00", mode="charge", charge_source="grid", power_w=3000)),
               readings=dev)
    assert [w.key for w in d.writes] == ["mode", "power_w"] and "mode" not in d.restore


def test_restore_value_is_fitted_to_entity_range():
    dev = {"mode": "sell_power", "power_w": 12000.0, "export_limit_enabled": 0.0}
    d, _ = run(schedule=plan(slot("10:00", "11:00", **STANDBY)), readings=dev)
    assert [w.key for w in d.writes] == ["power_w", "mode"]
    assert d.restore["power_w"].data == {"value": 10000.0} and d.restore_flat["power_w"] == 10000.0


def test_single_group_member_has_no_restore():
    dev = {"mode": "sell_power", "power_w": 2000.0, "export_limit_enabled": 0.0}
    d, _ = run(schedule=sched(power_w=2600), readings=dev)
    assert [w.key for w in d.writes] == ["power_w"] and d.restore == {}


def test_unfittable_restore_value_is_dropped():
    attrs = {**ATTRS, "number.ems_power": {"min": 100, "max": 10000, "step": 1}}
    dev = {"mode": "sell_power", "power_w": 50.0, "export_limit_enabled": 0.0}
    d, _ = run(schedule=plan(slot("10:00", "11:00", mode="charge", charge_source="grid", power_w=3000)),
               readings=dev, attrs=attrs)
    assert [w.key for w in d.writes] == ["mode", "power_w"]
    assert "power_w" not in d.restore and d.restore["mode"].data == {"option": "sell_power"}


@pytest.mark.parametrize("dev,slot_kw,safe", [
    ({"mode": "sell_power", "power_w": 1500.0}, dict(mode="charge", charge_source="grid", power_w=3000),
     ("mode",)),                                                      # powrót do sprzedaży: bezpieczny
    ({"mode": "charge_battery", "power_w": 1500.0}, dict(mode="discharge", discharge_purpose="sell",
                                                         power_w=3000), ()),     # powrót do ładowania
    ({"mode": "sell_power", "power_w": 3000.0}, dict(mode="idle"), ()),          # planowany postój
    ({"mode": "charge_battery", "power_w": 3000.0}, dict(mode="discharge", discharge_purpose="sell",
                                                         power_w=2000), ("power_w",)),
    ({"mode": "sell_power", "power_w": 3000.0}, dict(mode="charge", charge_source="grid", power_w=2000),
     ()),                                                             # planowane ładowanie
])
def test_restore_ambiguous_safe_per_direction(dev, slot_kw, safe):
    d, _ = run(schedule=plan(slot("10:00", "11:00", **slot_kw)),
               readings={**dev, "export_limit_enabled": 0.0})
    assert d.restore_ambiguous_safe == safe


def test_no_mode_restore_into_standby_with_unknown_power():
    d, _ = run(schedule=plan(slot("10:00", "11:00", mode="charge", charge_source="grid", power_w=3000)),
               readings={"mode": "battery_standby", "export_limit_enabled": 0.0})
    assert [w.key for w in d.writes] == ["mode", "power_w"] and "mode" not in d.restore
