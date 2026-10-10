"""Sensor `verification`: stan drabiny weryfikacji urządzenia (prawdziwy HA)."""
from __future__ import annotations

import homeassistant.util.dt as dt_util
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send

from custom_components.volcast.const import DOMAIN, SIGNAL_CONTROL_STATE_UPDATED
from custom_components.volcast.core.control.ladder import STOPPED, VERIFIED

from .conftest import ENTITY_OPTIONS, control_of, make_entry, setup_entry, store_state
from .inverter import async_setup_inverter

GATES = {"consent": True, "local_switch": True}


def _entity_id(hass, entry) -> str:
    return er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_control_verification")


async def test_sensor_reflects_ladder_state(hass: HomeAssistant, network_down, hass_storage):
    await async_setup_inverter(hass)
    store_state(hass_storage, "paired01", dict(GATES))
    entry = make_entry(hass, options=ENTITY_OPTIONS)
    await setup_entry(hass, entry)
    rt = control_of(hass, entry)
    eid = _entity_id(hass, entry)
    assert eid
    lad = rt.verification.ladder
    assert lad is not None
    signal = SIGNAL_CONTROL_STATE_UPDATED.format(entry_id=entry.entry_id)

    lad.state.state, lad.state.rung = "running", 3
    async_dispatcher_send(hass, signal)
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == "rung 3/5"

    lad.state.state, lad.state.since = VERIFIED, dt_util.utcnow()
    async_dispatcher_send(hass, signal)
    await hass.async_block_till_done()
    st = hass.states.get(eid)
    assert st.state == "verified" and st.attributes["state"] == "verified" and "since" in st.attributes

    lad.state.state, lad.state.stop_reason = STOPPED, "user_abort"
    async_dispatcher_send(hass, signal)
    await hass.async_block_till_done()
    st = hass.states.get(eid)
    assert st.state == "stopped" and st.attributes["stop_reason"] == "user_abort"
