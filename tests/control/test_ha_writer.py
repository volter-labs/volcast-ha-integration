import asyncio

import voluptuous as vol
from homeassistant.exceptions import HomeAssistantError, ServiceNotFound, ServiceValidationError

from custom_components.volcast.control.ha_writer import WRITE_TIMEOUT_S, EntityServiceWriter
from custom_components.volcast.core.entity_map import EntityWrite
from custom_components.volcast.core.write_sequence import DENIED, ERROR, OK, UNSUPPORTED

from .ha_fakes import GOODWE_ENTITIES, FakeContext, FakeHass, goodwe_hass

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


# ── odczyt zwrotny po zapisie (profile z przełącznikiem / mocą wg kierunku) ───────────────────────

SWITCH, SELECT, CHARGE = "switch.remote_ems", "select.ems_mode", "number.charge_limit"
LIMIT = 0.2


def _remote_hass(*, select_state="auto"):
    """Encje marki z przełącznikiem zdalnego sterowania: switch, select trybu, nastawa w kW."""
    h = FakeHass()
    h.states.set(SWITCH, "off", {})
    h.states.set(SELECT, select_state, {"options": ["auto", "charge_battery", "sell_power"]})
    h.states.set(CHARGE, "0.0", {"min": 0, "max": 12, "step": 0.01, "unit_of_measurement": "kW"})
    return h


def _rw(key, eid, domain, service, data, limit=LIMIT):
    return EntityWrite(key, eid, domain, service, data, readback_s=limit)


def _writer(h):
    return EntityServiceWriter(h, readback_poll_s=0.01)


def test_read_back_ok_for_switch_number_and_select():
    h = _remote_hass()
    wr = _writer(h)
    assert asyncio.run(wr.async_write(_rw("charge_power_w", CHARGE, "number", "set_value", {"value": 2.5}))) == OK
    assert asyncio.run(wr.async_write(_rw("enable", SWITCH, "switch", "turn_on", {}))) == OK
    assert asyncio.run(wr.async_write(_rw("mode", SELECT, "select", "select_option",
                                          {"option": "charge_battery"}))) == OK
    assert [(c[0], c[1], c[2]) for c in h.services.calls] == [
        ("number", "set_value", {"entity_id": CHARGE, "value": 2.5}),
        ("switch", "turn_on", {"entity_id": SWITCH}),
        ("select", "select_option", {"entity_id": SELECT, "option": "charge_battery"})]


def test_read_back_waits_for_late_state():
    """Integracja odświeża stan dopiero po chwili (następne odpytanie) — czekamy w limicie."""
    h = _remote_hass()

    async def run():
        loop = asyncio.get_running_loop()
        orig = h.services.async_call

        async def late(domain, service, data, blocking=False, context=None):
            h.services.calls.append((domain, service, dict(data), context))
            loop.call_later(0.05, lambda: h.states.set(SWITCH, "on", context=context))
        h.services.async_call = late
        out = await _writer(h).async_write(_rw("enable", SWITCH, "switch", "turn_on", {}))
        h.services.async_call = orig
        return out
    assert asyncio.run(run()) == OK


def test_read_back_mismatch_is_error_with_reason_and_actual():
    from custom_components.volcast.control.ha_writer import MISMATCH
    h = _remote_hass()

    async def clamped(domain, service, data, blocking=False, context=None):
        h.services.calls.append((domain, service, dict(data), context))
        h.states.set(CHARGE, "2.0", context=context)        # urządzenie ustawiło inną wartość
    h.services.async_call = clamped
    out = asyncio.run(_writer(h).async_write(_rw("charge_power_w", CHARGE, "number", "set_value",
                                                 {"value": 2.5})))
    assert out == ERROR and out.reason == MISMATCH and out.actual == "2.0"


def test_read_back_without_change_is_error_timeout():
    from custom_components.volcast.control.ha_writer import TIMEOUT
    h = _remote_hass()

    async def ignored(domain, service, data, blocking=False, context=None):
        h.services.calls.append((domain, service, dict(data), context))   # stan bez zmian
    h.services.async_call = ignored
    out = asyncio.run(_writer(h).async_write(_rw("enable", SWITCH, "switch", "turn_on", {})))
    assert out == ERROR and out.reason == TIMEOUT and out.actual == "off"
    assert len(h.services.calls) == 1                   # jedna próba zapisu, bez ponowień


def test_read_back_entity_gone_after_write_is_timeout():
    from custom_components.volcast.control.ha_writer import TIMEOUT
    h = _remote_hass()

    async def gone(domain, service, data, blocking=False, context=None):
        h.states.set(SWITCH, "unavailable", context=context)
    h.services.async_call = gone
    out = asyncio.run(_writer(h).async_write(_rw("enable", SWITCH, "switch", "turn_on", {})))
    assert out == ERROR and out.reason == TIMEOUT


def test_select_available_only_after_enable_is_awaited():
    """Select trybu bywa niedostępny, dopóki przełącznik zdalnego sterowania jest wyłączony."""
    from custom_components.volcast.core.write_sequence import async_run_writes
    h = _remote_hass(select_state="unavailable")

    async def run():
        loop = asyncio.get_running_loop()
        base = h.services.async_call

        async def integration(domain, service, data, blocking=False, context=None):
            await base(domain, service, data, blocking, context)
            if data["entity_id"] == SWITCH and service == "turn_on":
                loop.call_later(0.05, lambda: h.states.set(SELECT, "auto"))
        h.services.async_call = integration
        writes = [_rw("charge_power_w", CHARGE, "number", "set_value", {"value": 2.5}),
                  _rw("enable", SWITCH, "switch", "turn_on", {}),
                  _rw("mode", SELECT, "select", "select_option", {"option": "charge_battery"})]
        return await async_run_writes(writes, _writer(h).async_write)
    rep = asyncio.run(run())
    assert rep.written == ["charge_power_w", "enable", "mode"] and not rep.failed
    assert [c[1] for c in h.services.calls] == ["set_value", "turn_on", "select_option"]
    assert h.states.get(SELECT).state == "charge_battery"


def test_entity_unavailable_beyond_limit_is_error_without_call():
    h = _remote_hass(select_state="unavailable")
    out = asyncio.run(_writer(h).async_write(_rw("mode", SELECT, "select", "select_option",
                                                 {"option": "charge_battery"})))
    assert out == ERROR and h.services.calls == []


def test_unknown_option_after_waiting_is_unsupported_without_call():
    h = _remote_hass()
    out = asyncio.run(_writer(h).async_write(_rw("mode", SELECT, "select", "select_option",
                                                 {"option": "eco"})))
    assert out == UNSUPPORTED and h.services.calls == []


def test_without_read_back_writer_does_not_wait_or_compare():
    """Regresja: zapis bez `readback_s` (GoodWe, odbiorniki) — OK po samej usłudze, jak dotąd."""
    h = goodwe_hass()

    async def ignored(domain, service, data, blocking=False, context=None):
        h.services.calls.append((domain, service, dict(data), context))
    h.services.async_call = ignored
    out = asyncio.run(EntityServiceWriter(h).async_write(
        _w("power_w", POWER, "number", "set_value", {"value": 5.0})))
    assert out == OK and type(out) is str
