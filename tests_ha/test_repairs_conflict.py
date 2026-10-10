"""Naprawy: konflikt sterowników, zatrzymana weryfikacja, nieudany wybór z aplikacji (prawdziwy HA)."""
from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
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
