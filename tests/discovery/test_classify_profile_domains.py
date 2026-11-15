"""Domeny falowników z danych: wpis `ha.integrations[]` załadowanego profilu z `model_regex` pasującym do
„producent model” urządzenia czyni domenę wpisu konfiguracji falownikiem. Ogólna platforma bez wzorca
(np. `modbus` w profilu z pakietu) — jak dotąd: nic po domenie, tylko ścieżka producenta."""
from custom_components.volcast.core.control.select import InverterHint
from custom_components.volcast.core.discovery.classify import classify, is_inverter
from custom_components.volcast.core.discovery.known import INVERTER_DOMAINS
from custom_components.volcast.core.discovery.models import ConfigEntrySnap, DeviceSnap, EntitySnap
from custom_components.volcast.core.profile import load_builtin, packaged_ids, profile_from_dict
from tests.core.profile_fixtures import eo_profile

SA = "solar_assistant"


def _sa_profile(regex="(?i)\\bsolar ?assistant\\b"):
    raw = eo_profile()
    raw["id"] = "solarassistant-generic"
    integ = raw["ha"]["integrations"][0]
    integ["domain"], integ["model_regex"] = SA, [regex]
    return profile_from_dict(raw)


def _dev(id, manufacturer, entry, model=None):
    return DeviceSnap(id=id, manufacturer=manufacturer, model=model, name=None, sw_version=None,
                      hw_version=None, serial_number=None, identifiers=(), config_entry_ids=(entry,))


def _ent(eid, platform, device_id, entry):
    return EntitySnap(entity_id=eid, platform=platform, unique_id=eid, device_id=device_id,
                      config_entry_id=entry, device_class=None, unit=None, translation_key=None,
                      original_name=None, disabled=False)


def _sa_registry():
    entries = [ConfigEntrySnap("sa1", SA, "Solar Assistant", None)]
    devs = [_dev("total", "SolarAssistant", "sa1"), _dev("inv1", "SolarAssistant", "sa1")]
    ents = [_ent("sensor.total_pv_power", SA, "total", "sa1"), _ent("sensor.inverter_1_pv_power", SA, "inv1", "sa1")]
    return devs, ents, entries


def _all_packaged():
    return [load_builtin(pid) for pid in packaged_ids()]


def test_profile_declared_domain_classifies_inverter_entry():
    devs, ents, entries = _sa_registry()
    c = classify(devs, ents, entries, {}, profiles=[_sa_profile()])
    assert len(c.inverters) == 1
    inv = c.inverters[0]
    assert (inv.domain, inv.config_entry_id, inv.matched_by) == (SA, "sa1", "domain")
    assert [d.id for d in inv.devices] == ["total", "inv1"]
    assert [e.entity_id for e in inv.entities] == ["sensor.total_pv_power", "sensor.inverter_1_pv_power"]


def test_without_declaring_profile_nothing_is_found():
    devs, ents, entries = _sa_registry()
    assert classify(devs, ents, entries, {}).inverters == []
    assert classify(devs, ents, entries, {}, profiles=_all_packaged()).inverters == []


def test_declared_domain_needs_the_integration_pattern_to_match_a_device():
    devs, ents, entries = _sa_registry()
    assert classify(devs, ents, entries, {}, profiles=[_sa_profile("(?i)\\bnot-this\\b")]).inverters == []


def test_generic_platform_of_a_packaged_profile_is_unchanged():
    # `modbus` (profil Sungrow z pakietu, bez `model_regex`): licznik na `modbus` nie jest falownikiem,
    # falownik Sungrow na `modbus` wpada jak dotąd po producencie — wynik identyczny z profilami i bez
    entries = [ConfigEntrySnap("m1", "modbus", "Modbus", None)]
    devs = [_dev("meter", "Eastron", "m1", model="SDM630"), _dev("sg", "Sungrow", "m1", model="SH10RT")]
    ents = [_ent("sensor.meter_power", "modbus", "meter", "m1"), _ent("sensor.sg_power", "modbus", "sg", "m1")]
    before = classify(devs, ents, entries, {})
    after = classify(devs, ents, entries, {}, profiles=_all_packaged())
    assert after == before
    assert [(i.domain, i.matched_by, [d.id for d in i.devices]) for i in after.inverters] == [
        ("modbus", "manufacturer", ["sg"])]


def test_packaged_profiles_add_no_inverter_domain():
    # Żaden profil z pakietu nie rozszerza listy domen: wpisy z wzorcem mają domeny już znane, a wpisy bez
    # wzorca (np. `modbus`, `solaredge_modbus`) nie deklarują falownika dla żadnego tekstu urządzenia.
    for p in _all_packaged():
        for integ in p.raw["ha"]["integrations"]:
            if integ.get("model_regex"):
                assert integ["domain"] in INVERTER_DOMAINS, (p.id, integ["domain"])
            else:
                hint = InverterHint(integ["domain"], p.id, "any model")
                assert is_inverter(hint, [p]) == (integ["domain"] in INVERTER_DOMAINS), (p.id, integ["domain"])


def test_is_inverter_known_domain_without_profiles():
    assert is_inverter(InverterHint("goodwe", None, None)) is True
    assert is_inverter(InverterHint(SA, "SolarAssistant", None)) is False
    assert is_inverter(InverterHint(SA, "SolarAssistant", None), [_sa_profile()]) is True
