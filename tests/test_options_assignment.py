"""Opcje „Installation details”: propozycja przypisania czujników, ocena znaku, odporność na błąd."""
import asyncio
import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

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


NO_INVERTER = SimpleNamespace(choice=None, direct=None)
GW_DEVICE = SimpleNamespace(id="gw", manufacturer="GoodWe", model="GW8KN-ET", name="Inverter", sw_version=None,
                            serial_number=None, identifiers=set(), config_entries={"gw_entry"}, disabled_by=None)


def live_registry(monkeypatch, hass, devices=(), profiles=None):
    """Bieżące rejestry HA dla wyboru profilu liczonego na żywo: urządzenia (mutowalne `reg.devices`),
    wpisy integracji falowników (`gw_entry` → `goodwe`) i profile (domyślnie wbudowany GoodWe ET)."""
    from custom_components.volcast.control import runtime as rt_mod
    from custom_components.volcast.core.profile import load_builtin

    reg = SimpleNamespace(devices={d.id: d for d in devices})
    monkeypatch.setattr(rt_mod.dr, "async_get", lambda h: reg)
    profs = [load_builtin("goodwe-et")] if profiles is None else list(profiles)
    monkeypatch.setattr(rt_mod.ds, "load_profiles", lambda: list(profs))

    async def job(fn, *args):
        return fn(*args)
    hass.async_add_executor_job = job
    ce = getattr(hass, "config_entries", None)
    if ce is None:
        ce = hass.config_entries = SimpleNamespace()
    prev = getattr(ce, "async_get_entry", None)
    ce.async_get_entry = lambda i: SimpleNamespace(domain="goodwe") if i == "gw_entry" \
        else (prev(i) if prev is not None else None)
    return reg


def _flow(monkeypatch, result, calls=None, translations=None, runtime=NO_INVERTER, **kw):
    async def tr(hass, category):
        if isinstance(translations, Exception):
            raise translations
        return (translations or _flat())[category]
    monkeypatch.setattr(cf, "_async_translations", tr)
    async def fake(hass, entry, with_proposal=True):
        if calls is not None:
            calls.append(1)
        if isinstance(result, Exception):
            raise result
        if not with_proposal:
            return dataclasses.replace(result, proposal=Proposal(()))
        return result
    monkeypatch.setattr(cf, "async_propose", fake)
    f = base.flow(runtime=runtime, **kw)
    f.registry = live_registry(monkeypatch, f.hass)        # domyślnie: w rejestrach nie ma falownika
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
    assert shown["description_placeholders"] == {"candidates": "", "sign_check": ""}
    assert _suggested(shown["data_schema"], "pv_power_w") is None


def test_translation_failure_does_not_block_the_form(monkeypatch):
    f, shown = _flow(monkeypatch, _result(chosen={"soc": "sensor.soc"}), translations=RuntimeError("x"))
    r = asyncio.run(f.async_step_details())
    assert r["type"] == "form" and shown["step_id"] == "details"
    assert shown["description_placeholders"] == {"candidates": "", "sign_check": ""}
    assert _suggested(shown["data_schema"], "soc") == "sensor.soc"


def test_lines_use_translated_texts_for_the_ha_language(monkeypatch):
    pl = _flat({"component.volcast.exceptions.assignment_quantity_grid.message": "Siec",
                "component.volcast.exceptions.assignment_sign_recommend_on.message": "{quantity}: zalecane wlaczenie ({option})"})
    pl["options"]["component.volcast.options.step.details.data.grid_power_w"] = "Moc sieci"
    res = _result(ambiguous={"grid_power_w": ["sensor.g1"]}, grid=ON)
    f, shown = _flow(monkeypatch, res, translations=pl)
    asyncio.run(f.async_step_details())
    ph = shown["description_placeholders"]
    assert "- Siec: zalecane wlaczenie (Grid sensor shows export as positive)" in ph["sign_check"]
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
    assert "- Grid: recommended - turn on \"Grid sensor shows export as positive\"" in ph["sign_check"]
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
    reasons = {v for k, v in vars(sc).items() if k.isupper() and isinstance(v, str) and v not in sc.QUANTITIES}
    assert len(reasons) >= 6
    excluded = {v for k, v in vars(sa).items() if k.startswith("EXCLUDED_")}
    notes = {v for k, v in vars(sa).items() if k.startswith("NOTE_")}
    assert len(excluded) == 3 and notes
    expected = ({f"assignment_reason_{c}" for c in reasons} | {f"assignment_excluded_{c}" for c in excluded}
                | {f"assignment_note_{c}" for c in notes})
    assert expected <= keys
    assert {"assignment_quantity_grid", "assignment_quantity_battery", "assignment_sign_recommend_on",
            "assignment_sign_recommend_off", "assignment_sign_match", "assignment_sign_unknown",
            "assignment_note", "assignment_reason_unknown"} <= keys


def _shown_init(monkeypatch, **kw):
    async def tr(hass, category):
        return _flat()[category]
    monkeypatch.setattr(cf, "_async_translations", tr)
    f = base.flow(**kw)
    shown = {}
    f.async_show_form = lambda **k: shown.update(k) or {"type": "form", "step_id": k["step_id"]}
    f.async_show_menu = lambda **k: shown.update(k) or {"type": "menu", "step_id": k["step_id"]}
    return f, shown


