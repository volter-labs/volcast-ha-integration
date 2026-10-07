import json

from custom_components.volcast.core.control.select import (InverterHint, control_verified,
                                                           select_profile)
from custom_components.volcast.core.profile import PROFILES_DIR, load_builtin, profile_from_dict

PROFILES = [load_builtin("deye-sg"), load_builtin("goodwe-et")]


def test_domain_match_gives_entity_capable_choice():
    c = select_profile([InverterHint("goodwe", "GoodWe", "GW8KN-ET")], PROFILES)
    assert (c.profile.id, c.integration_domain, c.model) == ("goodwe-et", "goodwe", "GW8KN-ET")


def test_model_outside_profile_regex_is_read_only():
    c = select_profile([InverterHint("goodwe", "GoodWe", "GW5048D-ES")], PROFILES)
    assert (c.profile.id, c.integration_domain) == ("goodwe-et", None)


def test_manufacturer_match_is_read_only():
    c = select_profile([InverterHint("solarman", "Deye", "SUN-10K-SG04LP3-EU")], PROFILES)
    assert (c.profile.id, c.integration_domain) == ("deye-sg", None)


def test_nothing_known_is_none():
    assert select_profile([InverterHint("huawei_solar", "Huawei", "SUN2000")], PROFILES) is None
    assert select_profile([], PROFILES) is None


def test_profile_without_model_regex_is_read_only_not_error():
    raw = json.loads((PROFILES_DIR / "deye-sg.json").read_text(encoding="utf-8"))
    raw["ha"]["integrations"].append({"domain": "solarman", "ems": False, "status": "draft",
                                      "entities": {"soc": {"domain": "sensor",
                                                           "unique_id_regex": "^solarman-soc"}}})
    deye = profile_from_dict(raw)
    c = select_profile([InverterHint("solarman", "Solarman", "SUN-10K-SG04LP3-EU")], [deye])
    assert (c.profile.id, c.integration_domain) == ("deye-sg", None)


def test_control_verified_needs_profile_and_integration():
    gw = load_builtin("goodwe-et")
    assert control_verified(gw, None) is False
    assert control_verified(gw, "goodwe") is True
    assert control_verified(gw, "unknown_domain") is False
    assert control_verified(gw, "solarman") is False
    assert control_verified(load_builtin("deye-sg"), "solarman") is False
