"""Pisarz usług na prawdziwej szynie usług HA."""
import asyncio

import pytest
import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.volcast.control.ha_writer import EntityServiceWriter
from custom_components.volcast.core.entity_map import EntityWrite
from custom_components.volcast.core.write_sequence import DENIED, ERROR, OK, UNSUPPORTED
from custom_components.volcast.core.write_sequence import async_run_writes


async def test_writer_calls_service_with_own_context(hass: HomeAssistant):
    calls = []

    async def handler(call):
        calls.append(call)
    hass.services.async_register("select", "select_option", handler)
    hass.states.async_set("select.ems", "auto", {"options": ["auto", "sell_power"]})
    w = EntityServiceWriter(hass)
    out = await w.async_write(EntityWrite("mode", "select.ems", "select", "select_option", {"option": "sell_power"}))
    assert out == OK and calls[0].data == {"entity_id": "select.ems", "option": "sell_power"}
    assert w.is_ours(calls[0].context.id)


async def test_missing_service_is_unsupported(hass: HomeAssistant):
    hass.states.async_set("number.p", "0", {"min": 0, "max": 10})
    out = await EntityServiceWriter(hass).async_write(EntityWrite("power_w", "number.p", "number", "set_value",
                                                                  {"value": 1.0}))
    assert out == UNSUPPORTED


def _num(hass, handler, schema=None):
    hass.services.async_register("number", "set_value", handler, schema=schema)
    hass.states.async_set("number.p", "0", {"min": 0, "max": 10})
    return EntityWrite("power_w", "number.p", "number", "set_value", {"value": 1.0})


async def test_validation_error_is_denied(hass: HomeAssistant):
    async def handler(call):
        raise ServiceValidationError("out of range")
    w = _num(hass, handler)
    assert await EntityServiceWriter(hass).async_write(w) == DENIED


async def test_schema_rejection_is_denied(hass: HomeAssistant):
    async def handler(call):
        raise AssertionError("schema must reject first")
    w = _num(hass, handler, schema=vol.Schema({"entity_id": str, "value": vol.All(float, vol.Range(max=0.5))}))
    assert await EntityServiceWriter(hass).async_write(w) == DENIED


async def test_ha_error_is_error(hass: HomeAssistant):
    async def handler(call):
        raise HomeAssistantError("inverter did not answer")
    assert await EntityServiceWriter(hass).async_write(_num(hass, handler)) == ERROR


async def test_non_ha_exception_escapes_writer_and_sequence_reports_class(hass: HomeAssistant):
    """W prawdziwym HA wyjątek spoza HomeAssistantError wychodzi z pisarza — łapie go sekwencja."""
    async def handler(call):
        raise RuntimeError("library bug")
    w = _num(hass, handler)
    writer = EntityServiceWriter(hass)
    with pytest.raises(RuntimeError):
        await writer.async_write(w)
    rep = await async_run_writes([w], writer.async_write)
    assert rep.errors == {"power_w": "RuntimeError"}


async def test_timeout_cancels_the_service_handler(hass: HomeAssistant):
    """Limit czasu anuluje zapis w toku: wywołanie blokujące biegnie w korutynie pisarza."""
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def handler(call):
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
    w = _num(hass, handler)
    out = await EntityServiceWriter(hass, timeout_s=0.05).async_write(w)
    assert out == ERROR and started.is_set() and cancelled.is_set()


async def test_unknown_select_option_is_unsupported_without_call(hass: HomeAssistant):
    calls = []

    async def handler(call):
        calls.append(call)
    hass.services.async_register("select", "select_option", handler)
    hass.states.async_set("select.ems", "auto", {"options": ["auto"]})
    out = await EntityServiceWriter(hass).async_write(
        EntityWrite("mode", "select.ems", "select", "select_option", {"option": "sell_power"}))
    assert out == UNSUPPORTED and calls == []


async def test_state_change_carries_our_context(hass: HomeAssistant):
    """Integracja odświeżająca stan z kontekstem wywołania — zmiana rozpoznana jako nasza."""
    seen = []

    async def handler(call):
        hass.states.async_set("select.ems", call.data["option"], {"options": ["auto", "sell_power"]},
                              context=call.context)
    hass.services.async_register("select", "select_option", handler)
    hass.states.async_set("select.ems", "auto", {"options": ["auto", "sell_power"]})
    hass.bus.async_listen("state_changed", lambda e: seen.append(e))
    w = EntityServiceWriter(hass)
    assert await w.async_write(EntityWrite("mode", "select.ems", "select", "select_option",
                                           {"option": "sell_power"})) == OK
    await hass.async_block_till_done()
    ev = [e for e in seen if e.data["entity_id"] == "select.ems"][-1]
    assert w.is_ours(ev.data["new_state"].context.id)
    assert ev.data["new_state"].context.user_id is None and ev.data["new_state"].context.parent_id is None