def test_unpaired_form_mentions_the_account_connection(monkeypatch):
    f, shown = _shown_init(monkeypatch, paired=False)
    r = asyncio.run(f.async_step_init())
    assert (r["type"], r["step_id"]) == ("form", "init")
    assert "connection to your Volcast account" in shown["description_placeholders"]["account_hint"]
    for name in ("strings.json", "translations/en.json"):
        data = json.loads((ROOT / name).read_text(encoding="utf-8"))
        assert data["options"]["step"]["init"]["description"] == "{account_hint}"
        assert "connection to your Volcast account" in data["exceptions"]["account_connection_hint"]["message"]


def test_unpaired_form_survives_a_translation_failure(monkeypatch):
    async def boom(hass, category):
        raise RuntimeError("x")
    monkeypatch.setattr(cf, "_async_translations", boom)
    f = base.flow(paired=False)
    shown = {}
    f.async_show_form = lambda **k: shown.update(k) or {"type": "form", "step_id": k["step_id"]}
    assert asyncio.run(f.async_step_init())["type"] == "form"
    assert shown["description_placeholders"] == {"account_hint": ""}


def test_paired_menu_has_no_account_sentence(monkeypatch):
    f, shown = _shown_init(monkeypatch)
    r = asyncio.run(f.async_step_init())
    assert r["type"] == "menu"
    assert shown["description_placeholders"] == {"account_hint": ""}


# ── twarde ograniczenie: rozpoznany falownik nie dostaje ręcznej mapy z podpowiedzi ─────────────────────

def _recognised():
    return SimpleNamespace(choice=SimpleNamespace(integration_domain="goodwe"), direct=None)


def _no_hint_for_every_field(shown):
    return all(_suggested(shown["data_schema"], f) is None for f in cf.TELEMETRY_FIELDS)


@pytest.mark.parametrize("runtime,options", [
    (_recognised(), {}),                                                         # profil rozpoznany
    (SimpleNamespace(choice=None, direct=object()), {}),                         # połączenie bezpośrednie (runtime)
    (SimpleNamespace(choice=None, direct=None), {"direct_target": {"host": "1.2.3.4"}}),   # cel zapisany w opcjach
    (None, {}),                                                                  # brak runtime: nie wiadomo
])
def test_no_suggestions_when_the_plugin_already_recognises_the_inverter(monkeypatch, runtime, options):
    f, shown = _flow(monkeypatch, _result(chosen={"pv_power_w": "sensor.pv", "battery_power_w": "sensor.b"},
                                         ambiguous={"grid_power_w": ["sensor.g1", "sensor.g2"]}, grid=ON),
                     runtime=runtime, options=dict(options))
    asyncio.run(f.async_step_details())
    assert _no_hint_for_every_field(shown)
    assert shown["description_placeholders"] == {"candidates": "", "sign_check": ""}   # bez zapisanej mapy: cisza


def test_recognised_profile_capacity_change_saves_no_telemetry_map(monkeypatch):
    f, shown = _flow(monkeypatch, _result(chosen={"pv_power_w": "sensor.pv"}), runtime=_recognised(),
                     options={"battery_capacity_kwh": 10})
    asyncio.run(f.async_step_details())
    # formularz pokazuje pola bez podpowiedzi, więc zatwierdzenie samej pojemności nie niesie czujników
    r = asyncio.run(f.async_step_details({"battery_capacity_kwh": 12}))
    assert r["data"] == {"battery_capacity_kwh": 12}


def test_recognised_inverter_with_saved_manual_map_still_shows_sign_check_but_no_candidates(monkeypatch):
    res = _result(ambiguous={"grid_power_w": ["sensor.g1", "sensor.g2"]}, grid=ON)
    f, shown = _flow(monkeypatch, res, runtime=_recognised(), options={"telemetry_map": {"soc": "sensor.soc"}})
    asyncio.run(f.async_step_details())
    ph = shown["description_placeholders"]
    assert ph["candidates"] == "" and "Grid: recommended" in ph["sign_check"]
    assert _suggested(shown["data_schema"], "soc") == "sensor.soc"          # zapisana wartość zostaje


def test_headings_are_empty_without_candidates(monkeypatch):
    f, shown = _flow(monkeypatch, _result(chosen={"soc": "sensor.soc"}))
    asyncio.run(f.async_step_details())
    ph = shown["description_placeholders"]
    assert ph["candidates"] == "" and ph["sign_check"].startswith("Power sign check")


def test_details_texts_have_the_placeholders_the_code_provides():
    d = json.loads((ROOT / "strings.json").read_text(encoding="utf-8"))["options"]["step"]["details"]
    assert "{candidates}" in d["description"] and "{sign_check}" in d["description"]
    assert "already filled in" not in d["description"]
    assert "battery_power_negate" in d["data"] and "battery_power_negate" in d["data_description"]


