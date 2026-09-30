"""Cykl sterowania na celu rejestrowym — ten sam rdzeń co tryb encji."""
from custom_components.volcast.core.control.cycle import (
    BLOCKED, DRY_RUN, ERROR, IDLE, WRITE, ControlMemory, EntityContext, Gates, Limits, Telemetry,
    decide_cycle)
from custom_components.volcast.core.control.target import RegisterTarget
from custom_components.volcast.core.registers import RegisterWrite
from tests.core.golden import T0

from .conftest import MODE_REG, POWER_REG, SOC_REG, goodwe_reading


def _gates(**kw):
    base = dict(consent=True, local_switch=True, control_mode="direct", verified=True)
    return Gates(**{**base, **kw})


def _run(profile, schedule, reading, *, memory=None, gates=None, age=5.0, unreadable=frozenset()):
    memory = memory or ControlMemory.for_profile(profile)
    return decide_cycle(profile=profile, schedule=schedule, now_utc=T0, now_mono=1000.0,
                        tele=Telemetry(soc=80.0, soc_age_s=age, battery_temp_c=25.0),
                        limits=Limits(rated_power_w=8000.0), gates=gates or _gates(),
                        memory=memory, target=RegisterTarget(reading, unreadable=unreadable)), memory


def test_direct_sell_slot_writes_registers_params_before_mode(goodwe_profile, sell_schedule, reading_auto):
    d, _ = _run(goodwe_profile, sell_schedule, reading_auto)
    assert d.status == WRITE and d.target_kind == "direct"
    assert all(isinstance(w, RegisterWrite) for w in d.writes)
    assert [w.addr for w in d.writes][-2:] in ([47512, 47511], [47511, 47512])   # grupa na końcu
    assert all(w.addr != 47760 or w.key == "soc_max" for w in d.writes)
    assert next(w for w in d.writes if w.key == "mode").value == goodwe_profile.mode_value("sell_power")


def test_entities_mode_does_not_drive_register_target(goodwe_profile, sell_schedule, reading_auto):
    d, _ = _run(goodwe_profile, sell_schedule, reading_auto, gates=_gates(control_mode="entities"))
    assert (d.status, d.reason) == (IDLE, "no_mode_chosen")


def test_unverified_direct_is_dry_run(goodwe_profile, sell_schedule, reading_auto):
    d, _ = _run(goodwe_profile, sell_schedule, reading_auto, gates=_gates(verified=False))
    assert (d.status, d.reason) == (DRY_RUN, "unverified_profile")
    assert d.writes


def test_foreign_mode_value_blocks_with_takeover(goodwe_profile, sell_schedule, reading_mode_99):
    d, _ = _run(goodwe_profile, sell_schedule, reading_mode_99)
    assert (d.status, d.reason, d.takeover) == (BLOCKED, "foreign_mode", True)
    assert d.writes == []


def test_settled_registers_not_rewritten(goodwe_profile, sell_schedule, reading_auto):
    first, _ = _run(goodwe_profile, sell_schedule, reading_auto)
    over = {str(w.addr): w.value for w in first.writes}
    over[str(SOC_REG)] = 80
    d, _ = _run(goodwe_profile, sell_schedule, goodwe_reading(goodwe_profile, **over))
    assert (d.status, d.reason) == (IDLE, "nothing_to_write")


def test_stale_reading_blocks_i9(goodwe_profile, sell_schedule, reading_auto):
    d, _ = _run(goodwe_profile, sell_schedule, reading_auto, age=400.0)
    assert (d.status, d.reason) == (BLOCKED, "guard:I-9") and d.writes == []


def test_unreadable_soc_max_dropped_not_holding_mode(goodwe_profile, charge_schedule, reading_auto):
    memory = ControlMemory.for_profile(goodwe_profile)
    memory.unsupported = {"soc_max"}
    d, _ = _run(goodwe_profile, charge_schedule, reading_auto, memory=memory, unreadable=frozenset({"soc_max"}))
    assert d.status == WRITE and "mode" in [w.key for w in d.writes]
    assert "soc_max" in d.dropped_unsupported and "mode_held" not in d.notes
    assert all(w.addr != 47760 for w in d.writes)


