"""Ścieżka „tylko encje” od rejestru HA do mapy encji — na czystych funkcjach, bez sprzętu.

Integracja pośrednicząca odczyt (`solar_assistant`): urządzenia z producentem, bez modelu; `unique_id`
= `<zakres>_<urządzenie>/<metryka>`. Profil „tylko encje” deklaruje integrację i wzorzec producenta.
"""
from custom_components.volcast.core.control.recommend import NO_WRITE_PATH, RUNG_IDENTIFY, UNSUPPORTED, recommend
from custom_components.volcast.core.control.select import (InverterHint, ProfileChoice, ambiguous_profiles,
                                                           select_profile)
from custom_components.volcast.core.discovery.classify import classify, is_inverter
from custom_components.volcast.core.discovery.models import ConfigEntrySnap, DeviceSnap, EntitySnap
from custom_components.volcast.core.entity_map import EntityCandidate, resolve_entities
from custom_components.volcast.core.profile import entities_only, profile_from_dict, writes_allowed

SA = "solar_assistant"
SCOPE = "local:10.0.0.10"


def _profile():
    # Minimalny profil inline (prawdziwy plik profilu powstaje osobno).
    return profile_from_dict({
        "schema_version": 1, "id": "solarassistant-test", "label": "Solar Assistant (test)", "status": "draft",
        "sources": [{"what": "test", "ref": "test"}], "access": "entities_only", "min_engine": 3,
        "ha": {"integrations": [{"domain": SA, "ems": False, "status": "draft",
                                 "model_regex": ["(?i)\\bsolar ?assistant\\b"], "entities": {
            "soc": {"domain": "sensor", "unique_id_regex": "_total/battery_state_of_charge$"},
            "pv_power_w": {"domain": "sensor", "unique_id_regex": "_total/pv_power$"},
            "load_power_w": {"domain": "sensor", "unique_id_regex": "_total/load_power$"},
            "grid_power_w": {"domain": "sensor", "unique_id_regex": "_total/grid_power$"}}}]},
        "capabilities": {"force_charge_from_grid": False, "sell_from_battery": False, "force_discharge": False,
                         "standby": False, "set_power_w": False, "limit_export": False, "set_soc_floor": False,
                         "set_soc_ceiling": False, "time_windows": 0},
    })


def _dev(id):
    return DeviceSnap(id=id, manufacturer="SolarAssistant", model=None, name=None, sw_version=None,
                      hw_version=None, serial_number=None, identifiers=(), config_entry_ids=("sa1",))


def _ent(topic, device_id, unit):
    eid = "sensor." + topic.replace("/", "_")
    return EntitySnap(entity_id=eid, platform=SA, unique_id=f"{SCOPE}_{topic}", device_id=device_id,
                      config_entry_id="sa1", device_class=None, unit=unit, translation_key=None,
                      original_name=None, disabled=False)


def _registry():
    entries = [ConfigEntrySnap("sa1", SA, "Solar Assistant", None)]
    devices = [_dev("total"), _dev("inverter_1"), _dev("battery_1")]
    entities = [_ent("total/pv_power", "total", "W"), _ent("total/load_power", "total", "W"),
                _ent("total/grid_power", "total", "W"), _ent("total/battery_state_of_charge", "total", "%"),
                # urządzenia numerowane — wzorce `_total/...` ich nie biorą
                _ent("inverter_1/pv_power", "inverter_1", "W"),
                _ent("battery_1/state_of_charge", "battery_1", "%")]
    return devices, entities, entries


def _hints(finding, profiles):
    hints = [InverterHint(finding.domain, d.manufacturer, d.model) for d in finding.devices]
    return [h for h in hints if is_inverter(h, profiles)]


def test_entities_only_profile_from_registry_to_entity_map():
    devices, entities, entries = _registry()
    profile = _profile()
    assert entities_only(profile) and writes_allowed(profile) is False

    cls = classify(devices, entities, entries, {}, profiles=[profile])
    assert [(i.domain, i.matched_by) for i in cls.inverters] == [(SA, "domain")]
    inv = cls.inverters[0]
    assert {d.id for d in inv.devices} == {"total", "inverter_1", "battery_1"}

    hints = _hints(inv, [profile])
    assert hints and all(h == InverterHint(SA, "SolarAssistant", None) for h in hints)

    choice = select_profile(hints, [profile])
    assert (choice.profile.id, choice.integration_domain, choice.model) == ("solarassistant-test", SA, None)
    assert ambiguous_profiles(hints, [profile]) == {}

    cands = [EntityCandidate(e.entity_id, e.platform, e.unique_id, e.unit) for e in inv.entities]
    res = resolve_entities(choice.profile, choice.integration_domain, cands)
    assert res.mapped == {"soc": "sensor.total_battery_state_of_charge",
                          "pv_power_w": "sensor.total_pv_power",
                          "load_power_w": "sensor.total_load_power",
                          "grid_power_w": "sensor.total_grid_power"}
    assert res.missing == [] and res.ambiguous == {} and res.incompatible == {}


def test_same_registry_without_the_profile_yields_nothing():
    devices, entities, entries = _registry()
    assert classify(devices, entities, entries, {}).inverters == []
    hint = InverterHint(SA, "SolarAssistant", None)
    assert is_inverter(hint, ()) is False
    assert select_profile([hint], []) is None


def test_recommendation_for_entities_only_choice_is_unsupported_no_write_path():
    # Profil „tylko encje” odczytuje, ale nie ma drogi zapisu: rekomendacja `unsupported`/`no_write_path`
    # z dołączoną integracją; to samo z wyborem podanym i wyliczonym z raportu.
    profile = _profile()
    report = {"inverters": [{"domain": SA, "matched_by": "domain", "config_entry_title": "Solar Assistant",
                             "devices": [{"manufacturer": "SolarAssistant", "model": None}], "entities": []}]}
    mapped = {"pv_power_w": "sensor.total_pv_power", "soc": "sensor.total_battery_state_of_charge"}
    for choice in (ProfileChoice(profile, SA, None), None):
        rec = recommend(report, [profile], entity_map=mapped, choice=choice)
        assert (rec.path, rec.reason, rec.ladder_start) == (UNSUPPORTED, NO_WRITE_PATH, RUNG_IDENTIFY)
        assert rec.integration == {"domain": SA, "name": SA}
        assert rec.device == {"manufacturer": "SolarAssistant", "model": None}
