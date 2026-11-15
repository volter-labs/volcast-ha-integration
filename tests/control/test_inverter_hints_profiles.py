"""Wskazówki falowników w trybie sterowania: domena znana albo zadeklarowana przez załadowany profil."""
from types import SimpleNamespace

from custom_components.volcast.control import runtime
from custom_components.volcast.core.profile import load_builtin, profile_from_dict
from tests.core.profile_fixtures import eo_profile

SA = "solar_assistant"


def _sa_profile():
    raw = eo_profile()
    raw["ha"]["integrations"][0].update(domain=SA, model_regex=["(?i)\\bsolar ?assistant\\b"])
    return profile_from_dict(raw)


def _dev(i, manufacturer, model, entry):
    return SimpleNamespace(id=f"d{i}", manufacturer=manufacturer, model=model, name=None, sw_version=None,
                           config_entries={entry}, disabled_by=None, serial_number=None, identifiers=set())


def _hass(monkeypatch, devices, domains):
    reg = SimpleNamespace(devices={d.id: d for d in devices})
    monkeypatch.setattr(runtime.dr, "async_get", lambda hass: reg)
    return SimpleNamespace(config_entries=SimpleNamespace(
        async_get_entry=lambda ce_id: SimpleNamespace(domain=domains[ce_id])))


def test_profile_declared_domain_gives_hint_for_matching_device(monkeypatch):
    hass = _hass(monkeypatch, [_dev(1, "SolarAssistant", None, "sa"), _dev(2, "Other", None, "sa"),
                               _dev(3, "GoodWe", "GW10K-ET", "gw")],
                 {"sa": SA, "gw": "goodwe"})
    hints = runtime.inverter_hints(hass, [_sa_profile()])
    assert [(h.domain, h.manufacturer, h.model) for h in hints] == [
        (SA, "SolarAssistant", None), ("goodwe", "GoodWe", "GW10K-ET")]
    # bez profilu — jak dotąd: tylko znane domeny
    assert [h.domain for h in runtime.inverter_hints(hass)] == ["goodwe"]


def test_generic_platform_of_packaged_profile_gives_no_hint(monkeypatch):
    hass = _hass(monkeypatch, [_dev(1, "Sungrow", "SH10RT", "m")], {"m": "modbus"})
    assert runtime.inverter_hints(hass, [load_builtin("sungrow-sh")]) == []


def test_choice_for_uses_profile_declared_domain(monkeypatch):
    hass = _hass(monkeypatch, [_dev(1, "SolarAssistant", None, "sa")], {"sa": SA})
    entry = SimpleNamespace(options={})
    choice = runtime._choice_for(hass, entry, [_sa_profile()])
    assert (choice.profile.id, choice.integration_domain, choice.model) == ("test-eo", SA, None)
