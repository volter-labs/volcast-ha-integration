"""Wybór profilu dla urządzenia z producentem, ale bez modelu (np. integracja pośrednicząca odczyt).

Dopasowanie encji bez modelu idzie przez `ha.integrations[].model_regex` na tekście „producent model”;
reszta reguł (pierwszeństwo pakietu, niejednoznaczność) bez zmian.
"""
import copy
import dataclasses

import pytest

from custom_components.volcast.core.control.select import (InverterHint, ambiguous_profiles,
                                                           declared_by_profile, select_profile)
from custom_components.volcast.core.profile import (ORIGIN_FEED, ORIGIN_PACKAGE, load_builtin, packaged_ids,
                                                    profile_from_dict)
from custom_components.volcast.core.profile_schema import GENERIC_HA_PLATFORMS
from custom_components.volcast.core.regex_safety import INPUT_CAP
from tests.core.profile_fixtures import eo_profile, ms_profile

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


def test_entities_only_device_with_model_matches_by_integration_pattern():
    # Profil „tylko encje” nie ma `identify.model_regex`: dopasowanie przez wzorzec integracji na „producent
    # model” także wtedy, gdy integracja zacznie podawać model.
    sa = _eo("solarassistant-generic")
    c = select_profile([InverterHint(SA, "SolarAssistant", "X")], [sa])
    assert (c.profile.id, c.integration_domain, c.model) == ("solarassistant-generic", SA, "X")
    # wzorzec nie pasuje do tekstu urządzenia → jak dotąd tylko podgląd po domenie
    c = select_profile([InverterHint(SA, "Someone Else", "X")], [sa])
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


def test_register_profile_never_declares_a_domain():
    # Feed z profilem rejestrowym opisującym nieznaną domenę ze wzorcem: domena NIE staje się falownikiem.
    raw = ms_profile()
    raw["id"] = "acme-hybrid"
    raw["ha"]["integrations"][0].update(domain="acme_cloud", model_regex=["(?i)\\bacme\\b"])
    acme = profile_from_dict(raw)
    assert declared_by_profile(InverterHint("acme_cloud", "ACME", None), [acme]) is False


@pytest.mark.parametrize("domain", sorted(GENERIC_HA_PLATFORMS))
def test_generic_platform_never_declared_even_by_an_entities_only_profile(domain):
    # Schemat odrzuca taki profil; wybór i tak go nie uzna (obrona niezależna od walidacji).
    sa = _eo("solarassistant-generic")
    raw = copy.deepcopy(dict(sa.raw))
    raw["ha"] = {"integrations": [{**sa.raw["ha"]["integrations"][0], "domain": domain}]}
    forged = dataclasses.replace(sa, raw=raw)
    assert declared_by_profile(InverterHint(domain, "SolarAssistant", None), [forged]) is False


def test_manufacturer_over_the_input_cap_declares_nothing():
    sa = _eo("solarassistant-generic")
    hint = InverterHint(SA, "SolarAssistant " + "x" * INPUT_CAP, None)
    assert declared_by_profile(hint, [sa]) is False
    assert select_profile([hint], [sa]) is None


def test_entities_only_choice_beats_a_packaged_read_only_choice_on_another_device():
    # Wybór tylko do odczytu nie niesie odczytów; wybór „tylko encje” niesie — wygrywa z nim.
    sa = _eo("solarassistant-generic")
    sofar = load_builtin("sofar-hyd")
    hints = [InverterHint("solarman", "SOFAR", None), InverterHint(SA, "SolarAssistant", None)]
    assert select_profile(hints[:1], [sa, sofar]).integration_domain is None
    c = select_profile(hints, [sa, sofar])
    assert (c.profile.id, c.integration_domain) == ("solarassistant-generic", SA)


def test_packaged_entity_capable_draft_beats_entities_only_choice():
    sa = _eo("solarassistant-generic")
    sofar = load_builtin("sofar-hyd")
    hints = [InverterHint(SA, "SolarAssistant", None), InverterHint("solarman", "Sofar", "SP1ES120N6")]
    c = select_profile(hints, [sa, sofar])
    assert (c.profile.id, c.integration_domain) == ("sofar-hyd", "solarman")


def test_verified_packaged_profile_beats_entities_only_choice():
    sa = _eo("solarassistant-generic")
    gw = load_builtin("goodwe-et")
    hints = [InverterHint(SA, "SolarAssistant", None), InverterHint("goodwe", "GoodWe", "GW10K-ET")]
    c = select_profile(hints, [sa, gw])
    assert (c.profile.id, c.integration_domain) == ("goodwe-et", "goodwe")


@pytest.mark.parametrize("status", ["draft", "verified"])
def test_entities_only_choice_never_outranks_a_choice_with_integration_or_verified(status):
    # Profil „tylko encje” nawet ze statusem `verified` (z pakietu) nie wyprzedza wyboru z integracją
    # ani profilu zweryfikowanego tylko do odczytu.
    raw = eo_profile()
    raw["id"], raw["status"] = "solarassistant-generic", status
    raw["ha"]["integrations"][0].update(domain=SA, model_regex=["(?i)\\bsolar ?assistant\\b"], status=status)
    sa = profile_from_dict(raw, origin=ORIGIN_PACKAGE)
    sofar = load_builtin("sofar-hyd")
    c = select_profile([InverterHint(SA, "SolarAssistant", None), InverterHint("solarman", "Sofar", "SP1ES120N6")],
                       [sa, sofar])
    assert c.profile.id == "sofar-hyd"
    gw = load_builtin("goodwe-et")          # `verified`; model spoza `identify.model_regex` → tylko podgląd
    c = select_profile([InverterHint(SA, "SolarAssistant", None), InverterHint("goodwe", "GoodWe", "GW5048D-ES")],
                       [sa, gw])
    assert (c.profile.id, c.integration_domain) == ("goodwe-et", None)
