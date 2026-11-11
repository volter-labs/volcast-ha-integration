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


# ── odczyt zwrotny: przełącznik zdalnego sterowania, nastawa w kW, select dostępny po włączeniu ──

_OPTS = {"options": ["auto", "charge_battery"]}


def _remote_entities(hass: HomeAssistant, *, select_state: str = "unavailable", clamp_kw: float | None = None):
    """Integracja z przełącznikiem: select trybu niedostępny, dopóki przełącznik jest wyłączony."""
    hass.states.async_set("switch.remote", "off")
    hass.states.async_set("select.ems", select_state, _OPTS)
    hass.states.async_set("number.charge", "0.0", {"min": 0, "max": 12, "step": 0.01,
                                                   "unit_of_measurement": "kW"})

    async def switch_on(call):
        hass.states.async_set("switch.remote", "on", context=call.context)
        hass.loop.call_later(0.05, lambda: hass.states.async_set("select.ems", "auto", _OPTS))

    async def select_option(call):
        hass.states.async_set("select.ems", call.data["option"], _OPTS, context=call.context)

    async def set_value(call):
        value = call.data["value"] if clamp_kw is None else clamp_kw
        hass.states.async_set("number.charge", str(value), context=call.context)
    hass.services.async_register("switch", "turn_on", switch_on)
    hass.services.async_register("select", "select_option", select_option)
    hass.services.async_register("number", "set_value", set_value)


def _remote_writes(limit: float = 1.0) -> list[EntityWrite]:
    return [EntityWrite("charge_power_w", "number.charge", "number", "set_value", {"value": 2.5}, readback_s=limit),
            EntityWrite("enable", "switch.remote", "switch", "turn_on", {}, readback_s=limit),
            EntityWrite("mode", "select.ems", "select", "select_option", {"option": "charge_battery"},
                        readback_s=limit)]


async def test_remote_switch_sequence_waits_for_mode_select(hass: HomeAssistant):
    _remote_entities(hass)
    rep = await async_run_writes(_remote_writes(), EntityServiceWriter(hass, readback_poll_s=0.01).async_write)
    assert rep.written == ["charge_power_w", "enable", "mode"] and not rep.failed
    assert hass.states.get("select.ems").state == "charge_battery"
    assert hass.states.get("number.charge").state == "2.5"


async def test_set_point_mismatch_holds_the_mode(hass: HomeAssistant):
    from custom_components.volcast.control.ha_writer import MISMATCH
    _remote_entities(hass, clamp_kw=2.0)
    writer = EntityServiceWriter(hass, readback_poll_s=0.01)
    out = await writer.async_write(_remote_writes(0.1)[0])
    assert out == ERROR and out.reason == MISMATCH and out.actual == "2.0"
    rep = await async_run_writes(_remote_writes(0.1), writer.async_write)
    assert rep.failed == ["charge_power_w"] and rep.held == ["enable"] and rep.mode_held
    assert hass.states.get("switch.remote").state == "off"           # przełącznik nie włączony
    assert hass.states.get("select.ems").state == "unavailable"


async def test_hand_back_switch_off_is_sent_after_failed_set_point(hass: HomeAssistant):
    from custom_components.volcast.core.control.group_writes import async_run_group_writes
    _remote_entities(hass, clamp_kw=2.0)
    hass.states.async_set("switch.remote", "on")
    offs = []

    async def switch_off(call):
        offs.append(call)
        hass.states.async_set("switch.remote", "off", context=call.context)
    hass.services.async_register("switch", "turn_off", switch_off)
    writes = [EntityWrite("charge_power_w", "number.charge", "number", "set_value", {"value": 0.0}, readback_s=0.1),
              EntityWrite("enable", "switch.remote", "switch", "turn_off", {}, readback_s=0.1)]
    rep = await async_run_group_writes(writes, EntityServiceWriter(hass, readback_poll_s=0.01).async_write)
    assert rep.failed == ["charge_power_w"] and "enable" in rep.written and len(offs) == 1
    assert hass.states.get("switch.remote").state == "off"


async def test_select_never_available_is_error_without_call(hass: HomeAssistant):
    calls = []
    hass.states.async_set("select.ems", "unavailable", _OPTS)

    async def handler(call):
        calls.append(call)
    hass.services.async_register("select", "select_option", handler)
    out = await EntityServiceWriter(hass, readback_poll_s=0.01).async_write(_remote_writes(0.1)[2])
    assert out == ERROR and calls == []
