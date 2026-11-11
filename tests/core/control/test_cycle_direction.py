"""Cykl na profilu z mocą wg kierunku i przełącznikiem zdalnego sterowania (konwencja planu: `power_w`)."""
from dataclasses import replace

import pytest

from custom_components.volcast.core.control.cycle import (
    BLOCKED, IDLE, WRITE, ControlMemory, EntityContext, Gates, Limits, Telemetry, commit, decide_cycle)
from custom_components.volcast.core.control.target import RegisterTarget
from custom_components.volcast.core.modbus.reading import build_reading
from custom_components.volcast.core.registers import RegisterImage
from custom_components.volcast.core.write_sequence import WriteReport
from tests.core.golden import T0
from tests.core.modbus.engine_profile import PLANT, directional_profile

from .conftest import one_slot

SELL = dict(mode="discharge", discharge_purpose="sell", power_w=2500)
CHARGE = dict(mode="charge", charge_source="grid", power_w=3000)


def _blocks(*, mode=1, enable=0, charge=0, discharge=0, soc_min=200):
    """Rejestry urządzenia (adres PLANT) w słowach: tryb, przełącznik, nastawy (u32; rozładowanie
    lo_hi ze skalą 10), próg SoC (skala 0,1); SoC na adresie połączenia."""
    d = int(discharge // 10)
    return {(3, 40029, PLANT): [enable], (3, 40031, PLANT): [mode],
            (3, 40032, PLANT): [charge >> 16, charge & 0xFFFF, d & 0xFFFF, d >> 16],
            (3, 40046, PLANT): [soc_min], 37007: [80]}


def _apply(blocks, writes):
    out = dict(blocks)
    for w in writes:
        if w.unit_id is None:
            out[w.addr] = w.words()
            continue
        words = w.words()
        for key in [k for k in out if isinstance(k, tuple) and k[2] == w.unit_id]:
            base = key[1]
            vals = list(out[key])
            for i, word in enumerate(words):
                if base <= w.addr + i < base + len(vals):
                    vals[w.addr + i - base] = word
            out[key] = vals
    return out


def _gates(kind="direct"):
    return Gates(consent=True, local_switch=True, control_mode=kind, verified=True)


def _tele():
    return Telemetry(soc=80.0, soc_age_s=5.0, battery_temp_c=25.0, pv_power_w=0.0, pv_age_s=5.0,
                     load_power_w=0.0, load_age_s=5.0)


def _run(profile, slot, blocks, *, memory=None, now=1000.0, unavailable=frozenset(), owner=None):
    memory = memory or ControlMemory.for_profile(profile)
    reading = build_reading(profile, RegisterImage.from_blocks(blocks), at_mono=now, at_utc=T0)
    target = RegisterTarget(reading, unavailable=unavailable)
    target.owner_values = owner or {}
    d = decide_cycle(profile=profile, schedule=one_slot(**slot), now_utc=T0, now_mono=now, tele=_tele(),
                     limits=Limits(rated_power_w=8000.0), gates=_gates(), memory=memory, target=target)
    return d, memory


def _ok(d):
    return WriteReport(written=[w.key for w in d.writes])


def test_takeover_writes_set_point_then_switch_then_mode():
    p = directional_profile()
    d, _ = _run(p, SELL, _blocks())
    assert d.status == WRITE
    keys = [w.key for w in d.writes]
    assert keys.index("discharge_power_w") < keys.index("enable") < keys.index("mode")
    assert "charge_power_w" not in keys
    sp = next(w for w in d.writes if w.key == "discharge_power_w")
    en = next(w for w in d.writes if w.key == "enable")
    assert sp.value == 250 and en.turns_on is True


def test_unchanged_plan_sends_nothing_on_the_second_cycle():
    p = directional_profile()
    d, memory = _run(p, SELL, _blocks())
    commit(d, _ok(d), memory, 1000.0)
    after = _apply(_blocks(), d.writes)
    again, _ = _run(p, SELL, after, memory=memory, now=1600.0)
    assert (again.status, again.reason) == (IDLE, "nothing_to_write"), [w.key for w in again.writes]


def test_restart_with_settled_device_writes_nothing():
    p = directional_profile()
    d, _ = _run(p, SELL, _blocks(mode=p.mode_value("sell_power"), enable=1, discharge=2500))
    assert (d.status, d.reason) == (IDLE, "nothing_to_write")


def test_scaled_set_point_settles_after_one_write():
    p = directional_profile()
    slot = dict(SELL, power_w=2505)
    d, memory = _run(p, slot, _blocks())
    assert next(w for w in d.writes if w.key == "discharge_power_w").value == 250
    commit(d, _ok(d), memory, 1000.0)
    again, _ = _run(p, slot, _apply(_blocks(), d.writes), memory=memory, now=1600.0)
    assert (again.status, again.reason) == (IDLE, "nothing_to_write")


def test_commit_records_plan_power_for_the_directional_write():
    p = directional_profile()
    d, memory = _run(p, SELL, _blocks())
    commit(d, _ok(d), memory, 1000.0)
    assert memory.last_written["power_w"] == 2500.0 and "discharge_power_w" not in memory.last_written
    assert memory.throttle.known("power_w") == 2500.0


def test_ambiguous_directional_write_marks_plan_power_uncertain():
    p = directional_profile()
    d, memory = _run(p, SELL, _blocks())
    report = replace(_ok(d), written=[k for k in _ok(d).written if k != "discharge_power_w"],
                     failed=["discharge_power_w"])
    commit(d, report, memory, 1000.0)
    assert "power_w" in memory.uncertain and "discharge_power_w" not in memory.uncertain


def test_direction_change_compares_with_the_new_direction_set_point():
    p = directional_profile()
    # urządzenie: rozładowanie 2500 W; plan: ładowanie 3000 W — nastawa ładowania (0) musi pójść
    d, _ = _run(p, CHARGE, _blocks(mode=p.mode_value("sell_power"), enable=1, discharge=2500))
    by_key = {w.key: w for w in d.writes}
    assert by_key["charge_power_w"].value == 3000 and "discharge_power_w" not in by_key
    assert by_key["mode"].value == p.mode_value("charge_battery")


def test_unsupported_set_point_blocks_only_its_direction():
    p = directional_profile()
    memory = ControlMemory.for_profile(p)
    memory.unsupported = {"discharge_power_w"}
    d, _ = _run(p, SELL, _blocks(), memory=memory)
    assert (d.status, d.reason) == (BLOCKED, "power_unsupported")
    memory = ControlMemory.for_profile(p)
    memory.unsupported = {"discharge_power_w"}
    d, _ = _run(p, CHARGE, _blocks(), memory=memory)
    assert d.status == WRITE and "charge_power_w" in [w.key for w in d.writes]


def test_missing_set_point_degrades_its_direction_to_neutral():
    p = directional_profile()
    d, _ = _run(p, SELL, _blocks(), unavailable=frozenset({"discharge_power_w"}))
    keys = [w.key for w in d.writes]
    assert "discharge_power_w" not in keys and "degraded" in d.notes
    assert d.flat["mode"] == "auto" and "power_w" not in d.flat


def test_unsupported_switch_blocks_the_whole_cycle():
    p = directional_profile()
    memory = ControlMemory.for_profile(p)
    memory.unsupported = {"enable"}
    d, _ = _run(p, SELL, _blocks(), memory=memory)
    assert (d.status, d.reason, d.writes) == (BLOCKED, "enable_unsupported", [])
    assert "enable" in d.dropped_unsupported


def test_unsupported_switch_already_on_does_not_block():
    p = directional_profile()
    memory = ControlMemory.for_profile(p)
    memory.unsupported = {"enable"}
    d, _ = _run(p, SELL, _blocks(enable=1), memory=memory)
    assert d.status == WRITE and "mode" in [w.key for w in d.writes]


def test_neutral_slot_writes_no_set_point_and_keeps_switch_on():
    p = directional_profile()
    d, _ = _run(p, dict(mode="self_consume"), _blocks(mode=p.mode_value("sell_power"), enable=1,
                                                       discharge=2500))
    keys = [w.key for w in d.writes]
    assert keys == ["mode"] and "power_w" not in d.flat and d.flat["enable"] == 1.0


def test_profile_without_switch_gets_no_enable():
    from custom_components.volcast.core.profile import load_builtin
    from .conftest import goodwe_reading, MODE_REG, SOC_REG
    gw = load_builtin("goodwe-et")
    reading = goodwe_reading(gw, **{str(MODE_REG): 1, str(SOC_REG): 80})
    d = decide_cycle(profile=gw, schedule=one_slot(**SELL), now_utc=T0, now_mono=1000.0, tele=_tele(),
                     limits=Limits(rated_power_w=8000.0), gates=_gates(), memory=ControlMemory.for_profile(gw),
                     target=RegisterTarget(reading))
    assert "enable" not in d.flat and d.aliases == {}


# ── tryb encji ──────────────────────────────────────────────────────────────

_MAPPED = {"mode": "select.ems", "enable": "switch.remote", "charge_power_w": "number.charge",
           "discharge_power_w": "number.discharge", "soc_min": "number.dod"}


def _entity_profile(extra=None):
    def mutate(raw):
        raw["ha"]["integrations"][0]["entities"]["mode"] = {"domain": "select", "unique_id_regex": "^x-ems-"}
        if extra is not None:
            extra(raw)
    return directional_profile(mutate)


def _ents(readings, mapped=_MAPPED, discharge_max=10.0):
    return EntityContext(
        domain="goodwe", mapped=mapped,
        units={"charge_power_w": "kW", "discharge_power_w": "kW", "soc_min": "%"},
        attrs={"select.ems": {"options": ["auto", "charge_battery", "sell_power", "discharge_battery",
                                          "battery_standby"]},
               "number.charge": {"min": 0, "max": 10, "step": 0.01},
               "number.discharge": {"min": 0.5, "max": discharge_max, "step": 0.01},
               "number.dod": {"min": 0, "max": 100, "step": 1}},
        readings=readings)


def _entity_run(profile, slot, ents):
    return decide_cycle(profile=profile, schedule=one_slot(**slot), now_utc=T0, now_mono=1000.0, tele=_tele(),
                        limits=Limits(rated_power_w=8000.0), gates=_gates("entities"),
                        memory=ControlMemory.for_profile(profile), ents=ents)


def test_entity_path_needs_the_switch_entity():
    p = _entity_profile()
    mapped = {k: v for k, v in _MAPPED.items() if k != "enable"}
    d = _entity_run(p, SELL, _ents({"mode": "auto"}, mapped=mapped))
    assert (d.status, d.reason) == (IDLE, "missing_entities") and "enable" in d.unmapped


def test_entity_path_settled_set_point_in_kw_is_not_rewritten():
    p = _entity_profile()
    d = _entity_run(p, SELL, _ents({"mode": "sell_power", "enable": "on", "discharge_power_w": 2500.0,
                                    "soc_min": 80.0}))
    assert "discharge_power_w" not in [w.key for w in d.writes], d.reason


def test_entity_path_takeover_writes_kw_set_point_switch_then_mode():
    p = _entity_profile()
    d = _entity_run(p, SELL, _ents({"mode": "auto", "enable": "off"}))
    assert d.status == WRITE, d.reason
    keys = [w.key for w in d.writes]
    assert keys.index("discharge_power_w") < keys.index("enable") < keys.index("mode")
    assert next(w for w in d.writes if w.key == "discharge_power_w").data["value"] == 2.5


@pytest.fixture
def live_sell():
    def mutate(raw):
        raw["intents"]["sell"]["power"] = "slot_live_export"
    return _entity_profile(mutate)


def test_entity_live_export_sell_uses_the_discharge_entity(live_sell):
    d = _entity_run(live_sell, SELL, _ents({"mode": "auto", "enable": "off"}))
    assert d.status == WRITE, d.reason
    w = next((w for w in d.writes if w.key == "discharge_power_w"), None)
    assert w is not None, (d.notes, d.flat, [w.key for w in d.writes], d.unmapped)
    assert w.data["value"] == 2.5


def test_entity_live_export_below_the_discharge_minimum_goes_neutral(live_sell):
    d = _entity_run(live_sell, dict(SELL, power_w=300), _ents({"mode": "auto", "enable": "off"}))
    assert "sell_below_min" in d.notes and "discharge_power_w" not in [w.key for w in d.writes]


# ── tryb neutralny i postój: nastawy właściciela ─────────────────────────────

OWNER_SP = {"charge_power_w": 5000.0, "discharge_power_w": 6000.0}
NEUTRAL = dict(mode="self_consume")


def test_neutral_slot_returns_both_set_points_to_the_owner_values():
    p = directional_profile()
    d, _ = _run(p, NEUTRAL, _blocks(mode=p.mode_value("sell_power"), enable=1), owner=OWNER_SP)
    by_key = {w.key: w for w in d.writes}
    assert by_key["charge_power_w"].value == 5000 and by_key["discharge_power_w"].value == 600
    keys = [w.key for w in d.writes]
    assert max(keys.index("charge_power_w"), keys.index("discharge_power_w")) < keys.index("mode")
    assert d.flat["charge_power_w"] == 5000.0 and "power_w" not in d.flat and d.aliases == {}


def test_idle_slot_returns_owner_set_points_and_settles():
    p = directional_profile()
    standby = dict(mode="idle")
    d, _ = _run(p, standby, _blocks(enable=1, charge=5000, discharge=6000), owner=OWNER_SP)
    assert not {"charge_power_w", "discharge_power_w"} & {w.key for w in d.writes}
    assert d.flat["discharge_power_w"] == 6000.0


def test_missing_owner_value_is_never_invented():
    p = directional_profile()
    d, _ = _run(p, NEUTRAL, _blocks(mode=p.mode_value("sell_power"), enable=1),
                owner={"charge_power_w": 5000.0})
    keys = [w.key for w in d.writes]
    assert "charge_power_w" in keys and "discharge_power_w" not in keys and "discharge_power_w" not in d.flat


def test_directional_slot_keeps_plan_power_not_owner_values():
    p = directional_profile()
    d, _ = _run(p, SELL, _blocks(), owner=OWNER_SP)
    keys = [w.key for w in d.writes]
    assert "charge_power_w" not in keys and d.flat["power_w"] == 2500.0 and "charge_power_w" not in d.flat


# ── ogranicznik eksportu właściciela „bez limitu” ───────────────────────────


def _u32_export():
    def mutate(raw):
        raw["write"]["export_limit_w"] = {"addr": 40038, "type": "u32", "encode": "watts", "unit_id": PLANT}
    return directional_profile(mutate)


def _export_blocks(limit_words, enabled=1):
    return {**_blocks(mode=1, enable=1), (3, 40038, PLANT): list(limit_words), 47509: [enabled]}


def test_owner_no_limit_export_restored_exactly_without_blocking_control():
    p = _u32_export()
    owner = {"export_limit_enabled": 1.0, "export_limit_w": 4294967295.0}
    d, _ = _run(p, NEUTRAL, _export_blocks([0, 0]), owner=owner)      # nasz zakaz eksportu (0 W) na urządzeniu
    assert d.status == WRITE, d.reason
    w = next(w for w in d.writes if w.key == "export_limit_w")
    assert w.value == 0xFFFFFFFF and w.words() == [0xFFFF, 0xFFFF]
    assert d.guard.params.export_limit_w is None                       # strażnik tej wartości nie oceniał


def test_owner_value_at_register_maximum_counts_as_no_limit():
    def mutate(raw):
        raw["write"]["export_limit_w"] = {"addr": 40038, "type": "u16", "encode": "watts", "scale": 0.1,
                                          "unit_id": PLANT}
    p = directional_profile(mutate)
    owner = {"export_limit_enabled": 1.0, "export_limit_w": 6553.5}
    d, _ = _run(p, NEUTRAL, {**_blocks(mode=1, enable=1), (3, 40038, PLANT): [0], 47509: [1]}, owner=owner)
    assert d.status == WRITE and d.guard.params.export_limit_w is None
    assert next(w for w in d.writes if w.key == "export_limit_w").value == 0xFFFF


def test_real_owner_export_value_still_goes_through_the_guards():
    p = _u32_export()
    owner = {"export_limit_enabled": 1.0, "export_limit_w": 25000.0}
    d, _ = _run(p, NEUTRAL, _export_blocks([0, 0]), owner=owner)
    assert d.status == WRITE and d.guard.params.export_limit_w == 25000.0
    assert next(w for w in d.writes if w.key == "export_limit_w").value == 25000


def test_plan_export_limit_above_the_sanity_bound_is_still_rejected():
    p = _u32_export()
    d, _ = _run(p, dict(NEUTRAL, export_limit_w=40000), _export_blocks([0, 0]))
    assert (d.status, d.reason) == (BLOCKED, "guard:I-10")
