import copy
from dataclasses import replace

import pytest

from custom_components.volcast.core.engines.mode_setpoint import (
    NOTE_DIRECTION_WITHOUT_POWER, NOTE_OK, NOTE_POWER_WITHOUT_DIRECTION, map_slot, slot_intent,
)
from custom_components.volcast.core.profile import load_builtin, profile_from_dict
from custom_components.volcast.core.slot import Action, Slot, parse_schedule
from tests.core.golden import (
    T0, assert_params_equal, load_golden, params_from_golden, slot_from_golden,
)
from tests.core.profile_fixtures import tw_profile

NOTES = [NOTE_OK, NOTE_POWER_WITHOUT_DIRECTION, NOTE_DIRECTION_WITHOUT_POWER]
GW = load_builtin("goodwe-et")
VECTORS = load_golden("mapper")["vectors"]

_END = T0.replace(hour=11)


def _slot(**kw) -> Slot:
    return Slot(start=T0, end=_END, **kw)


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
    # Fixture musi zachować slot self_consume z KIERUNKIEM OPISOWYM (discharge_purpose="self"):
    # to jedyny sposób, żeby przetestować gałąź "auto śledzi pobór lepiej niż nastawa"
    # na żywym planie, a nie tylko na syntetycznym wektorze.
    described = [s for s in sch.slots
                 if s.action == Action.SELF_CONSUME and s.discharge_purpose == "self"]
    assert len(described) == 1, "fixture stracił slot self_consume z kierunkiem opisowym"
    got = map_slot(described[0], GW, 8000)
    assert got.params.mode == "auto"
    assert got.params.power_w is None


def test_rejects_time_window_profile():
    sch = parse_schedule(load_golden("plan_live"))
    with pytest.raises(ValueError):
        map_slot(sch.slots[0], profile_from_dict(tw_profile()), 8000)


@pytest.mark.parametrize("kw,expected", [
    # bez kierunku
    (dict(action=Action.SELF_CONSUME), ("self_consume", NOTE_OK)),
    (dict(action=Action.SELF_CONSUME, power_w=500), ("self_consume", NOTE_POWER_WITHOUT_DIRECTION)),
    (dict(action=Action.IDLE), ("standby", NOTE_OK)),
    (dict(action=Action.IDLE, power_w=500), ("standby", NOTE_POWER_WITHOUT_DIRECTION)),
    (dict(action=Action.HOLD), ("standby", NOTE_OK)),
    # ładowanie
    (dict(action=Action.SELF_CONSUME, charge_source="pv"), ("charge_pv", NOTE_OK)),
    (dict(action=Action.SELF_CONSUME, charge_source="pv", power_w=500), ("charge_pv", NOTE_OK)),
    (dict(action=Action.SELF_CONSUME, charge_source="grid", power_w=500), ("charge_grid", NOTE_OK)),
    (dict(action=Action.CHARGE, power_w=500), ("charge_grid", NOTE_OK)),
    (dict(action=Action.CHARGE), ("self_consume", NOTE_DIRECTION_WITHOUT_POWER)),
    # rozładowanie
    (dict(action=Action.SELF_CONSUME, discharge_purpose="self"), ("self_consume", NOTE_OK)),
    (dict(action=Action.SELF_CONSUME, discharge_purpose="sell", power_w=500), ("sell", NOTE_OK)),
    (dict(action=Action.DISCHARGE, power_w=500), ("discharge_forced", NOTE_OK)),
    (dict(action=Action.DISCHARGE), ("self_consume", NOTE_DIRECTION_WITHOUT_POWER)),
])
def test_slot_intent_table(kw, expected):
    # `slot_intent` jest dzielony z silnikiem okien czasowych; w tabeli GoodWe zarówno
    # `charge_pv`, jak i `self_consume` kończą na (auto, none), więc bez tego testu
    # zamiana tych dwóch intencji przeszłaby wszystkie wektory złote bez zauważenia.
    assert slot_intent(_slot(**kw)) == expected


@pytest.mark.parametrize("kw,rated_power_w,expected_power_w", [
    # ujemna moc jest przycinana do zera niezależnie od mocy znamionowej
    (dict(action=Action.CHARGE, power_w=-500.0), 8000, 0.0),
    (dict(action=Action.DISCHARGE, discharge_purpose="sell", power_w=-500.0), 8000, 0.0),
    # moc znamionowa 0 (nieznana, np. brak identyfikacji) = brak górnego przycięcia
    (dict(action=Action.CHARGE, power_w=99000.0), 0, 99000.0),
    (dict(action=Action.DISCHARGE, discharge_purpose="sell", power_w=99000.0), 0, 99000.0),
])
def test_clip_power_edge_cases(kw, rated_power_w, expected_power_w):
    got = map_slot(_slot(**kw), GW, rated_power_w)
    assert got.params.power_w == expected_power_w


def test_map_slot_rejects_slot_power_missing_defensively():
    # Walidator profilu (POWERED_INTENTS) odrzuca `"power": "slot"` na intencjach bez
    # gwarancji mocy, więc profile normalnie ładowane przez `load_builtin`/`profile_from_dict`
    # tu nigdy nie trafiają. Tor zapisu ma jednak własną obronę: symulujemy zepsuty
    # profil (obchodząc walidację) i sprawdzamy, że `map_slot` kończy jawnym ValueError,
    # a nie TypeError z `float(None)`.
    bad_raw = copy.deepcopy(dict(GW.raw))
    bad_raw["intents"]["standby"]["power"] = "slot"
    bad_profile = replace(GW, raw=bad_raw)
    with pytest.raises(ValueError, match="power_w"):
        map_slot(_slot(action=Action.IDLE), bad_profile, 8000)