def test_soc_max_without_reading_written_when_supported(goodwe_profile, charge_schedule, reading_auto):
    d, _ = _run(goodwe_profile, charge_schedule, reading_auto)
    assert RegisterWrite("soc_max", 47760, 90) in d.writes and d.writes[-1].key in ("mode", "power_w")


def test_uncertain_register_resolved_by_reading(goodwe_profile, sell_schedule, reading_auto):
    memory = ControlMemory.for_profile(goodwe_profile)
    memory.uncertain = {"power_w", "soc_max"}
    _run(goodwe_profile, sell_schedule, reading_auto, memory=memory)
    assert memory.uncertain == {"soc_max"}                      # 47760 bez odczytu — dalej niepewny


def test_group_restore_writes_are_register_writes(goodwe_profile, sell_schedule, reading_auto):
    d, _ = _run(goodwe_profile, sell_schedule, reading_auto)
    assert d.restore == {"mode": RegisterWrite("mode", MODE_REG, 1),
                         "power_w": RegisterWrite("power_w", POWER_REG, 8846)}
    assert d.restore_flat == {"mode": "auto", "power_w": 8846.0}


def test_same_cycle_code_as_entities(goodwe_profile, sell_schedule, reading_auto):
    # Ten sam plan przez encje i przez rejestry daje te same klucze w tej samej kolejności.
    readings = {"mode": "auto", "power_w": 8846.0, "soc_min": 5.0, "export_limit_w": 16000.0,
                "export_limit_enabled": 0.0}
    ents = EntityContext(
        domain="goodwe",
        mapped={"mode": "select.ems_mode", "power_w": "number.ems_power", "soc_min": "number.dod",
                "soc_max": "number.soc_upper", "export_limit_w": "number.export_limit",
                "export_limit_enabled": "switch.export_limit"},
        units={"power_w": "W", "soc_min": "%", "soc_max": "%", "export_limit_w": "W"},
        attrs={"number.ems_power": {"min": 0, "max": 65535, "step": 1},
               "number.dod": {"min": 0, "max": 100, "step": 1},
               "number.soc_upper": {"min": 0, "max": 100, "step": 1},
               "number.export_limit": {"min": 0, "max": 65535, "step": 1}},
        readings=readings)
    e = decide_cycle(profile=goodwe_profile, schedule=sell_schedule, now_utc=T0, now_mono=1000.0,
                     tele=Telemetry(soc=80.0, soc_age_s=5.0, battery_temp_c=25.0),
                     limits=Limits(rated_power_w=8000.0), gates=_gates(control_mode="entities"),
                     memory=ControlMemory.for_profile(goodwe_profile), ents=ents)
    r, _ = _run(goodwe_profile, sell_schedule, reading_auto)
    assert [w.key for w in e.writes] == [w.key for w in r.writes]
    # Moc sprzedaży (`slot_live_export`) liczy się z odczytów PV/poboru tylko w trybie encji —
    # tu bez tych odczytów encje dają 0 W; cel rejestrowy zachowuje moc baterii z planu.
    assert {k: v for k, v in e.flat.items() if k != "power_w"} == \
        {k: v for k, v in r.flat.items() if k != "power_w"}
    assert e.flat["power_w"] == 0.0 and r.flat["power_w"] == 2500.0
    assert e.status == r.status == WRITE


def test_target_and_ents_exclusive(goodwe_profile, sell_schedule, reading_auto):
    kw = dict(profile=goodwe_profile, schedule=sell_schedule, now_utc=T0, now_mono=1000.0,
              tele=Telemetry(soc=80.0, soc_age_s=5.0, battery_temp_c=25.0),
              limits=Limits(rated_power_w=8000.0), gates=_gates(),
              memory=ControlMemory.for_profile(goodwe_profile))
    assert (decide_cycle(**kw).status, decide_cycle(**kw).reason) == (ERROR, "exception:TypeError")
    both = decide_cycle(**kw, target=RegisterTarget(reading_auto),
                        ents=EntityContext("goodwe", {}, {}, {}, {}))
    assert (both.status, both.reason) == (ERROR, "exception:TypeError")
