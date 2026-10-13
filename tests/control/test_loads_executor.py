"""Wykonawca slotów ładowarki EV: kolejność zapisów, bramki, throttling, magazyn."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.volcast.control.ha_writer import EntityServiceWriter
from custom_components.volcast.control.loads_executor import (
    LoadsExecutor, charger_roles, is_charging, pick_option)
from custom_components.volcast.control.loads_store import LoadsState, LoadsStore
from custom_components.volcast.core.ev.model import LoadSlot, LoadsMeta

from .ha_fakes import FakeHass

NOW = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
GEN = datetime(2026, 9, 23, 9, 55, tzinfo=timezone.utc)
STATUS, CURRENT, START_STOP = "sensor.wb_status", "number.wb_current", "select.wb_start_stop"
SWITCH = "switch.wb_charging"
OPTIONS = ["Start charging", "Stop charging", "No operation"]
ROLES = {"status": STATUS, "setpoint": CURRENT, "start_stop": START_STOP}
INVERTER = "number.goodwe_ems_power_limit"


class Clock:
    def __init__(self):
        self.mono = 1000.0
        self.utc = NOW

    def advance(self, s: float) -> None:
        self.mono += s
        self.utc += timedelta(seconds=s)


def _hass(status="Available", roles=ROLES, switch_state="off"):
    h = FakeHass()
    h.states.set(STATUS, status, {"options": ["Available", "Preparing", "Charging", "Finished"]})
    h.states.set(CURRENT, "6", {"min": 6, "max": 16, "step": 1, "unit_of_measurement": "A"})
    h.states.set(START_STOP, "No operation", {"options": OPTIONS})
    h.states.set(SWITCH, switch_state, {})
    h.states.set(INVERTER, "0", {"min": 0, "max": 10000, "step": 1, "unit_of_measurement": "W"})
    h.test_roles = roles
    # rejestr encji: encje ładowarki należą do jej urządzenia, falownik do swojego
    charger = (STATUS, CURRENT, START_STOP, SWITCH, "sensor.wb_current")
    h.data["entity_registry"] = SimpleNamespace(entities={
        **{e: SimpleNamespace(entity_id=e, device_id="dev1") for e in charger},
        INVERTER: SimpleNamespace(entity_id=INVERTER, device_id="inv1")})
    return h


def _entry(roles=ROLES):
    return SimpleNamespace(entry_id="e1", options={"ev_chargers": [
        {"device_id": "dev1", "label": "Wallbox", "roles": roles}]})


def _slot(mode="forced", power=11100.0, start=NOW - timedelta(minutes=5), minutes=30, device="dev1",
          session="s1"):
    return LoadSlot(device_id=device, start=start, end=start + timedelta(minutes=minutes), mode=mode,
                    power_w=power, min_power_w=None, session_id=session, battery_guard="bypass")


def _meta(control=True, gen=GEN, phases=3, **kw):
    base = dict(session_id="s1", load_key="dev1", control_enabled=control, phases=phases,
                control_unit="A", min=6.0, max=16.0, step=1.0, max_power_w=11040.0,
                plan_generated_at=gen)
    base.update(kw)
    return LoadsMeta(**base)


def _schedule(*slots, meta=None):
    return SimpleNamespace(loads=tuple(slots), loads_meta=meta if meta is not None else _meta())


def _make(hass, *, store=None, consent=True, switch=True, clock=None, roles=None, forbidden=frozenset(),
          plan_only=False):
    clock = clock or Clock()
    entry = _entry(roles or ROLES)
    ex = LoadsExecutor(hass, entry, writer=EntityServiceWriter(hass),
                       roles_provider=lambda key: charger_roles(entry.options, key),
                       store=store or LoadsStore(hass, "e1"),
                       consent_getter=lambda: consent, local_switch_getter=lambda: switch,
                       plan_only_getter=lambda: plan_only,
                       forbidden_entities=forbidden, clock=lambda: clock.mono, utcnow=lambda: clock.utc,
                       readback_s=0)
    return ex, clock


def _calls(hass):
    return [(d, s, {k: v for k, v in data.items()}) for d, s, data, _ctx in hass.services.calls]


async def _started(hass, **kw):
    ex, clock = _make(hass, **kw)
    await ex.async_start()
    return ex, clock


# ── kolejność zapisów ──────────────────────────────────────────────────────

def test_forced_writes_setpoint_before_start():
    async def body():
        h = _hass()
        ex, _ = await _started(h)
        await ex.async_on_plan(_schedule(_slot()))
        assert _calls(h) == [
            ("number", "set_value", {"entity_id": CURRENT, "value": 16.0}),
            ("select", "select_option", {"entity_id": START_STOP, "option": "Start charging"}),
        ]
        await ex.async_stop()
    asyncio.run(body())


def test_power_change_while_charging_writes_only_setpoint():
    async def body():
        h = _hass()
        ex, clock = await _started(h)
        await ex.async_on_plan(_schedule(_slot()))
        h.states.set(STATUS, "Charging")
        h.services.calls.clear()
        clock.advance(61)
        await ex.async_on_plan(_schedule(_slot(power=6900.0), meta=_meta(gen=GEN + timedelta(minutes=1))))
        assert _calls(h) == [("number", "set_value", {"entity_id": CURRENT, "value": 10.0})]
        await ex.async_stop()
    asyncio.run(body())


def test_same_power_next_tick_writes_nothing():
    async def body():
        h = _hass()
        ex, clock = await _started(h)
        await ex.async_on_plan(_schedule(_slot()))
        h.services.calls.clear()
        clock.advance(61)
        await ex.async_tick()          # ładowarka jeszcze nie ruszyła: start już wysłany, bez powtórki
        assert _calls(h) == []
        await ex.async_stop()
    asyncio.run(body())


def test_off_slot_stops_charging():
    async def body():
        h = _hass(status="Charging")
        ex, _ = await _started(h)
        await ex.async_on_plan(_schedule(_slot(mode="off", power=None)))
        assert _calls(h) == [("select", "select_option", {"entity_id": START_STOP, "option": "Stop charging"})]
        await ex.async_stop()
    asyncio.run(body())


def test_off_slot_not_charging_writes_nothing():
    async def body():
        h = _hass(status="Available")
        ex, _ = await _started(h)
        await ex.async_on_plan(_schedule(_slot(mode="off", power=None)))
        assert _calls(h) == []
        await ex.async_stop()
    asyncio.run(body())


def test_no_slot_now_writes_nothing():
    async def body():
        h = _hass(status="Charging")
        ex, _ = await _started(h)
        later = _slot(start=NOW + timedelta(hours=1))
        await ex.async_on_plan(_schedule(later))
        await ex.async_on_plan(_schedule(_slot(mode="off", start=NOW - timedelta(hours=1), minutes=60)))
        assert _calls(h) == []         # [from, to): koniec slotu = brak slotu
        await ex.async_stop()
    asyncio.run(body())


def test_slot_of_another_device_is_ignored():
    async def body():
        h = _hass()
        ex, _ = await _started(h)
        await ex.async_on_plan(_schedule(_slot(device="dev2")))
        assert _calls(h) == []
        await ex.async_stop()
    asyncio.run(body())


def test_forced_without_power_uses_max_power():
    async def body():
        h = _hass()
        ex, _ = await _started(h)
        await ex.async_on_plan(_schedule(_slot(power=None)))
        assert _calls(h)[0] == ("number", "set_value", {"entity_id": CURRENT, "value": 16.0})
        await ex.async_stop()
    asyncio.run(body())


def test_forced_below_minimum_writes_nothing():
    async def body():
        h = _hass()
        ex, _ = await _started(h)
        await ex.async_on_plan(_schedule(_slot(power=3000.0)))     # 3000 W / 3f = 4,3 A < 6 A
        assert _calls(h) == []
        await ex.async_stop()
    asyncio.run(body())


def test_failed_setpoint_write_does_not_start():
    async def body():
        from homeassistant.exceptions import HomeAssistantError
        h = _hass()
        h.services.fail[CURRENT] = HomeAssistantError("x")
        ex, _ = await _started(h)
        await ex.async_on_plan(_schedule(_slot()))
        assert [c[1] for c in _calls(h)] == ["set_value"]
        await ex.async_stop()
    asyncio.run(body())


# ── bramki ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("gate", ["consent", "switch", "control_enabled", "unavailable", "unknown"])
def test_gates_block_every_write(gate):
    async def body():
        h = _hass(status="unavailable" if gate == "unavailable" else "unknown" if gate == "unknown"
                  else "Available")
        ex, _ = await _started(h, consent=gate != "consent", switch=gate != "switch")
        await ex.async_on_plan(_schedule(_slot(), meta=_meta(control=gate != "control_enabled")))
        h.states.set(STATUS, gate if gate in ("unavailable", "unknown") else "Charging")
        await ex.async_on_plan(_schedule(_slot(mode="off", power=None),
                                         meta=_meta(control=gate != "control_enabled")))
        assert _calls(h) == []
        await ex.async_stop()
    asyncio.run(body())


def test_consent_none_blocks_writes():
    async def body():
        h = _hass()
        ex, _ = await _started(h, consent=None)
        await ex.async_on_plan(_schedule(_slot()))
        assert _calls(h) == []
        await ex.async_stop()
    asyncio.run(body())


def test_no_meta_writes_nothing():
    async def body():
        h = _hass()
        ex, _ = await _started(h)
        await ex.async_on_plan(SimpleNamespace(loads=(_slot(),), loads_meta=None))
        assert _calls(h) == []
        await ex.async_stop()
    asyncio.run(body())


def test_never_writes_inverter_entities():
    async def body():
        h = _hass()
        roles = {"status": STATUS, "setpoint": INVERTER, "start_stop": START_STOP}
        ex, _ = await _started(h, roles=roles, forbidden=frozenset({INVERTER}))
        await ex.async_on_plan(_schedule(_slot()))
        assert all(c[2]["entity_id"] != INVERTER for c in _calls(h))
        assert _calls(h) == []         # bez nastawy, której wolno dotknąć, nie startujemy
        await ex.async_stop()
    asyncio.run(body())


@pytest.mark.parametrize("plan_only", [True, None])
def test_plan_only_mode_blocks_writes(plan_only):
    async def body():
        h = _hass()
        ex, _ = await _started(h, plan_only=plan_only)      # nieznana wartość = brak zapisów
        await ex.async_on_plan(_schedule(_slot()))
        h.states.set(STATUS, "Charging")
        await ex.async_on_plan(_schedule(_slot(mode="off", power=None), meta=_meta(gen=GEN + timedelta(minutes=1))))
        assert _calls(h) == []
        await ex.async_stop()
    asyncio.run(body())


def test_inverter_entity_role_blocked_even_without_forbidden_set():
    async def body():
        h = _hass()
        roles = {"status": STATUS, "setpoint": INVERTER, "start_stop": START_STOP}
        ex, _ = await _started(h, roles=roles)               # tryb bezpośredni: brak mapy encji falownika
        await ex.async_on_plan(_schedule(_slot()))
        assert _calls(h) == []
        await ex.async_stop()
    asyncio.run(body())


@pytest.mark.parametrize("registry", ["other_device", "missing"])
def test_writable_role_outside_charger_device_invalidates_charger(registry):
    async def body():
        h = _hass()
        if registry == "missing":
            del h.data["entity_registry"].entities[START_STOP]
        else:
            h.data["entity_registry"].entities[START_STOP] = SimpleNamespace(entity_id=START_STOP, device_id="x")
        ex, _ = await _started(h)
        await ex.async_on_plan(_schedule(_slot()))
        assert _calls(h) == []         # także nastawa (należy do ładowarki) — cała ładowarka nieważna
        await ex.async_stop()
    asyncio.run(body())


def test_setpoint_rewritten_when_entity_differs_from_cache():
    async def body():
        h = _hass()
        ex, clock = await _started(h)
        await ex.async_on_plan(_schedule(_slot()))
        h.services.calls.clear()
        h.states.set(CURRENT, "10")    # ktoś zmienił nastawę poza Volcast
        clock.advance(61)
        await ex.async_on_plan(_schedule(_slot(), meta=_meta(gen=GEN + timedelta(minutes=1))))
        assert _calls(h) == [
            ("number", "set_value", {"entity_id": CURRENT, "value": 16.0}),
            ("select", "select_option", {"entity_id": START_STOP, "option": "Start charging"}),
        ]
        await ex.async_stop()
    asyncio.run(body())


def test_no_start_when_setpoint_differs_and_write_is_throttled():
    async def body():
        h = _hass(status="Charging")
        ex, clock = await _started(h)
        await ex.async_on_plan(_schedule(_slot()))          # nastawa 16 zapisana, ładuje
        h.services.calls.clear()
        h.states.set(CURRENT, "8")
        h.states.set(STATUS, "Available")
        clock.advance(10)
        await ex.async_on_plan(_schedule(_slot(), meta=_meta(gen=GEN + timedelta(minutes=1))))
        assert _calls(h) == []         # nastawa czeka na odstęp, start czeka na nastawę
        await ex.async_stop()
    asyncio.run(body())


def test_slot_without_session_is_ignored():
    async def body():
        h = _hass()
        ex, _ = await _started(h)
        await ex.async_on_plan(_schedule(_slot(session=None)))
        await ex.async_on_plan(_schedule(_slot(session="other"), meta=_meta(gen=GEN + timedelta(minutes=1))))
        assert _calls(h) == []
        await ex.async_stop()
    asyncio.run(body())


def test_plan_limits_are_clamped_to_entity_limits():
    async def body():
        h = _hass()
        ex, _ = await _started(h)
        await ex.async_on_plan(_schedule(_slot(power=22080.0), meta=_meta(max=32.0, max_power_w=22080.0)))
        assert _calls(h)[0] == ("number", "set_value", {"entity_id": CURRENT, "value": 16.0})
        await ex.async_stop()
    asyncio.run(body())


def test_meta_without_generated_at_writes_nothing():
    async def body():
        h = _hass()
        ex, _ = await _started(h)
        await ex.async_on_plan(_schedule(_slot(), meta=_meta(gen=None)))
        assert _calls(h) == []
        await ex.async_stop()
    asyncio.run(body())


def test_start_is_sent_again_in_a_later_forced_slot_of_the_same_plan():
    async def body():
        h = _hass()
        ex, clock = await _started(h)
        first = _slot(start=NOW, minutes=15)
        gap = _slot(mode="off", power=None, start=NOW + timedelta(minutes=15), minutes=15)
        second = _slot(start=NOW + timedelta(minutes=30), minutes=15)
        await ex.async_on_plan(_schedule(first, gap, second))
        assert [c[1] for c in _calls(h)] == ["set_value", "select_option"]   # auto nie ruszyło
        h.services.calls.clear()
        clock.advance(15 * 60)
        await ex.async_tick()          # slot off bez ładowania: nic
        assert _calls(h) == []
        clock.advance(15 * 60)
        await ex.async_tick()
        assert _calls(h) == [("select", "select_option", {"entity_id": START_STOP, "option": "Start charging"})]
        await ex.async_stop()
    asyncio.run(body())


def test_setpoint_role_of_wrong_domain_is_not_written():
    async def body():
        h = _hass()
        h.states.set("sensor.wb_current", "6", {"unit_of_measurement": "A"})
        roles = {"status": STATUS, "setpoint": "sensor.wb_current", "start_stop": START_STOP}
        ex, _ = await _started(h, roles=roles)
        await ex.async_on_plan(_schedule(_slot()))
        assert _calls(h) == []
        await ex.async_stop()
    asyncio.run(body())


# ── throttling ─────────────────────────────────────────────────────────────

def test_throttle_min_interval_per_entity():
    async def body():
        h = _hass(status="Charging")
        ex, clock = await _started(h)
        await ex.async_on_plan(_schedule(_slot()))
        assert [c[1] for c in _calls(h)] == ["set_value"]
        h.services.calls.clear()
        clock.advance(10)
        await ex.async_on_plan(_schedule(_slot(power=6900.0), meta=_meta(gen=GEN + timedelta(minutes=1))))
        assert _calls(h) == []         # 10 s po poprzednim zapisie tej encji
        clock.advance(51)
        await ex.async_tick()
        assert _calls(h) == [("number", "set_value", {"entity_id": CURRENT, "value": 10.0})]
        await ex.async_stop()
    asyncio.run(body())


def test_throttled_start_after_stop_waits_interval():
    async def body():
        h = _hass(status="Charging")
        ex, clock = await _started(h)
        await ex.async_on_plan(_schedule(_slot(mode="off", power=None)))
        h.states.set(STATUS, "Available")
        h.services.calls.clear()
        clock.advance(5)
        await ex.async_on_plan(_schedule(_slot(), meta=_meta(gen=GEN + timedelta(minutes=1))))
        # nastawa jedzie (inna encja), start czeka na odstęp od zapisu „stop” tej samej encji
        assert [c[1] for c in _calls(h)] == ["set_value"]
        h.services.calls.clear()
        clock.advance(60)
        await ex.async_tick()
        assert _calls(h) == [("select", "select_option", {"entity_id": START_STOP, "option": "Start charging"})]
        await ex.async_stop()
    asyncio.run(body())


# ── dopasowanie opcji i ładowarka na przełączniku ──────────────────────────

def test_pick_option_tokens():
    assert pick_option(OPTIONS, "start") == "Start charging"
    assert pick_option(OPTIONS, "stop") == "Stop charging"
    assert pick_option(["Restart", "Resume", "Pause"], "start") == "Resume"
    assert pick_option(["Restart", "Resume", "Pause"], "stop") == "Pause"
    assert pick_option(["No operation"], "start") is None
    assert pick_option(None, "start") is None


def test_is_charging():
    assert is_charging("Charging") and is_charging("charging")
    assert not is_charging("Not charging") and not is_charging("Available") and not is_charging(None)
    assert not is_charging("discharging")
    for s in ("Charging paused", "charging_completed", "Charging finished", "Charging complete",
              "Charging stopped", "Suspended charging", "charging_done", "Charging ended", "charging_error",
              "Waiting for charging", "Charging interrupted", "charging_scheduled", "Charging fault"):
        assert not is_charging(s), s
    for s in ("charging", "Charging", "ev_charging", "Charging EV", "car_charging", "Charging in progress"):
        assert is_charging(s), s


def test_switch_charger_turn_on_and_off():
    async def body():
        roles = {"status": STATUS, "setpoint": CURRENT, "start_stop": SWITCH}
        h = _hass(roles=roles)
        ex, clock = await _started(h, roles=roles)
        await ex.async_on_plan(_schedule(_slot()))
        assert _calls(h) == [("number", "set_value", {"entity_id": CURRENT, "value": 16.0}),
                             ("switch", "turn_on", {"entity_id": SWITCH})]
        h.states.set(STATUS, "Charging")
        h.services.calls.clear()
        clock.advance(61)
        await ex.async_on_plan(_schedule(_slot(mode="off", power=None), meta=_meta(gen=GEN + timedelta(minutes=1))))
        assert _calls(h) == [("switch", "turn_off", {"entity_id": SWITCH})]
        await ex.async_stop()
    asyncio.run(body())


def test_charger_without_start_stop_role_only_sets_current():
    async def body():
        roles = {"status": STATUS, "setpoint": CURRENT}
        h = _hass()
        ex, _ = await _started(h, roles=roles)
        await ex.async_on_plan(_schedule(_slot()))
        assert _calls(h) == [("number", "set_value", {"entity_id": CURRENT, "value": 16.0})]
        await ex.async_stop()
    asyncio.run(body())


def test_watt_setpoint_charger():
    async def body():
        h = _hass()
        h.states.set(CURRENT, "1400", {"min": 1400, "max": 11000, "step": 100, "unit_of_measurement": "W"})
        ex, _ = await _started(h)
        meta = _meta(control_unit="W", min=1400.0, max=11000.0, step=100.0)
        await ex.async_on_plan(_schedule(_slot(power=7380.0), meta=meta))
        assert _calls(h)[0] == ("number", "set_value", {"entity_id": CURRENT, "value": 7300.0})
        await ex.async_stop()
    asyncio.run(body())


# ── magazyn i restart ──────────────────────────────────────────────────────

def test_state_survives_restart():
    async def body():
        h = _hass()
        store = LoadsStore(h, "e1")
        ex, clock = await _started(h, store=store)
        await ex.async_on_plan(_schedule(_slot()))
        await ex.async_stop()
        saved = await store.async_load()
        assert saved.last_action == "start" and saved.last_setpoint_a == 16.0
        assert saved.last_write_at == NOW.isoformat() and saved.plan_generated_at == GEN.isoformat()
        h.services.calls.clear()
        ex2, _ = _make(h, store=store, clock=clock)
        clock.advance(120)
        await ex2.async_start()
        await ex2.async_on_plan(_schedule(_slot()))      # ten sam plan po restarcie
        assert _calls(h) == []
        await ex2.async_stop()
    asyncio.run(body())


def test_start_from_stored_plan_seeds_slots():
    async def body():
        h = _hass()
        ex, _ = _make(h)
        await ex.async_start(_schedule(_slot()))
        await ex.async_tick()
        assert [c[1] for c in _calls(h)] == ["set_value", "select_option"]
        await ex.async_stop()
    asyncio.run(body())


def test_store_roundtrip_and_corrupt_fields():
    async def body():
        st = LoadsStore(object(), "e1")
        assert await st.async_load() == LoadsState()
        s = LoadsState(last_action="stop", last_write_at="2026-09-23T10:00:00+00:00", last_setpoint_a=10.0,
                       plan_generated_at="2026-09-23T09:55:00+00:00", fallback_since=None,
                       manual_event="stop", manual_at="2026-09-23T10:01:00+00:00")
        await st.async_save(s)
        assert await st.async_load() == s
        await st._store.async_save({"last_action": "dance", "last_setpoint_a": "16", "last_write_at": 5,
                                    "manual_event": 1})
        assert await st.async_load() == LoadsState()
        await st._store.async_save("garbage")
        assert await st.async_load() == LoadsState()
    asyncio.run(body())


def test_store_key_is_per_entry():
    st = LoadsStore(object(), "abc")
    assert st._store._key == "volcast.loads.abc"


def test_stopped_executor_writes_nothing():
    async def body():
        h = _hass()
        ex, _ = await _started(h)
        await ex.async_stop()
        await ex.async_on_plan(_schedule(_slot()))
        await ex.async_tick()
        assert _calls(h) == []
    asyncio.run(body())


# ── odczyt kontrolny ───────────────────────────────────────────────────────

def test_readback_warns_once_without_rewrite(caplog):
    async def body():
        caplog.set_level(logging.WARNING, logger="custom_components.volcast.control.loads_executor")
        h = _hass()
        ex, _ = await _started(h)
        await ex.async_on_plan(_schedule(_slot()))
        await ex.async_readback_done()
        assert "did not start" in caplog.text
        assert len(_calls(h)) == 2     # bez ponownego zapisu po rozjeździe
        await ex.async_stop()
    asyncio.run(body())


def test_readback_quiet_when_charger_follows(caplog):
    async def body():
        caplog.set_level(logging.WARNING, logger="custom_components.volcast.control.loads_executor")
        h = _hass()
        ex, _ = await _started(h)
        await ex.async_on_plan(_schedule(_slot()))
        h.states.set(STATUS, "Charging")
        await ex.async_readback_done()
        assert "did not" not in caplog.text and "differs" not in caplog.text
        await ex.async_stop()
    asyncio.run(body())


# ── runtime: rozdział planu ────────────────────────────────────────────────

def test_plan_fan_out_reaches_both_and_loads_error_never_blocks_battery():
    from custom_components.volcast.control.runtime import plan_fan_out
    seen = []

    class Battery:
        async def async_on_plan(self, raw, schedule):
            seen.append(("battery", raw, schedule))

    class Loads:
        async def async_on_plan(self, schedule):
            seen.append(("loads", schedule))
            raise RuntimeError("boom")

    on_plan = plan_fan_out(Battery(), Loads())
    asyncio.run(on_plan({"a": 1}, "S"))
    assert seen == [("battery", {"a": 1}, "S"), ("loads", "S")]
    asyncio.run(plan_fan_out(Battery(), None)({"b": 2}, "T"))
    assert seen[-1] == ("battery", {"b": 2}, "T")


def test_unload_stops_loads_executor_without_writes():
    from custom_components.volcast.control import runtime as rt_mod
    calls = []

    class Exec:
        async def async_neutral_at_stop(self):
            calls.append("neutral")

        async def async_stop(self):
            calls.append("stop")

    class Telemetry:
        async def async_stop(self):
            calls.append("telemetry_stop")

    class Loads:
        async def async_stop(self):
            calls.append("loads_stop")

    rt = rt_mod.ControlRuntime(executor=Exec(), fetcher=None, telemetry=Telemetry(), cloud=None, choice=None,
                               mapped={}, rated_power_w=None, loads_executor=Loads())
    asyncio.run(rt_mod.async_unload_control(object(), rt))
    assert calls == ["loads_stop", "neutral", "telemetry_stop", "stop"]


def test_build_loads_executor_forbids_inverter_entities():
    from custom_components.volcast.control import runtime as rt_mod
    h = _hass()
    ex = rt_mod._build_loads_executor(h, _entry(), SimpleNamespace(consent=True, local_switch=True, plan_only=False),
                                      frozenset({"select.goodwe_ems_mode"}), {"power_w": INVERTER})
    assert {INVERTER, "select.goodwe_ems_mode"} <= ex._forbidden
    assert ex._consent() is True and ex._local_switch() is True and ex._plan_only() is False
    bare = rt_mod._build_loads_executor(h, _entry(), SimpleNamespace(), frozenset(), {})
    assert bare._plan_only() is None and bare._consent() is None    # brak pola = bramka zamknięta


def test_charger_roles_lookup():
    opts = {"ev_chargers": [{"device_id": "dev1", "roles": ROLES}, "x", {"device_id": "dev2"}]}
    assert charger_roles(opts, "dev1") == ROLES
    assert charger_roles(opts, "dev2") == {}
    assert charger_roles(opts, "nope") is None
    assert charger_roles({}, "dev1") is None
