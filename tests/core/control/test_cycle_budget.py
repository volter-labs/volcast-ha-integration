"""Budżet zapisów NVM w cyklu: wstrzymanie, powrót do bazowego przy trybie wymuszonym, liczenie."""
from custom_components.volcast.core.control.baseline import needs_restore
from custom_components.volcast.core.control.cycle import (
    DRY_RUN, RESTORE, WRITE, ControlMemory, EntityContext, Gates, Limits, Telemetry, commit,
    decide_cycle)
from custom_components.volcast.core.control.group_writes import GroupReport
from custom_components.volcast.core.control.target import RegisterTarget
from custom_components.volcast.core.registers import RegisterWrite
from custom_components.volcast.core.write_sequence import WriteReport
from tests.core.golden import T0

from .conftest import MODE_REG, POWER_REG, goodwe_reading

NOW_WALL = T0.timestamp()
GATES = Gates(consent=True, local_switch=True, control_mode="direct", verified=True)
EGATES = Gates(consent=True, local_switch=True, control_mode="entities", verified=True)
MAPPED = {"mode": "select.ems_mode", "power_w": "number.ems_power", "soc_min": "number.dod",
          "soc_max": "number.soc_upper", "export_limit_w": "number.export_limit",
          "export_limit_enabled": "switch.export_limit"}
UNITS = {"power_w": "W", "soc_min": "%", "soc_max": "%", "export_limit_w": "W"}
ATTRS = {"number.ems_power": {"min": 0, "max": 10000, "step": 1},
         "number.dod": {"min": 0, "max": 99, "step": 1},
         "number.soc_upper": {"min": 10, "max": 100, "step": 1},
         "number.export_limit": {"min": 0, "max": 10000, "step": 1}}


def _exhaust(memory, key, n=None):
    for _ in range(n or memory.budget.per_key):
        memory.budget.note(key, NOW_WALL - 60)


def _run(profile, schedule, reading, memory, gates=GATES):
    return decide_cycle(profile=profile, schedule=schedule, now_utc=T0, now_mono=1000.0,
                        tele=Telemetry(soc=80.0, soc_age_s=5.0, battery_temp_c=25.0),
                        limits=Limits(rated_power_w=8000.0), gates=gates, memory=memory,
                        target=RegisterTarget(reading))


def _run_ents(profile, schedule, memory, readings=None):
    return decide_cycle(profile=profile, schedule=schedule, now_utc=T0, now_mono=1000.0,
                        tele=Telemetry(soc=80.0, soc_age_s=5.0, battery_temp_c=25.0),
                        limits=Limits(rated_power_w=8000.0), gates=EGATES, memory=memory,
                        ents=EntityContext("goodwe", MAPPED, UNITS, ATTRS,
                                           readings or {"mode": "auto", "power_w": 0.0}))


def test_budget_exhausted_power_holds_group(goodwe_profile, sell_schedule, reading_auto):
    memory = ControlMemory.for_profile(goodwe_profile)
    _exhaust(memory, "power_w")
    d = _run(goodwe_profile, sell_schedule, reading_auto, memory)
    keys = [w.key for w in d.writes]
    assert "power_w" not in keys and "mode" not in keys
    assert "nvm_budget" in d.notes and memory.budget.hit is True


def test_budget_exhausted_condition_holds_mode(goodwe_profile, charge_schedule, reading_auto):
    memory = ControlMemory.for_profile(goodwe_profile)
    _exhaust(memory, "soc_max")
    d = _run(goodwe_profile, charge_schedule, reading_auto, memory)
    keys = [w.key for w in d.writes]
    assert "soc_max" not in keys and "mode" not in keys and "power_w" not in keys
    assert "nvm_budget" in d.notes and "mode_held" in d.notes


def test_budget_exhausted_in_forced_mode_returns_to_baseline(goodwe_profile, sell_schedule):
    # Urządzenie ładuje z sieci 8 kW, budżet mocy wyczerpany → tryb bazowy, nie zamrożenie komendy.
    reading = goodwe_reading(goodwe_profile, **{str(MODE_REG): 11, str(POWER_REG): 8000, "37007": 80})
    memory = ControlMemory.for_profile(goodwe_profile)
    _exhaust(memory, "power_w")
    d = _run(goodwe_profile, sell_schedule, reading, memory)
    assert (d.status, d.reason) == (RESTORE, "nvm_budget")
    assert d.writes == [RegisterWrite("mode", MODE_REG, 1)]
    assert d.flat == {"mode": "auto"} and d.direction == "neutral"


def test_budget_restore_even_when_mode_itself_exhausted(goodwe_profile, sell_schedule):
    reading = goodwe_reading(goodwe_profile, **{str(MODE_REG): 11, str(POWER_REG): 8000, "37007": 80})
    memory = ControlMemory.for_profile(goodwe_profile)
    _exhaust(memory, "mode")
    d = _run(goodwe_profile, sell_schedule, reading, memory)
    assert d.status == RESTORE and d.writes == [RegisterWrite("mode", MODE_REG, 1)]


