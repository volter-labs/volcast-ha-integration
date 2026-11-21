"""Opcje „Installation details”: propozycja przypisania czujników, ocena znaku, odporność na błąd."""
import asyncio
import json
from pathlib import Path

import pytest

import tests.test_options_flow as base
from custom_components.volcast import config_flow as cf
from custom_components.volcast import sensor_assignment as sa
from custom_components.volcast.core.discovery import sign_check as sc
from custom_components.volcast.core.discovery.assign import Candidate, FieldProposal, Proposal
from custom_components.volcast.core.discovery.sign_check import SignCheck, SignVerdict

ON = SignVerdict(True, None, 40, 0.9)
OFF = SignVerdict(False, None, 40, 0.9)
ROOT = Path(cf.__file__).parent


def _result(*, chosen=None, ambiguous=None, grid=OFF, battery=OFF, grid_negate=False, battery_negate=False,
            excluded=None, notes=()):
    fields = [FieldProposal(f, (Candidate(e, "high", 9, ()),), e) for f, e in (chosen or {}).items()]
    fields += [FieldProposal(f, tuple(Candidate(e, "medium", 5, ()) for e in es), None)
               for f, es in (ambiguous or {}).items()]
    return sa.AssignmentResult(
        proposal=Proposal(tuple(fields)), telemetry_map={}, grid_negate=grid_negate, battery_negate=battery_negate,
        sign_check=SignCheck(grid=grid, battery=battery, samples=288), basis="history", samples=288, sources={},
        source_origin={}, excluded=excluded or {}, notes=notes)


def _strings():
    return json.loads((ROOT / "strings.json").read_text(encoding="utf-8"))


def _flat(overrides=None):
    """Spłaszczone tłumaczenia jak z `async_get_translations` (kategorie `exceptions` i `options`)."""
    st = _strings()
    exc = {f"component.volcast.exceptions.{k}.message": v["message"] for k, v in st["exceptions"].items()}
    opt = {f"component.volcast.options.step.details.data.{k}": v for k, v in st["options"]["step"]["details"]["data"].items()}
    exc.update(overrides or {})
    return {"exceptions": exc, "options": opt}


def _flow(monkeypatch, result, calls=None, translations=None, **kw):
    async def tr(hass, category):
        if isinstance(translations, Exception):
            raise translations
        return (translations or _flat())[category]
    monkeypatch.setattr(cf, "_async_translations", tr)
    async def fake(hass, entry):
        if calls is not None:
            calls.append(1)
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(cf, "async_propose", fake)
    f = base.flow(**kw)
    shown = {}
    f.async_show_form = lambda **k: shown.update(k) or {"type": "form", "step_id": k["step_id"]}
    return f, shown


def _suggested(schema, name):
    for key in schema.schema:
        if str(key) == name:
            return key.description["suggested_value"]
    raise KeyError(name)


def test_empty_map_gets_suggestions_from_proposal(monkeypatch):
    f, shown = _flow(monkeypatch, _result(chosen={"pv_power_w": "sensor.pv", "soc": "sensor.soc"}))
    asyncio.run(f.async_step_details())
    assert _suggested(shown["data_schema"], "pv_power_w") == "sensor.pv"
    assert _suggested(shown["data_schema"], "soc") == "sensor.soc"
    assert _suggested(shown["data_schema"], "grid_power_w") is None


def test_saved_map_is_not_overwritten(monkeypatch):
    f, shown = _flow(monkeypatch, _result(chosen={"pv_power_w": "sensor.pv", "soc": "sensor.soc"}),
                     options={"telemetry_map": {"pv_power_w": "sensor.mine"}})
    asyncio.run(f.async_step_details())
    assert _suggested(shown["data_schema"], "pv_power_w") == "sensor.mine"
    assert _suggested(shown["data_schema"], "soc") == "sensor.soc"


def test_opening_the_form_saves_nothing(monkeypatch):
    opts = {"telemetry_map": {"soc": "sensor.s"}, "grid_power_negate": True}
    f, _ = _flow(monkeypatch, _result(chosen={"pv_power_w": "sensor.pv"}, grid=OFF), options=dict(opts))
    r = asyncio.run(f.async_step_details())
    assert r["type"] == "form"
    assert f.config_entry.options == opts


def test_sign_checkboxes_default_to_options_not_recommendation(monkeypatch):
    f, shown = _flow(monkeypatch, _result(grid=ON, battery=ON), options={"grid_power_negate": False})
    asyncio.run(f.async_step_details())
    defaults = {str(k): k.default() for k in shown["data_schema"].schema
                if str(k) in ("grid_power_negate", "battery_power_negate")}
    assert defaults == {"grid_power_negate": False, "battery_power_negate": False}


def test_submit_saves_battery_negate_and_none_when_unchecked(monkeypatch):
    f, _ = _flow(monkeypatch, _result())
    r = asyncio.run(f.async_step_details({"pv_power_w": "sensor.pv", "battery_power_negate": True}))
    assert r["data"] == {"telemetry_map": {"pv_power_w": "sensor.pv"}, "battery_power_negate": True}
    f, _ = _flow(monkeypatch, _result(), options={"battery_power_negate": True, "grid_power_negate": True})
    r = asyncio.run(f.async_step_details({"battery_power_negate": False, "grid_power_negate": False}))
    assert "battery_power_negate" not in r["data"] and "grid_power_negate" not in r["data"]


