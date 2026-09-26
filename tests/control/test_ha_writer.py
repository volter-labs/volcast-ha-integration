import asyncio

import voluptuous as vol
from homeassistant.exceptions import HomeAssistantError, ServiceNotFound, ServiceValidationError

from custom_components.volcast.control.ha_writer import WRITE_TIMEOUT_S, EntityServiceWriter
from custom_components.volcast.core.entity_map import EntityWrite
from custom_components.volcast.core.write_sequence import DENIED, ERROR, OK, UNSUPPORTED

from .ha_fakes import GOODWE_ENTITIES, FakeContext, goodwe_hass

MODE = GOODWE_ENTITIES["mode"]
POWER = GOODWE_ENTITIES["power_w"]


def _w(key, eid, domain, service, data):
    return EntityWrite(key, eid, domain, service, data)


def test_ok_sets_state_with_own_context():
    h = goodwe_hass()
    wr = EntityServiceWriter(h)
    out = asyncio.run(wr.async_write(_w("mode", MODE, "select", "select_option", {"option": "sell_power"})))
    assert out == OK and h.states.get(MODE).state == "sell_power"
    domain, service, data, ctx = h.services.calls[0]
    assert data == {"entity_id": MODE, "option": "sell_power"} and wr.is_ours(ctx.id)
    assert (domain, service) == ("select", "select_option")
    assert h.states.get(MODE).context is ctx          # stan po zapisie niesie nasz kontekst


def test_call_is_blocking():
    h = goodwe_hass()
    asyncio.run(EntityServiceWriter(h).async_write(_w("power_w", POWER, "number", "set_value", {"value": 5.0})))
    assert h.services.blocking == [True]


def test_every_call_gets_its_own_context_from_factory():
    h = goodwe_hass()
    made = []

    def factory():
        made.append(FakeContext())
        return made[-1]

    wr = EntityServiceWriter(h, context_factory=factory)
    w = _w("power_w", POWER, "number", "set_value", {"value": 5.0})
    asyncio.run(wr.async_write(w))
    asyncio.run(wr.async_write(w))
    assert [c[3] for c in h.services.calls] == made and made[0].id != made[1].id
    assert all(wr.is_ours(c.id) for c in made)


def test_slow_service_times_out_as_error():
    assert WRITE_TIMEOUT_S == 10.0
    h = goodwe_hass()
    h.services.delay_s = 1.0
    out = asyncio.run(EntityServiceWriter(h, timeout_s=0.01).async_write(
        _w("power_w", POWER, "number", "set_value", {"value": 5.0})))
    assert out == ERROR and h.states.get(POWER).state == "0"


def test_option_outside_entity_options_is_unsupported_without_call():
    h = goodwe_hass()
    out = asyncio.run(EntityServiceWriter(h).async_write(
        _w("mode", MODE, "select", "select_option", {"option": "eco_charge"})))
    assert out == UNSUPPORTED and h.services.calls == []


def test_unavailable_entity_is_error_without_call():
    h = goodwe_hass()
    h.states.set(POWER, "unavailable")
    out = asyncio.run(EntityServiceWriter(h).async_write(
        _w("power_w", POWER, "number", "set_value", {"value": 100.0})))
    assert out == ERROR and h.services.calls == []


def test_missing_or_unknown_entity_is_error_without_call():
    h = goodwe_hass()
    h.states.set(POWER, "unknown")
    wr = EntityServiceWriter(h)
    assert asyncio.run(wr.async_write(_w("power_w", POWER, "number", "set_value", {"value": 1.0}))) == ERROR
    assert asyncio.run(wr.async_write(_w("x", "number.gone", "number", "set_value", {"value": 1.0}))) == ERROR
    assert h.services.calls == []


def test_exception_mapping():
    h = goodwe_hass()
    wr = EntityServiceWriter(h)
    w = _w("power_w", POWER, "number", "set_value", {"value": 100.0})
    for exc, expected in ((ServiceNotFound("x"), UNSUPPORTED), (ServiceValidationError("x"), DENIED),
                          (vol.Invalid("x"), DENIED), (HomeAssistantError("x"), ERROR),
                          (asyncio.TimeoutError(), ERROR)):
        h.services.fail[POWER] = exc
        assert asyncio.run(wr.async_write(w)) == expected


def test_foreign_context_is_not_ours():
    wr = EntityServiceWriter(goodwe_hass())
    assert wr.is_ours("someone-else") is False and wr.is_ours(None) is False
