"""Naprawa „Wznów sterowanie teraz" w zgłoszeniu pauzy przejęcia."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.conftest import FakeHass


def _hass_with_control(executor) -> FakeHass:
    hass = FakeHass()
    hass.data["volcast"] = {"e1": {"control": SimpleNamespace(executor=executor)}}
    return hass


@pytest.mark.asyncio
async def test_foreign_control_fix_flow_confirms_then_resumes():
    from custom_components.volcast import repairs

    ex = SimpleNamespace(async_resume_control=AsyncMock(return_value="resumed"))
    hass = _hass_with_control(ex)
    flow = await repairs.async_create_fix_flow(hass, "foreign_control_e1", {"entry_id": "e1"})
    flow.hass = hass
    form = await flow.async_step_init()
    assert form["type"] == "form" and form["step_id"] == "confirm"
    ex.async_resume_control.assert_not_awaited()                 # samo otwarcie niczego nie wznawia
    done = await flow.async_step_confirm({})
    assert done["type"] == "create_entry"
    ex.async_resume_control.assert_awaited_once()


@pytest.mark.asyncio
async def test_fix_flow_for_unloaded_entry_just_closes():
    from custom_components.volcast import repairs

    hass = FakeHass()
    hass.data["volcast"] = {}
    flow = await repairs.async_create_fix_flow(hass, "foreign_control_e1", {"entry_id": "e1"})
    flow.hass = hass
    await flow.async_step_init()
    assert (await flow.async_step_confirm({}))["type"] == "create_entry"


@pytest.mark.asyncio
async def test_resume_service_resumes_every_controlled_entry():
    from custom_components.volcast import _async_register_services
    from custom_components.volcast.const import DOMAIN, SERVICE_RESUME_CONTROL

    ex = SimpleNamespace(async_resume_control=AsyncMock(return_value="resumed"))
    hass = _hass_with_control(ex)
    hass.data["volcast"]["e2"] = {"reconciler": None}            # wpis bez sterowania
    _async_register_services(hass)
    await hass.services.registered[(DOMAIN, SERVICE_RESUME_CONTROL)](SimpleNamespace(data={}))
    ex.async_resume_control.assert_awaited_once()


@pytest.mark.asyncio
async def test_resume_service_without_control_raises_validation_error():
    from homeassistant.exceptions import ServiceValidationError

    from custom_components.volcast import _async_register_services
    from custom_components.volcast.const import DOMAIN, SERVICE_RESUME_CONTROL

    hass = FakeHass()
    hass.data["volcast"] = {"e1": {"reconciler": None}}
    _async_register_services(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.registered[(DOMAIN, SERVICE_RESUME_CONTROL)](SimpleNamespace(data={}))


@pytest.mark.asyncio
async def test_fix_flow_aborts_while_inverter_mode_is_outside_the_profile():
    # Tryb spoza profilu nadal wstrzymuje zapisy — „wznowiono" byłoby nieprawdą; zgłoszenie zostaje.
    from custom_components.volcast import repairs

    ex = SimpleNamespace(async_resume_control=AsyncMock(return_value="foreign_mode"))
    hass = _hass_with_control(ex)
    flow = await repairs.async_create_fix_flow(hass, "foreign_control_e1", {"entry_id": "e1"})
    flow.hass = hass
    await flow.async_step_init()
    assert await flow.async_step_confirm({}) == {"type": "abort", "reason": "foreign_mode"}


# ── mapa czujników z propozycją (brak rozpoznanego falownika) ─────────────────

_ISSUE = "sensor_assignment_missing_e1"


def _assignment_world(monkeypatch, *, options=None, loaded=True, entry_exists=True):
    """Flow naprawy na atrapach: wpis w `config_entries`, propozycja i tłumaczenia podmienione."""
    import tests.test_options_assignment as oa
    from custom_components.volcast import config_flow as cf
    from custom_components.volcast import repairs

    entry = SimpleNamespace(entry_id="e1", data={"api_key": "vk_x"}, options=dict(options or {}))
    updates = []

    def update(e, **kw):
        updates.append(kw)
        for k, v in kw.items():
            setattr(e, k, v)
        return True

    async def tr(hass, category):
        return oa._flat()[category]

    async def propose(hass, e, with_proposal=True):
        if not with_proposal:
            return oa._result(grid=oa.ON)
        return oa._result(chosen={"pv_power_w": "sensor.pv_now", "soc": "sensor.batt_soc"},
                          ambiguous={"grid_power_w": ["sensor.grid_a", "sensor.grid_b"]},
                          grid=oa.ON)
    monkeypatch.setattr(cf, "_async_translations", tr)
    monkeypatch.setattr(cf, "async_propose", propose)
    hass = FakeHass()
    hass.data["volcast"] = {"e1": {"control": SimpleNamespace(choice=None, direct=None)}} if loaded else {}
    hass.config_entries = SimpleNamespace(async_get_entry=lambda eid: entry if entry_exists and eid == "e1" else None,
                                          async_update_entry=update)
    return hass, entry, updates, repairs


async def _open(hass, repairs):
    flow = await repairs.async_create_fix_flow(hass, _ISSUE, {"entry_id": "e1"})
    flow.hass = hass
    shown = {}
    flow.async_show_form = lambda **k: shown.update(k) or {"type": "form", "step_id": k["step_id"]}
    first = await flow.async_step_init()
    return flow, shown, first


def _suggested(shown):
    return {str(k): (k.description or {}).get("suggested_value") for k in shown["data_schema"].schema}


@pytest.mark.asyncio
async def test_assignment_fix_shows_the_proposal_and_does_not_touch_options(monkeypatch):
    hass, entry, updates, repairs = _assignment_world(monkeypatch, options={"x": 1})
    flow, shown, first = await _open(hass, repairs)
    assert first["type"] == "form" and first["step_id"] == "assign"
    sug = _suggested(shown)
    assert sug["pv_power_w"] == "sensor.pv_now" and sug["soc"] == "sensor.batt_soc"
    assert sug["grid_power_w"] is None                                   # niejednoznaczne: bez podpowiedzi
    # te same pola mapy co krok opcji, bez części połączenia bezpośredniego i mocy znamionowej
    from custom_components.volcast.control.telemetry import TELEMETRY_FIELDS
    assert set(sug) == {*TELEMETRY_FIELDS, "grid_power_negate", "battery_power_negate"}
    assert "sensor.grid_a" in shown["description_placeholders"]["candidates"]
    assert shown["description_placeholders"]["sign_check"] != "-"
    assert updates == [] and entry.options == {"x": 1}


@pytest.mark.asyncio
async def test_assignment_fix_submit_saves_map_and_flags_and_keeps_other_options(monkeypatch):
    hass, entry, updates, repairs = _assignment_world(
        monkeypatch, options={"control_mode": "entities", "telemetry_map": {"soc": "sensor.old"}})
    flow, shown, _ = await _open(hass, repairs)
    done = await flow.async_step_assign({"pv_power_w": "sensor.pv_now", "soc": "", "grid_power_w": "  sensor.grid_a ",
                                         "grid_power_negate": True, "battery_power_negate": False})
    assert done["type"] == "create_entry"
    assert len(updates) == 1
    assert entry.options == {"control_mode": "entities",
                             "telemetry_map": {"pv_power_w": "sensor.pv_now", "grid_power_w": "sensor.grid_a"},
                             "grid_power_negate": True}                     # pusty = brak wpisu; fałsz = brak klucza


@pytest.mark.asyncio
async def test_assignment_fix_submit_clears_other_fields_without_losing_other_options(monkeypatch):
    hass, entry, updates, repairs = _assignment_world(monkeypatch, options={"rated_power_w": 8000,
                                                                          "battery_power_negate": True})
    flow, _, _ = await _open(hass, repairs)
    await flow.async_step_assign({"pv_power_w": "sensor.pv"})
    assert entry.options == {"rated_power_w": 8000, "telemetry_map": {"pv_power_w": "sensor.pv"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("user_input", [{}, {"pv_power_w": ""}, {"pv_power_w": "   ", "soc": "sensor.soc"}])
async def test_assignment_fix_without_pv_power_shows_the_form_again_and_saves_nothing(monkeypatch, user_input):
    hass, entry, updates, repairs = _assignment_world(monkeypatch, options={"rated_power_w": 8000})
    flow, shown, _ = await _open(hass, repairs)
    shown.clear()
    result = await flow.async_step_assign(user_input)
    assert result["type"] == "form" and result["step_id"] == "assign"
    assert shown["errors"] == {"base": "pv_power_required"}
    assert updates == [] and entry.options == {"rated_power_w": 8000}
    # to, co użytkownik już wybrał, zostaje w formularzu
    if user_input.get("soc"):
        assert _suggested(shown)["soc"] == "sensor.soc"


@pytest.mark.asyncio
@pytest.mark.parametrize("kw", [{"loaded": False}, {"entry_exists": False}])
async def test_assignment_fix_for_unloaded_or_missing_entry_aborts_clearly(monkeypatch, kw):
    hass, entry, updates, repairs = _assignment_world(monkeypatch, **kw)
    flow, shown, first = await _open(hass, repairs)
    assert first == {"type": "abort", "reason": "not_loaded"}
    done = await flow.async_step_assign({"pv_power_w": "sensor.pv"})
    assert done["type"] == "abort" and updates == []


@pytest.mark.asyncio
async def test_assignment_fix_survives_a_failing_proposal(monkeypatch):
    from custom_components.volcast import config_flow as cf
    hass, entry, updates, repairs = _assignment_world(monkeypatch)

    async def boom(hass, e, **_):
        raise RuntimeError("x")
    monkeypatch.setattr(cf, "async_propose", boom)
    flow, shown, first = await _open(hass, repairs)
    assert first["type"] == "form" and updates == []
    assert shown["description_placeholders"] == {"candidates": "", "sign_check": ""}


@pytest.mark.parametrize("name", ["strings.json", "translations/en.json"])
def test_assignment_fix_labels_match_the_installation_details_step(name):
    # HA nie dziedziczy etykiet pól z opcji — naprawa ma własne, te same co w kroku „details".
    import json
    from pathlib import Path

    from custom_components.volcast.control.telemetry import TELEMETRY_FIELDS

    raw = json.loads((Path(__file__).parents[1] / "custom_components" / "volcast" / name).read_text("utf-8"))
    step = raw["issues"]["sensor_assignment_missing"]["fix_flow"]["step"]["assign"]
    details = raw["options"]["step"]["details"]["data"]
    keys = [*TELEMETRY_FIELDS, "grid_power_negate", "battery_power_negate"]
    assert step["data"] == {k: details[k] for k in keys}
    assert "{candidates}" in step["description"] and "{sign_check}" in step["description"]


# ── luki w encjach sterujących (wyłączone / nieobecne) ────────────────────────


def _gap_world(entries, monkeypatch, *, verified=True):
    from custom_components.volcast.control import runtime as rt_mod
    from custom_components.volcast.core.control.select import ProfileChoice
    from tests.core.modbus.engine_profile import directional_profile

    def mutate(raw):
        raw["ha"]["integrations"][0]["entities"]["mode"] = {"domain": "select", "unique_id_regex": "^x-ems-"}
    reg = SimpleNamespace(entities={e["entity_id"]: SimpleNamespace(
        entity_id=e["entity_id"], platform=e.get("platform", "goodwe"), unique_id=e["uid"],
        disabled_by=e.get("disabled_by"), unit_of_measurement=None) for e in entries})
    hass = SimpleNamespace(data={"entity_registry": reg}, states=SimpleNamespace(get=lambda eid: None))
    monkeypatch.setattr(rt_mod, "control_verified", lambda *_: verified)
    return rt_mod, hass, ProfileChoice(directional_profile(mutate), "goodwe", "X")


MODE = {"entity_id": "select.x_ems", "uid": "x-ems-1"}
SWITCH = {"entity_id": "switch.x_remote", "uid": "x-remote_ems-1"}


def test_gaps_name_the_disabled_control_entity(monkeypatch):
    rt_mod, hass, choice = _gap_world([MODE, {**SWITCH, "disabled_by": "user"}], monkeypatch)
    gaps = rt_mod.control_entity_gaps(hass, choice, {"mode": "select.x_ems"})
    assert gaps.disabled == ("switch.x_remote",) and gaps.missing == () and gaps.recovered == ()


def test_gaps_integration_disabled_entity_counts_too(monkeypatch):
    rt_mod, hass, choice = _gap_world([MODE, {**SWITCH, "disabled_by": "integration"}], monkeypatch)
    assert rt_mod.control_entity_gaps(hass, choice, {"mode": "select.x_ems"}).disabled == ("switch.x_remote",)


def test_gaps_list_the_setting_whose_entity_does_not_exist(monkeypatch):
    # Integracja w trybie tylko do odczytu: encji przełącznika nie ma w rejestrze.
    rt_mod, hass, choice = _gap_world([MODE], monkeypatch)
    gaps = rt_mod.control_entity_gaps(hass, choice, {"mode": "select.x_ems"})
    assert gaps.missing == ("enable",) and gaps.disabled == ()


def test_gaps_clear_once_the_entity_is_enabled_and_mapped(monkeypatch):
    rt_mod, hass, choice = _gap_world([MODE, SWITCH], monkeypatch)
    gaps = rt_mod.control_entity_gaps(hass, choice, {"mode": "select.x_ems", "enable": "switch.x_remote"})
    assert (gaps.disabled, gaps.missing, gaps.recovered) == ((), (), ())


def test_gaps_enabled_but_not_yet_mapped_is_recovered_not_missing(monkeypatch):
    rt_mod, hass, choice = _gap_world([MODE, SWITCH], monkeypatch)
    gaps = rt_mod.control_entity_gaps(hass, choice, {"mode": "select.x_ems"})
    assert (gaps.disabled, gaps.missing, gaps.recovered) == ((), (), ("enable",))


def test_gaps_ignore_other_integrations_and_unverified_profiles(monkeypatch):
    rt_mod, hass, choice = _gap_world([MODE, {**SWITCH, "platform": "other", "disabled_by": "user"}], monkeypatch)
    assert rt_mod.control_entity_gaps(hass, choice, {"mode": "select.x_ems"}).disabled == ()
    rt_mod, hass, choice = _gap_world([], monkeypatch, verified=False)
    assert not rt_mod.control_entity_gaps(hass, choice, {})
    assert not rt_mod.control_entity_gaps(hass, None, {})


def test_gaps_only_cover_required_keys(monkeypatch):
    # Nastawa mocy bez encji to „nieobsługiwana nastawa" (osobne zgłoszenie), nie brak sterowania.
    rt_mod, hass, choice = _gap_world([MODE, SWITCH], monkeypatch)
    gaps = rt_mod.control_entity_gaps(hass, choice, {"mode": "select.x_ems", "enable": "switch.x_remote"})
    assert not gaps


@pytest.mark.asyncio
async def test_assignment_fix_opened_after_the_inverter_got_recognised_proposes_nothing(monkeypatch):
    hass, entry, updates, repairs = _assignment_world(monkeypatch, options={})
    hass.data["volcast"]["e1"]["control"] = SimpleNamespace(choice=SimpleNamespace(integration_domain="goodwe"), direct=None)
    flow, shown, first = await _open(hass, repairs)
    assert first["type"] == "form"
    assert all(v is None for v in _suggested(shown).values())
    assert shown["description_placeholders"] == {"candidates": "", "sign_check": ""}
    assert updates == []


@pytest.mark.asyncio
@pytest.mark.parametrize("control", [
    SimpleNamespace(choice=None, direct=None, announced_choice=("acme-hybrid", "acme")),   # profil czeka na przeładowanie
    SimpleNamespace(choice=SimpleNamespace(integration_domain="goodwe"), direct=None),    # integracja rozpoznana
    SimpleNamespace(choice=None, direct=object()),                                          # połączenie bezpośrednie
])
async def test_assignment_fix_saves_nothing_once_an_inverter_is_recognised(monkeypatch, control):
    hass, entry, updates, repairs = _assignment_world(monkeypatch)
    flow, shown, first = await _open(hass, repairs)
    assert first["type"] == "form"
    hass.data["volcast"]["e1"]["control"] = control        # rozpoznanie pojawiło się po otwarciu naprawy
    done = await flow.async_step_assign({"pv_power_w": "sensor.pv_now"})
    assert done == {"type": "abort", "reason": "inverter_recognised"} and updates == []
