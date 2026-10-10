"""Opcje „Sterowanie falownikiem": rekomendacja z uzasadnieniem i ręczna mapa encji (prawdziwy HA)."""
from __future__ import annotations

import json
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.volcast.const import OPT_ENTITY_MAP
from custom_components.volcast.core.control.recommend import (
    ENTITIES, INTEGRATION_WRITE_ENTITIES, Recommendation)

from .conftest import ENTITY_OPTIONS, control_of, make_entry, setup_entry, store_state
from .inverter import async_setup_inverter

GATES = {"consent": True, "local_switch": True}

COMPONENT = Path(__file__).parents[1] / "custom_components" / "volcast"


async def _open_control(hass, entry):
    menu = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(menu["flow_id"], {"next_step_id": "control"})


async def _setup(hass, hass_storage):
    await async_setup_inverter(hass)
    store_state(hass_storage, "paired01", dict(GATES))
    entry = make_entry(hass, options=ENTITY_OPTIONS)
    await setup_entry(hass, entry)
    return entry, control_of(hass, entry)


async def test_recommended_option_is_marked_with_reason(hass: HomeAssistant, network_down, hass_storage):
    entry, rt = await _setup(hass, hass_storage)
    rt.recommendation = Recommendation(path=ENTITIES, reason=INTEGRATION_WRITE_ENTITIES, ladder_start=4)
    menu = await _open_control(hass, entry)
    assert menu["type"] is FlowResultType.MENU
    assert menu["step_id"] == "control_integration_write_entities"
    assert {"control_entities", "control_direct", "control_off", "entity_map"} <= set(menu["menu_options"])
    en = json.loads((COMPONENT / "strings.json").read_text("utf-8"))["options"]["step"][menu["step_id"]]
    labels = en["menu_options"]
    assert "(recommended)" in labels["control_entities"]
    assert "(recommended)" not in labels["control_direct"] and "(recommended)" not in labels["control_off"]
    assert en["description"]


async def test_no_recommendation_keeps_plain_menu(hass: HomeAssistant, network_down, hass_storage):
    entry, rt = await _setup(hass, hass_storage)
    rt.recommendation = None
    menu = await _open_control(hass, entry)
    assert menu["step_id"] == "control"


async def test_entity_map_override_wins_over_regex(hass: HomeAssistant, network_down, hass_storage):
    entry, rt = await _setup(hass, hass_storage)
    auto_mode = rt.mapped["mode"]
    hass.states.async_set("select.my_mode", "auto", {"options": ["auto", "sell_power"]})
    menu = await _open_control(hass, entry)
    form = await hass.config_entries.options.async_configure(menu["flow_id"], {"next_step_id": "entity_map"})
    assert form["type"] is FlowResultType.FORM and form["step_id"] == "entity_map"
    defaults = {k.schema: (k.description or {}).get("suggested_value") for k in form["data_schema"].schema}
    assert defaults["mode"] == auto_mode
    done = await hass.config_entries.options.async_configure(form["flow_id"], {"mode": "select.my_mode"})
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[OPT_ENTITY_MAP] == {"mode": "select.my_mode"}
    await hass.async_block_till_done()
    assert control_of(hass, entry).mapped["mode"] == "select.my_mode"
    assert control_of(hass, entry).mapped["power_w"]      # reszta dalej z wzorców
