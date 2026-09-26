import pytest

from custom_components.volcast.core.engines.mode_setpoint import (
    NOTE_DIRECTION_WITHOUT_POWER, NOTE_OK, NOTE_POWER_WITHOUT_DIRECTION, map_slot,
)
from custom_components.volcast.core.profile import load_builtin, profile_from_dict
from custom_components.volcast.core.slot import parse_schedule
from tests.core.golden import (
    assert_params_equal, load_golden, params_from_golden, slot_from_golden,
)
from tests.core.profile_fixtures import tw_profile

NOTES = [NOTE_OK, NOTE_POWER_WITHOUT_DIRECTION, NOTE_DIRECTION_WITHOUT_POWER]
GW = load_builtin("goodwe-et")
VECTORS = load_golden("mapper")["vectors"]


@pytest.mark.parametrize("vec", VECTORS, ids=[str(i) for i in range(len(VECTORS))])
def test_matches_reference_mapper(vec):
    got = map_slot(slot_from_golden(vec["slot"]), GW, vec["rated_power_w"])
    assert_params_equal(got.params, params_from_golden(vec["params"], GW))
    assert got.note == NOTES[vec["note"]]


def test_standby_always_carries_zero_power():
    for vec in VECTORS:
        got = map_slot(slot_from_golden(vec["slot"]), GW, 8000)
        if got.params.mode == "battery_standby":
            assert got.params.power_w == 0.0
        if got.params.mode != "auto":
            assert got.params.power_w is not None, "tryb czytający Xset bez własnej nastawy"


def test_live_plan_counts_like_box():
    sch = parse_schedule(load_golden("plan_live"))
    modes = [map_slot(s, GW, 8000).params.mode for s in sch.slots]
    assert len(modes) == 14
    assert sum(m in ("sell_power", "discharge_battery") for m in modes) == 6
    assert sum(m in ("charge_battery", "charge_pv") for m in modes) == 2
    assert sum(m in ("auto", "battery_standby") for m in modes) == 6


def test_rejects_time_window_profile():
    sch = parse_schedule(load_golden("plan_live"))
    with pytest.raises(ValueError):
        map_slot(sch.slots[0], profile_from_dict(tw_profile()), 8000)
