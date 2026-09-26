import json

import pytest

from custom_components.volcast.core.guards import (
    STATUS_DEGRADED, STATUS_OK, STATUS_PARTIAL, GuardContext, apply_guards, temperature_ok,
)
from custom_components.volcast.core.params import Params, TouProgram
from custom_components.volcast.core.profile import ProfileError, load_builtin, profile_from_dict
from custom_components.volcast.core.slot import Action
from tests.core.golden import assert_params_equal, load_golden, params_from_golden
from tests.core.profile_fixtures import tw_profile

GW = load_builtin("goodwe-et")
STATUS = [STATUS_OK, STATUS_PARTIAL, STATUS_DEGRADED]
VECTORS = load_golden("guards")["vectors"]


def _ctx(raw: dict, action: str) -> GuardContext:
    return GuardContext(
        soc=raw["soc"] if raw["has_soc"] else None, soc_age_s=raw["soc_age_s"],
        temperature_ok=raw["temperature_ok"], soc_reserve=raw["soc_reserve"],
        action=Action(action), price_pln_kwh=raw["price_pln_kwh"],
        max_charge_w=raw["max_charge_w"], max_export_w=raw["max_export_w"],
        max_state_age_s=raw["max_state_age_s"])


@pytest.mark.parametrize("vec", VECTORS, ids=[str(i) for i in range(len(VECTORS))])
def test_matches_reference_guards(vec):
    got = apply_guards(params_from_golden(vec["params"], GW), _ctx(vec["ctx"], vec["action"]), GW)
    assert (got.status, got.write_allowed, got.invariant) == (
        STATUS[vec["status"]], vec["write_allowed"], vec["invariant"])
    if got.write_allowed:
        assert_params_equal(got.params, params_from_golden(vec["params_out"], GW))
    else:
        assert got.params == Params()


def _ok(**kw):
    base = dict(soc=50.0, soc_age_s=10.0, temperature_ok=True, soc_reserve=10.0, action=Action.SELF_CONSUME)
    base.update(kw)
    return GuardContext(**base)


def test_i7_backup_rejects_lowering_floor():
    r = apply_guards(Params(mode="auto", soc_min=5.0), _ok(backup_mode=True), GW)
    assert (r.write_allowed, r.invariant) == (False, "I-7")


def test_i9_soc_rate_rejects_impossible_jump_but_accepts_after_gap():
    bad = apply_guards(Params(mode="auto"), _ok(previous_soc=10.0, previous_soc_gap_s=60.0), GW)
    assert (bad.write_allowed, bad.invariant) == (False, "I-9")
    good = apply_guards(Params(mode="auto"), _ok(previous_soc=10.0, previous_soc_gap_s=1200.0), GW)
    assert good.write_allowed


def test_temperature_window_from_profile():
    assert temperature_ok(None, GW) is True
    assert temperature_ok(31.0, GW) is True
    assert temperature_ok(-10.0, GW) is False and temperature_ok(55.0, GW) is False


TW = profile_from_dict(tw_profile())


def _tou(*progs):
    return Params(tou=tuple(TouProgram(*p) for p in progs))


def test_tou_i1_raises_program_floor_to_reserve():
    r = apply_guards(_tou((0, 5000.0, 5.0, False), (600, 3000.0, 90.0, True)), _ok(soc=8.0), TW)
    assert r.status == STATUS_PARTIAL and r.invariant == "I-1"
    assert [p.soc for p in r.params.tou] == [10.0, 90.0]


def test_tou_i1_raises_program_floor_even_when_soc_is_high():
    r = apply_guards(_tou((0, 1000.0, 5.0, False)), _ok(soc=50.0), TW)
    assert r.status == STATUS_PARTIAL and r.invariant == "I-1"
    assert r.params.tou[0].soc == 10.0


def test_tou_i3_clips_power_and_i4_is_skipped_without_export_register():
    r = apply_guards(_tou((0, 9000.0, 20.0, True)), _ok(max_charge_w=5000.0, price_pln_kwh=-0.2), TW)
    assert r.params.tou[0].power_w == 5000.0 and r.params.export_limit_w is None


@pytest.mark.parametrize("progs", [
    ((0, 1000.0, 120.0, False),), ((0, -1.0, 20.0, False),), ((1440, 1000.0, 20.0, False),),
    ((600, 1000.0, 20.0, False), (0, 1000.0, 20.0, False)), ((0, 1000.0, 20.0, False), (3, 1.0, 20.0, False)),
])
def test_tou_i10_rejects_whole_program(progs):
    r = apply_guards(_tou(*progs), _ok(), TW)
    assert (r.write_allowed, r.invariant) == (False, "I-10")


def test_i1_never_yields_discharge_below_reserve_even_with_discharge_neutral_mode():
    # Profil z trybem neutralnym = rozładowanie nie może przejść walidacji…
    raw = json.loads(json.dumps(dict(GW.raw)))
    raw["neutral_mode"] = "discharge_battery"
    with pytest.raises(ProfileError, match=r"\$\.neutral_mode"):
        profile_from_dict(raw)
    # …a z profilem wbudowanym I-1 przy SoC pod rezerwą nie zostawia rozładowania.
    r = apply_guards(Params(mode="sell_power", power_w=3000.0),
                     _ok(soc=5.0, soc_reserve=10.0, action=Action.DISCHARGE), GW)
    assert r.write_allowed and r.invariant == "I-1"
    assert GW.mode_direction(r.params.mode) in ("neutral", "idle")
    assert r.params.power_w is None
