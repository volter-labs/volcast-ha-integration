"""Wybór profilu dla urządzenia z producentem, ale bez modelu (np. integracja pośrednicząca odczyt).

Dopasowanie encji bez modelu idzie przez `ha.integrations[].model_regex` na tekście „producent model”;
reszta reguł (pierwszeństwo pakietu, niejednoznaczność) bez zmian.
"""
import pytest

from custom_components.volcast.core.control.select import (InverterHint, ambiguous_profiles,
                                                           declared_by_profile, select_profile)
from custom_components.volcast.core.profile import (ORIGIN_FEED, ORIGIN_PACKAGE, load_builtin, packaged_ids,
                                                    profile_from_dict)
from tests.core.profile_fixtures import eo_profile

SA = "solar_assistant"


def _eo(pid: str, domain: str = SA, regex: str = "(?i)\\bsolar ?assistant\\b", origin: str = ORIGIN_FEED):
    raw = eo_profile()
    raw["id"] = pid
    integ = raw["ha"]["integrations"][0]
    integ["domain"], integ["model_regex"] = domain, [regex]
    return profile_from_dict(raw, origin=origin)


def test_modelless_device_matches_integration_regex_as_entity_choice():
    sa = _eo("solarassistant-generic")
    c = select_profile([InverterHint(SA, "SolarAssistant", None)], [sa])
    assert (c.profile.id, c.integration_domain, c.model) == ("solarassistant-generic", SA, None)


def test_modelless_device_with_other_manufacturer_is_not_an_entity_match():
    sa = _eo("solarassistant-generic")
    c = select_profile([InverterHint(SA, "Someone Else", None)], [sa])
    # domena pasuje → jak dotąd tylko odczyt/podgląd, bez mapowania encji
    assert (c.profile.id, c.integration_domain) == ("solarassistant-generic", None)


def test_device_with_model_still_needs_identify_regex():
    # profil „tylko encje” nie ma `identify.model_regex` — urządzenie z modelem nie daje dopasowania encji
    sa = _eo("solarassistant-generic")
    c = select_profile([InverterHint(SA, "SolarAssistant", "X1")], [sa])
    assert (c.profile.id, c.integration_domain) == ("solarassistant-generic", None)


def test_two_feed_profiles_sharing_a_domain_stay_ambiguous():
    a, b = _eo("solarassistant-a"), _eo("solarassistant-b")
    hint = InverterHint(SA, "SolarAssistant", None)
    assert select_profile([hint], [a, b]) is None
    assert ambiguous_profiles([hint], [a, b]) == {SA: ("solarassistant-a", "solarassistant-b")}


def test_packaged_profile_wins_over_two_with_one_packaged():
    a, b = _eo("solarassistant-a", origin=ORIGIN_PACKAGE), _eo("solarassistant-b")
    c = select_profile([InverterHint(SA, "SolarAssistant", None)], [a, b])
    assert (c.profile.id, c.integration_domain) == ("solarassistant-a", SA)


def test_feed_entities_only_profile_never_displaces_packaged_profile():
    # profil z feedu deklaruje domenę i wzorzec profilu z pakietu — pakiet wygrywa, feed odpada
    sofar = load_builtin("sofar-hyd")
    feed = _eo("sofar-entities", domain="solarman", regex="(?i)\\bsofar")
    hint = InverterHint("solarman", "SOFAR", None)
    c = select_profile([hint], [feed, sofar])
    assert c.profile.id == "sofar-hyd"
    assert select_profile([hint], [sofar]) == c
    assert ambiguous_profiles([hint], [feed, sofar]) == {}


def test_declared_by_profile_needs_integration_regex_match():
    sa = _eo("solarassistant-generic")
    assert declared_by_profile(InverterHint(SA, "SolarAssistant", None), [sa]) is True
    assert declared_by_profile(InverterHint(SA, "Someone Else", None), [sa]) is False
    assert declared_by_profile(InverterHint("other", "SolarAssistant", None), [sa]) is False
    assert declared_by_profile(InverterHint(SA, "SolarAssistant", None), []) is False
    # wpis ogólnej platformy bez `model_regex` (pakiet: `modbus`) nie deklaruje falownika
    sungrow = load_builtin("sungrow-sh")
    assert declared_by_profile(InverterHint("modbus", "Sungrow", "SH10RT"), [sungrow]) is False


@pytest.mark.parametrize("hint,pid", [
    (InverterHint("solax_modbus", "Sofar Solar", None), "sofar-hyd"),
    (InverterHint("solax_modbus", "Ginlong Solis", None), "solis-hybrid"),
    (InverterHint("solarman", "Solis", None), "solis-hybrid"),
    (InverterHint("solarman", "SOFAR", None), "sofar-hyd"),
])
def test_modelless_device_on_packaged_register_profile_stays_read_only(hint, pid):
    # Dopasowanie encji bez modelu tylko dla profili „tylko encje” — profile rejestrowe jak dotąd: podgląd.
    all_profiles = [load_builtin(i) for i in packaged_ids()]
    c = select_profile([hint], all_profiles)
    assert (c.profile.id, c.integration_domain, c.model) == (pid, None, None)
