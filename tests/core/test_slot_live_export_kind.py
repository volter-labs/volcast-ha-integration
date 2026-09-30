"""Rodzaj mocy `slot_live_export`: dla mappera i zdolności = `slot`, do odróżnienia dla wykonawcy."""
import pytest

from custom_components.volcast.core.control.caps import capabilities_for
from custom_components.volcast.core.engines.mode_setpoint import map_slot
from custom_components.volcast.core.profile import PROFILES_DIR, load_builtin, profile_from_dict
from custom_components.volcast.core.profile_schema import validate_profile
from custom_components.volcast.core.slot import Action, Slot
from tests.core.golden import T0
from tests.core.profile_fixtures import ms_profile

LIVE = "slot_live_export"
ALL = ("mode", "power_w", "soc_min", "soc_max", "export_limit_w", "export_limit_enabled")


def _with_sell(kind):
    p = ms_profile()
    p["intents"]["sell"]["power"] = kind
    return p


def test_schema_accepts_live_export_on_powered_intent():
    assert validate_profile(_with_sell(LIVE)) == []


@pytest.mark.parametrize("intent", ["standby", "self_consume", "charge_pv"])
def test_schema_rejects_live_export_on_unpowered_intent(intent):
    p = ms_profile()
    p["intents"][intent]["power"] = LIVE
    assert any(f"$.intents.{intent}.power" in e for e in validate_profile(p))


def test_schema_rejects_unknown_kind():
    errs = "\n".join(validate_profile(_with_sell("slot_live")))
    assert "$.intents.sell.power" in errs


def test_mapper_treats_live_export_like_slot():
    live = profile_from_dict(_with_sell(LIVE))
    plain = profile_from_dict(_with_sell("slot"))
    for power in (3000.0, 20000.0, 0.0):
        s = Slot(start=T0, end=T0.replace(hour=11), action=Action.DISCHARGE,
                 discharge_purpose="sell", power_w=power)
        assert map_slot(s, live, 8000) == map_slot(s, plain, 8000)


def test_caps_treat_live_export_like_slot():
    live = profile_from_dict(_with_sell(LIVE))
    plain = profile_from_dict(_with_sell("slot"))
    assert capabilities_for(live, ALL) == capabilities_for(plain, ALL)
    no_power = [k for k in ALL if k != "power_w"]
    assert capabilities_for(live, no_power) == capabilities_for(plain, no_power)
    assert capabilities_for(live, no_power)["sell_from_battery"] is False


def test_goodwe_sell_reports_live_export_others_unchanged():
    gw = load_builtin("goodwe-et")
    assert gw.power_kind("sell") == LIVE
    assert gw.power_kind("charge_grid") == "slot"
    assert gw.power_kind("discharge_forced") == "slot"
    assert gw.power_kind("standby") == "zero"
    assert gw.power_kind("self_consume") == "none"


def test_power_kind_none_for_unsupported_intent():
    assert load_builtin("deye-sg").power_kind("sell") is None


def test_only_goodwe_uses_live_export():
    users = [f.name for f in PROFILES_DIR.glob("*.json") if LIVE in f.read_text(encoding="utf-8")]
    assert users == ["goodwe-et.json"]
