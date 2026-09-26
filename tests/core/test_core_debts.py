"""Długi rdzenia, które wykonawca w HA musi mieć zamknięte przed pierwszym zapisem."""
from __future__ import annotations

import json
import math

import pytest

from custom_components.volcast.core.entity_map import canonical_value
from custom_components.volcast.core.guards import GuardContext, apply_guards
from custom_components.volcast.core.params import Params
from custom_components.volcast.core.profile import (PROFILES_DIR, ProfileError, load_builtin,
                                                    load_profile, profile_from_dict)
from custom_components.volcast.core.slot import Action

from .profile_fixtures import ms_profile


def _ctx(**kw):
    base = dict(soc=50.0, soc_age_s=10.0, temperature_ok=True, soc_reserve=10.0,
                action=Action.SELF_CONSUME)
    base.update(kw)
    return GuardContext(**base)


@pytest.mark.parametrize("age", [math.nan, -1.0, math.inf])
def test_state_age_nan_fails_closed(age):
    prof = profile_from_dict(ms_profile())
    r = apply_guards(Params(mode="auto"), _ctx(soc_age_s=age), prof)
    assert (r.write_allowed, r.invariant) == (False, "I-9")


@pytest.mark.parametrize("reserve", [math.nan, -1.0, 101.0])
def test_soc_reserve_out_of_range_fails_closed(reserve):
    prof = profile_from_dict(ms_profile())
    r = apply_guards(Params(mode="auto"), _ctx(soc_reserve=reserve), prof)
    assert (r.write_allowed, r.invariant) == (False, "I-10")


def test_reserve_engaged_overrides_soc_comparison():
    prof = profile_from_dict(ms_profile())
    sell = next(n for n, m in prof.modes.items() if m.direction == "discharge")
    # SoC nad rezerwą, ale zatrzask trzyma → rozładowanie zdjęte
    r = apply_guards(Params(mode=sell, power_w=2000.0), _ctx(soc=12.0, reserve_engaged=True,
                                                             action=Action.DISCHARGE), prof)
    assert r.invariant == "I-1" and r.params.mode == prof.neutral_mode and r.params.power_w is None
    assert "zatrzask" in r.note
    # SoC na rezerwie, zatrzask zwolniony → bez I-1
    r = apply_guards(Params(mode=sell, power_w=2000.0), _ctx(soc=10.0, reserve_engaged=False,
                                                             action=Action.DISCHARGE), prof)
    assert r.invariant is None and r.params.mode == sell


def test_reserve_engaged_none_keeps_soc_comparison():
    # Bez stanu zatrzasku (złote wektory) obowiązuje nieostre `soc <= rezerwa`.
    prof = profile_from_dict(ms_profile())
    sell = next(n for n, m in prof.modes.items() if m.direction == "discharge")
    r = apply_guards(Params(mode=sell, power_w=2000.0), _ctx(soc=10.0, action=Action.DISCHARGE), prof)
    assert r.invariant == "I-1" and r.params.mode == prof.neutral_mode
    r = apply_guards(Params(mode=sell, power_w=2000.0), _ctx(soc=12.0, action=Action.DISCHARGE), prof)
    assert r.invariant is None and r.params.mode == sell


def test_ref_cycle_rejected():
    raw = ms_profile()
    raw["read"]["pv_power_w"] = {"sum": [{"ref": "load_power_w"}]}
    raw["read"]["load_power_w"] = {"sum": [{"ref": "pv_power_w"}]}
    with pytest.raises(ProfileError) as exc:
        profile_from_dict(raw)
    assert "cykl" in str(exc.value)


def test_duplicate_keys_rejected(tmp_path):
    text = json.dumps(ms_profile())
    dup = text.replace('"status": ', '"status": "draft", "status": ', 1)
    path = tmp_path / f"{ms_profile()['id']}.json"
    path.write_text(dup, encoding="utf-8")
    with pytest.raises(ProfileError) as exc:
        load_profile(path)
    assert "status" in str(exc.value)


@pytest.mark.parametrize("pid", ["../goodwe-et", "GoodWe-ET", "goodwe_et", ""])
def test_builtin_id_escape_rejected(pid):
    with pytest.raises(ProfileError):
        load_builtin(pid)


def test_select_only_for_mode():
    # `Profile.raw` jest tylko do odczytu — mutowalną kopię bierzemy z pliku.
    raw = json.loads((PROFILES_DIR / "goodwe-et.json").read_text(encoding="utf-8"))
    raw["ha"]["integrations"][0]["entities"]["power_w"]["domain"] = "select"
    with pytest.raises(ProfileError):
        profile_from_dict(raw)


def test_direction_budget_at_least_one():
    raw = ms_profile()
    raw["write_policy"]["max_direction_changes_per_hour"] = 0
    with pytest.raises(ProfileError):
        profile_from_dict(raw)


@pytest.mark.parametrize("key,state,unit,expected", [
    ("battery_temp_c", "82.4", "°F", pytest.approx(28.0)),
    ("pv_power_w", "1.5", "kW", 1500.0),
    ("soc", "55", "%", 55.0),
    ("soc", "unavailable", "%", None),
    ("soc", "nan", "%", None),
    ("export_limit_w", "50", "%", None),     # % przy kluczu watowym = niezgodne
    ("pv_energy_total_kwh", "1500", "Wh", 1.5),
])
def test_canonical_value(key, state, unit, expected):
    assert canonical_value(key, state, unit) == expected


@pytest.mark.parametrize("state", [None, "unknown", "", "abc", "inf", "-inf"])
def test_canonical_value_unreadable_is_none(state):
    assert canonical_value("pv_power_w", state, "W") is None