# --- bez podpowiedzi: wynik dotyczy tylko czujników zapisanych ręcznie -------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("saved, expected_sources", [
    ({}, {}),
    ({"pv_power_w": "sensor.pv_power"}, {"pv_power_w": "sensor.pv_power"}),
])
async def test_not_applicable_result_covers_only_manually_saved_sensors(make_hass, saved, expected_sources):
    from unittest.mock import MagicMock, patch

    import tests.test_sensor_assignment as sat
    from custom_components.volcast.const import DOMAIN

    hass = sat._hass(make_hass, entities=sat._four_entities(), states=sat._four_states())
    hass.data = {DOMAIN: {"v1": {"control": SimpleNamespace(
        choice=SimpleNamespace(integration_domain="goodwe"), direct=None)}}}
    entry = sat._entry({"telemetry_map": saved} if saved else {})
    with patch.object(sa, "statistics_during_period", MagicMock(return_value=sat._stats_rows())):
        result, applicable = await cf.async_assignment_result(hass, entry)
    assert applicable is False and result.proposal.chosen() == {}
    # kandydaci z propozycji nie mogą trafić do oceny znaku ani do źródeł
    assert result.sources == expected_sources
    assert set(result.source_origin.values()) <= {"map"}
    if not saved:
        assert result.basis == sa.BASIS_NONE and result.samples == 0


# --- profil dostarczony po starcie: podpowiedź czujników ustępuje ogłoszonemu wyborowi ----------------

@pytest.mark.parametrize("announced, expected", [
    (None, True),                              # nic nie ogłoszono — podpowiedź dozwolona
    (("acme-hybrid", None), True),             # profil bez integracji (tylko odczyt) — nic nie zasłania
    (("acme-hybrid", "acme"), False),          # nowy profil z integracją czeka na przeładowanie
])
def test_pending_profile_choice_suppresses_sensor_suggestion(announced, expected):
    from custom_components.volcast.control.runtime import no_inverter_integration

    control = SimpleNamespace(choice=None, direct=None, announced_choice=announced)
    assert no_inverter_integration({}, control) is expected


# --- integracja falownika doinstalowana po setupie: wybór profilu liczony na żywo -------------------

_SUGGESTED = {"pv_power_w": "sensor.gw_pv", "soc": "sensor.gw_soc"}


def test_live_choice_recognises_the_goodwe_device(monkeypatch):
    """Założenie testów niżej: ten sam wybór co przy składaniu sterowania rozpoznaje dodane urządzenie."""
    from custom_components.volcast.control import runtime as rt_mod

    hass = SimpleNamespace()
    live_registry(monkeypatch, hass, devices=[GW_DEVICE])
    choice = rt_mod._choice_for(hass, SimpleNamespace(options={}), rt_mod.ds.load_profiles())
    assert choice is not None and choice.integration_domain == "goodwe"


def test_details_form_does_not_suggest_once_an_inverter_integration_appears(monkeypatch):
    f, shown = _flow(monkeypatch, _result(chosen=_SUGGESTED))
    f.registry.devices["gw"] = GW_DEVICE                 # integracja dodana po setupie, bez przeładowania wpisu
    asyncio.run(f.async_step_details())
    assert _no_hint_for_every_field(shown)
    assert shown["description_placeholders"] == {"candidates": "", "sign_check": ""}


def test_details_form_does_not_suggest_when_the_live_choice_fails(monkeypatch):
    from custom_components.volcast.control import runtime as rt_mod

    f, shown = _flow(monkeypatch, _result(chosen=_SUGGESTED))

    def boom(hass):
        raise RuntimeError("device registry unavailable")
    monkeypatch.setattr(rt_mod.dr, "async_get", boom)      # krok sam wczytuje profile wcześniej — psuje się rejestr
    asyncio.run(f.async_step_details())
    assert _no_hint_for_every_field(shown)


def test_details_form_still_suggests_without_an_inverter_integration(monkeypatch):
    f, shown = _flow(monkeypatch, _result(chosen=_SUGGESTED))
    # urządzenie spoza integracji falownika (nie pasuje do żadnego profilu) nie wyłącza podpowiedzi
    f.registry.devices["meter"] = SimpleNamespace(**{**vars(GW_DEVICE), "id": "meter", "manufacturer": "Acme",
                                                     "model": "Meter", "config_entries": {"other"}})
    asyncio.run(f.async_step_details())
    assert _suggested(shown["data_schema"], "pv_power_w") == "sensor.gw_pv"


def test_opening_the_form_leaves_the_control_choice_unchanged(monkeypatch):
    from custom_components.volcast.control import runtime as rt_mod

    rt = rt_mod.ControlRuntime(None, None, None, None, None, {}, None)
    f, shown = _flow(monkeypatch, _result(chosen=_SUGGESTED), runtime=rt)
    f.registry.devices["gw"] = GW_DEVICE
    asyncio.run(f.async_step_details())
    assert _no_hint_for_every_field(shown)
    # wybór na żywo służy tylko podpowiedzi — sterowanie dalej widzi wybór z chwili setupu
    assert rt.choice is None and rt.announced_choice is None and rt.mapped == {}
