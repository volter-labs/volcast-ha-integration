"""Naprawy: konflikt sterowników, zatrzymana weryfikacja, nieudany wybór z aplikacji (prawdziwy HA)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from custom_components.volcast import repairs
from custom_components.volcast.const import DOMAIN

from .conftest import ENTITY_OPTIONS, control_of, make_entry, setup_entry, store_state
from .inverter import async_setup_inverter

GATES = {"consent": True, "local_switch": True}


async def _setup(hass, hass_storage):
    await async_setup_inverter(hass)
    store_state(hass_storage, "paired01", dict(GATES))
    entry = make_entry(hass, options=ENTITY_OPTIONS)
    await setup_entry(hass, entry)
    return entry, control_of(hass, entry)


async def _flow(hass, issue_id, entry):
    flow = await repairs.async_create_fix_flow(hass, issue_id, {"entry_id": entry.entry_id})
    flow.hass = hass
    flow.issue_id = issue_id
    flow.handler = DOMAIN
    return flow


async def test_conflict_repair_own_ems_sets_plan_only(hass: HomeAssistant, network_down, hass_storage):
    entry, rt = await _setup(hass, hass_storage)
    flow = await _flow(hass, f"controller_conflict_{entry.entry_id}", entry)
    menu = await flow.async_step_init()
    assert menu["type"] is FlowResultType.MENU and set(menu["menu_options"]) == {"volcast", "own_ems"}
    done = await flow.async_step_own_ems()
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert rt.executor.plan_only is True


async def test_conflict_repair_volcast_applies_choice(hass: HomeAssistant, network_down, hass_storage):
    entry, rt = await _setup(hass, hass_storage)
    rt.async_apply_controller_choice = AsyncMock(return_value="applied")
    flow = await _flow(hass, f"controller_conflict_{entry.entry_id}", entry)
    done = await flow.async_step_volcast()
    assert done["type"] is FlowResultType.CREATE_ENTRY
    rt.async_apply_controller_choice.assert_awaited_once_with("volcast")


async def test_verification_stopped_repair_retries(hass: HomeAssistant, network_down, hass_storage):
    entry, rt = await _setup(hass, hass_storage)
    rt.verification.async_retry = AsyncMock()
    flow = await _flow(hass, f"verification_stopped_{entry.entry_id}", entry)
    shown = await flow.async_step_init()
    assert shown["type"] is FlowResultType.FORM and shown["step_id"] == "confirm"
    done = await flow.async_step_confirm({})
    assert done["type"] is FlowResultType.CREATE_ENTRY
    rt.verification.async_retry.assert_awaited_once()


async def test_choice_failed_issue_cleared_by_later_choice(hass: HomeAssistant, network_down, hass_storage):
    entry, rt = await _setup(hass, hass_storage)
    rt.report_choice_error()
    issue_id = f"control_choice_failed_{entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    assert await rt.async_apply_controller_choice("volcast") == "applied"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


MODE_ENTITY = "select.goodwe_ems_mode"


def _issue(hass, entry, prefix):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{prefix}_{entry.entry_id}")


async def _setup_without_mode(hass, hass_storage, *, remove: bool):
    await async_setup_inverter(hass)
    reg = er.async_get(hass)
    if remove:
        reg.async_remove(MODE_ENTITY)
    else:
        reg.async_update_entity(MODE_ENTITY, disabled_by=er.RegistryEntryDisabler.USER)
    await hass.async_block_till_done()
    store_state(hass_storage, "paired01", dict(GATES))
    entry = make_entry(hass, options=ENTITY_OPTIONS)
    await setup_entry(hass, entry)
    return entry, control_of(hass, entry), reg


async def test_disabled_control_entity_raises_issue_and_clears_after_enabling(
        hass: HomeAssistant, network_down, hass_storage):
    entry, rt, reg = await _setup_without_mode(hass, hass_storage, remove=False)
    await rt.executor.async_tick()
    issue = _issue(hass, entry, "control_entities_disabled")
    assert issue is not None and issue.translation_key == "control_entities_disabled"
    assert issue.translation_placeholders == {"entities": MODE_ENTITY}
    assert _issue(hass, entry, "control_entities_missing") is None
    rt.executor.request_reload = MagicMock()
    reg.async_update_entity(MODE_ENTITY, disabled_by=None)
    await rt.executor.async_tick()
    assert _issue(hass, entry, "control_entities_disabled") is None
    rt.executor.request_reload.assert_called_once()                # mapowanie dopiero po przeładowaniu


async def test_absent_control_entity_raises_issue_and_clears_when_it_appears(
        hass: HomeAssistant, network_down, hass_storage):
    entry, rt, reg = await _setup_without_mode(hass, hass_storage, remove=True)
    await rt.executor.async_tick()
    issue = _issue(hass, entry, "control_entities_missing")
    assert issue is not None and issue.translation_placeholders == {"entities": "mode"}
    assert _issue(hass, entry, "control_entities_disabled") is None
    reg.async_get_or_create("select", "goodwe", "goodwe-ems_mode-" + "X", suggested_object_id="goodwe_ems_mode")
    rt.executor.request_reload = MagicMock()
    await rt.executor.async_tick()
    assert _issue(hass, entry, "control_entities_missing") is None