def test_budget_restore_is_dry_run_behind_closed_gates(goodwe_profile, sell_schedule):
    reading = goodwe_reading(goodwe_profile, **{str(MODE_REG): 11, str(POWER_REG): 8000, "37007": 80})
    memory = ControlMemory.for_profile(goodwe_profile)
    _exhaust(memory, "power_w")
    d = _run(goodwe_profile, sell_schedule, reading, memory,
             gates=Gates(consent=True, local_switch=True, control_mode="direct", verified=False))
    assert (d.status, d.reason) == (DRY_RUN, "unverified_profile")


def test_idle_with_power_is_forced(goodwe_profile, sell_schedule):
    reading = goodwe_reading(goodwe_profile, **{str(MODE_REG): 8, str(POWER_REG): 3000, "37007": 80})
    memory = ControlMemory.for_profile(goodwe_profile)
    _exhaust(memory, "power_w")
    assert _run(goodwe_profile, sell_schedule, reading, memory).status == RESTORE
    idle0 = goodwe_reading(goodwe_profile, **{str(MODE_REG): 8, str(POWER_REG): 0, "37007": 80})
    assert _run(goodwe_profile, sell_schedule, idle0, memory).status != RESTORE


def test_budget_absent_changes_nothing(goodwe_profile, sell_schedule, reading_auto):
    with_budget = ControlMemory.for_profile(goodwe_profile)
    without = ControlMemory.for_profile(goodwe_profile)
    without.budget = None
    a = _run(goodwe_profile, sell_schedule, reading_auto, with_budget)
    b = _run(goodwe_profile, sell_schedule, reading_auto, without)
    assert a == b
    ea = _run_ents(goodwe_profile, sell_schedule, ControlMemory.for_profile(goodwe_profile))
    no = ControlMemory.for_profile(goodwe_profile)
    no.budget = None
    assert ea == _run_ents(goodwe_profile, sell_schedule, no)


def test_commit_without_now_wall_counts_nothing(goodwe_profile, sell_schedule):
    memory = ControlMemory.for_profile(goodwe_profile)
    d = _run_ents(goodwe_profile, sell_schedule, memory)
    assert d.status == WRITE
    commit(d, WriteReport(written=[w.key for w in d.writes]), memory, 1000.0)
    assert memory.budget.to_list() == []


def test_entity_mode_commit_counts_service_calls(goodwe_profile, sell_schedule):
    memory = ControlMemory.for_profile(goodwe_profile)
    d = _run_ents(goodwe_profile, sell_schedule, memory)
    keys = [w.key for w in d.writes]
    rep = WriteReport(written=keys[:-1], failed=[keys[-1]])
    commit(d, rep, memory, 1000.0, now_wall=NOW_WALL)
    assert sorted(k for k, _ in memory.budget.to_list()) == sorted(keys)


def test_entity_written_then_restored_counts_two(goodwe_profile, sell_schedule):
    memory = ControlMemory.for_profile(goodwe_profile)
    d = _run_ents(goodwe_profile, sell_schedule, memory)
    rep = GroupReport(written=["power_w"], failed=["mode"], ambiguous=[], restored=["power_w"])
    commit(d, rep, memory, 1000.0, now_wall=NOW_WALL)
    counted = [k for k, _ in memory.budget.to_list()]
    assert counted.count("power_w") == 2 and counted.count("mode") == 1


def test_direct_commit_counts_nothing(goodwe_profile, sell_schedule, reading_auto):
    # Tryb bezpośredni: ramki liczy pisarz rejestrów (`on_send`), nie `commit`.
    memory = ControlMemory.for_profile(goodwe_profile)
    d = _run(goodwe_profile, sell_schedule, reading_auto, memory)
    commit(d, WriteReport(written=[w.key for w in d.writes]), memory, 1000.0, now_wall=NOW_WALL)
    assert memory.budget.to_list() == []


def test_restore_decision_commits_mode(goodwe_profile, sell_schedule):
    reading = goodwe_reading(goodwe_profile, **{str(MODE_REG): 11, str(POWER_REG): 8000, "37007": 80})
    memory = ControlMemory.for_profile(goodwe_profile)
    _exhaust(memory, "power_w")
    d = _run(goodwe_profile, sell_schedule, reading, memory)
    commit(d, WriteReport(written=["mode"]), memory, 1000.0, now_wall=NOW_WALL)
    assert memory.last_written["mode"] == "auto"


def test_needs_restore_active_mode_direct():
    kw = dict(owned=True, consent=True, local_switch=True, active_mode="direct")
    assert needs_restore(control_mode="direct", **kw) is False
    assert needs_restore(control_mode=None, **kw) is True
    assert needs_restore(control_mode="entities", **kw) is True
    assert needs_restore(owned=True, consent=True, local_switch=True, control_mode="entities") is False