def test_validation_error_reuses_proposal_without_recomputing(monkeypatch):
    calls = []
    f, shown = _flow(monkeypatch, _result(chosen={"soc": "sensor.soc"}), calls=calls)
    asyncio.run(f.async_step_details())
    f._direct_errors = lambda user_input: {"base": "boom"}
    asyncio.run(f.async_step_details({"rated_power_w": 8000}))
    assert len(calls) == 1
    assert shown["errors"] == {"base": "boom"}
    assert _suggested(shown["data_schema"], "soc") == "sensor.soc"


def test_proposal_failure_does_not_block_the_form(monkeypatch):
    f, shown = _flow(monkeypatch, RuntimeError("boom"))
    r = asyncio.run(f.async_step_details())
    assert r["type"] == "form" and shown["step_id"] == "details"
    assert shown["description_placeholders"] == {"candidates": "-", "sign_check": "-"}
    assert _suggested(shown["data_schema"], "pv_power_w") is None


def test_translation_failure_does_not_block_the_form(monkeypatch):
    f, shown = _flow(monkeypatch, _result(chosen={"soc": "sensor.soc"}), translations=RuntimeError("x"))
    r = asyncio.run(f.async_step_details())
    assert r["type"] == "form" and shown["step_id"] == "details"
    assert shown["description_placeholders"] == {"candidates": "-", "sign_check": "-"}


def test_lines_use_translated_texts_for_the_ha_language(monkeypatch):
    pl = _flat({"component.volcast.exceptions.assignment_quantity_grid.message": "Siec",
                "component.volcast.exceptions.assignment_sign_recommend_on.message": "{quantity}: zalecane wlaczenie"})
    pl["options"]["component.volcast.options.step.details.data.grid_power_w"] = "Moc sieci"
    res = _result(ambiguous={"grid_power_w": ["sensor.g1"]}, grid=ON)
    f, shown = _flow(monkeypatch, res, translations=pl)
    asyncio.run(f.async_step_details())
    ph = shown["description_placeholders"]
    assert "- Siec: zalecane wlaczenie" in ph["sign_check"]
    assert "- Moc sieci: sensor.g1" in ph["candidates"]


def test_translations_are_fetched_in_the_ha_language_for_this_integration(monkeypatch):
    import sys
    import types
    calls = []

    async def fake(hass, language, category, integrations=None, config_flow=None):
        calls.append((language, category, integrations))
        return {}
    mod = types.ModuleType("homeassistant.helpers.translation")
    mod.async_get_translations = fake
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.translation", mod)
    hass = types.SimpleNamespace(config=types.SimpleNamespace(language="pl"))
    asyncio.run(cf._async_translations(hass, "exceptions"))
    assert calls == [("pl", "exceptions", ["volcast"])]


def test_description_lists_candidates_and_sign_results(monkeypatch):
    res = _result(ambiguous={"grid_power_w": ["sensor.g1", "sensor.g2"]}, grid=ON,
                  battery=SignVerdict(None, sc.BATTERY_IDLE, 0, 0.0),
                  excluded={"load_power_w": sa.EXCLUDED_LOAD_DERIVED})
    f, shown = _flow(monkeypatch, res)
    asyncio.run(f.async_step_details())
    ph = shown["description_placeholders"]
    assert "- Grid power: sensor.g1, sensor.g2" in ph["candidates"]
    assert "- Grid: recommended - turn the option on" in ph["sign_check"]
    assert "- Battery: could not be assessed - the battery was idle" in ph["sign_check"]
    assert "- Note: the house load sensor is calculated" in ph["sign_check"]


def test_match_is_reported_for_a_saved_map(monkeypatch):
    res = _result(grid=ON, battery=OFF, grid_negate=True)
    f, shown = _flow(monkeypatch, res, options={"telemetry_map": {"grid_power_w": "sensor.g"}})
    asyncio.run(f.async_step_details())
    text = shown["description_placeholders"]["sign_check"]
    assert "Grid: matches the current setting" in text and "Battery: matches the current setting" in text


def test_every_code_has_a_translation_key():
    keys = set(_strings()["exceptions"])
    reasons = {getattr(sc, n) for n in ("NO_SENSOR", "TOO_FEW_SAMPLES", "GRID_IDLE", "BATTERY_IDLE",
                                        "NO_LOAD_SENSOR", "AMBIGUOUS")}
    excluded = {v for k, v in vars(sa).items() if k.startswith("EXCLUDED_")}
    notes = {v for k, v in vars(sa).items() if k.startswith("NOTE_")}
    assert len(excluded) == 3 and notes
    expected = ({f"assignment_reason_{c}" for c in reasons} | {f"assignment_excluded_{c}" for c in excluded}
                | {f"assignment_note_{c}" for c in notes})
    assert expected <= keys
    assert {"assignment_quantity_grid", "assignment_quantity_battery", "assignment_sign_recommend_on",
            "assignment_sign_recommend_off", "assignment_sign_match", "assignment_sign_unknown",
            "assignment_note", "assignment_reason_unknown"} <= keys


def test_unpaired_form_mentions_the_account_connection():
    for name in ("strings.json", "translations/en.json"):
        text = json.loads((ROOT / name).read_text(encoding="utf-8"))["options"]["step"]["init"]["description"]
        assert "connection to your Volcast account" in text
    r = asyncio.run(base.flow(paired=False).async_step_init())
    assert (r["type"], r["step_id"]) == ("form", "init")


def test_details_texts_have_the_placeholders_the_code_provides():
    d = json.loads((ROOT / "strings.json").read_text(encoding="utf-8"))["options"]["step"]["details"]
    assert "{candidates}" in d["description"] and "{sign_check}" in d["description"]
    assert "battery_power_negate" in d["data"] and "battery_power_negate" in d["data_description"]
