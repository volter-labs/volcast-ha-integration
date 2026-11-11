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
