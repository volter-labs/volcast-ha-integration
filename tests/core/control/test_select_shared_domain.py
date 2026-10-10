"""Wybór profilu, gdy kilka profili opisuje tę samą domenę integracji HA."""
import pytest

from custom_components.volcast.core.control.select import (InverterHint, ambiguous_profiles,
                                                           select_profile)
from custom_components.volcast.core.profile import builtin_ids, load_builtin

ALL = [load_builtin(i) for i in builtin_ids()]


@pytest.mark.parametrize("hint,pid", [
    (InverterHint("solax_modbus", None, "X3-Hybrid"), "solax-x-hybrid"),
    (InverterHint("solax_modbus", "SolaX Power", "X3-Hybrid-G4"), "solax-x-hybrid"),
    (InverterHint("solax_modbus", "Ginlong Solis", None), "solis-hybrid"),
    (InverterHint("solax_modbus", "Sofar Solar", None), "sofar-hyd"),
    (InverterHint("solarman", "Solis", None), "solis-hybrid"),
    (InverterHint("solarman", "Ginlong", "S6-EH3P10K-H"), "solis-hybrid"),
    (InverterHint("solarman", None, "Solis S6-EH1P"), "solis-hybrid"),
    (InverterHint("solarman", "Sofar", "HYD 6000-EP"), "sofar-hyd"),
    (InverterHint("solarman", "Deye", "SUN-10K-SG04LP3-EU"), "deye-sg"),
])
def test_shared_domain_disambiguated_by_device_text(hint, pid):
    c = select_profile([hint], ALL)
    assert c is not None and c.profile.id == pid
    assert ambiguous_profiles([hint], ALL) == {}


@pytest.mark.parametrize("hint", [
    InverterHint("solarman", None, None),
    InverterHint("solarman", "Solarman", None),
    InverterHint("solax_modbus", None, None),
])
def test_shared_domain_without_device_text_selects_nothing(hint):
    assert select_profile([hint], ALL) is None
    amb = ambiguous_profiles([hint], ALL)
    assert set(amb) == {hint.domain} and len(amb[hint.domain]) >= 2 and "sofar-hyd" in amb[hint.domain]


def test_ambiguous_hint_does_not_block_a_clear_one():
    hints = [InverterHint("solarman", None, None), InverterHint("goodwe", "GoodWe", "GW8KN-ET")]
    c = select_profile(hints, ALL)
    assert (c.profile.id, c.integration_domain) == ("goodwe-et", "goodwe")
    assert ambiguous_profiles(hints, ALL) == {"solarman": ("sofar-hyd", "solis-hybrid")}


def test_goodwe_selection_unchanged_with_all_profiles():
    c = select_profile([InverterHint("goodwe", "GoodWe", "GW8KN-ET")], ALL)
    assert (c.profile.id, c.integration_domain, c.model) == ("goodwe-et", "goodwe", "GW8KN-ET")
    c = select_profile([InverterHint("goodwe", "GoodWe", "GW5048D-ES")], ALL)
    assert (c.profile.id, c.integration_domain) == ("goodwe-et", None)


def test_sofar_reached_by_its_integration_model_regex_alone():
    # Producent bez marki z id profilu i model spoza identify.model_regex: rozstrzyga tylko `ha` model_regex.
    hint = InverterHint("solax_modbus", None, "HYD 10KTL-3PH")
    assert select_profile([hint], ALL).profile.id == "sofar-hyd"


@pytest.mark.parametrize("hints", [
    [InverterHint("huawei_solar", "Huawei", "SUN2000-10KTL-M1"), InverterHint("goodwe", "GoodWe", "GW8KN-ET")],
    [InverterHint("goodwe", "GoodWe", "GW8KN-ET"), InverterHint("huawei_solar", "Huawei", "SUN2000-10KTL-M1")],
])
def test_verified_match_wins_over_draft_in_any_hint_order(hints):
    c = select_profile(hints, ALL)
    assert (c.profile.id, c.integration_domain) == ("goodwe-et", "goodwe")


def test_verified_read_only_match_wins_over_draft_entity_match():
    hints = [InverterHint("huawei_solar", "Huawei", "SUN2000-10KTL-M1"), InverterHint("goodwe", "GoodWe", "GW5048D-ES")]
    c = select_profile(hints, ALL)
    assert (c.profile.id, c.integration_domain) == ("goodwe-et", None)


@pytest.mark.parametrize("hints,pid", [
    ([InverterHint("huawei_solar", "Huawei", "SUN2000-10KTL-M1"), InverterHint("solax_modbus", "SolaX Power", "X3-Hybrid")],
     "huawei-sun2000"),
    ([InverterHint("solax_modbus", "SolaX Power", "X3-Hybrid"), InverterHint("huawei_solar", "Huawei", "SUN2000-10KTL-M1")],
     "huawei-sun2000"),
])
def test_drafts_only_household_keeps_entity_match_then_hint_order(hints, pid):
    # Same drafty: dopasowanie z integracją przed dopasowaniem tylko do odczytu, potem kolejność wskazówek.
    assert select_profile(hints, ALL).profile.id == pid
