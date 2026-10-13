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
        # encja falownika w amperach (np. prąd ładowania baterii): jednostka i zakres pasują do planu,
        # więc zapis zatrzymuje WYŁĄCZNIE sprawdzenie urządzenia w rejestrze encji (inv1 ≠ dev1)
        h.states.set(INVERTER, "0", {"min": 0, "max": 32, "step": 1, "unit_of_measurement": "A"})
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


# ── ponowny zapis nastawy przy rozjeździe odczytu ──────────────────────────

def test_setpoint_rewrite_at_most_once_per_plan_and_slot():
    async def body():
        h = _hass(status="Charging")
        ex, clock = await _started(h)
        slot = _slot(minutes=60)
        await ex.async_on_plan(_schedule(slot))
        assert [c[1] for c in _calls(h)] == ["set_value"]
        for _ in range(10):            # ładowarka trzyma własny limit 15 A przy planie 16 A
            h.states.set(CURRENT, "15")
            clock.advance(61)
            await ex.async_tick()
        assert [c[1] for c in _calls(h)] == ["set_value", "set_value"]     # jeden ponowny zapis
        h.services.calls.clear()
        h.states.set(CURRENT, "15")
        clock.advance(61)
        await ex.async_on_plan(_schedule(slot, meta=_meta(gen=GEN + timedelta(minutes=30))))
        assert [c[1] for c in _calls(h)] == ["set_value"]                   # nowy plan: znowu raz
        await ex.async_stop()
    asyncio.run(body())


def test_exhausted_rewrite_starts_only_below_planned_setpoint():
    async def body():
        h = _hass()
        ex, clock = await _started(h)
        await ex.async_on_plan(_schedule(_slot(minutes=60)))         # 16 A + start; auto nie rusza
        for actual in ("13", "13"):    # ponowny zapis i dalej 13 A (np. limit kabla)
            h.states.set(CURRENT, actual)
            clock.advance(61)
            await ex.async_tick()
        h.services.calls.clear()
        ex.state.last_action = None                                    # nowa faza: start znowu dozwolony
        clock.advance(61)
        await ex.async_tick()
        assert _calls(h) == [("select", "select_option", {"entity_id": START_STOP, "option": "Start charging"})]
        h.services.calls.clear()
        ex.state.last_action = None
        h.states.set(CURRENT, "32")    # obca nastawa WYŻSZA niż plan: bez startu
        clock.advance(61)
        await ex.async_tick()
        assert all(c[1] != "select_option" for c in _calls(h))
        await ex.async_stop()
    asyncio.run(body())


# ── reguła awaryjna ───────────────────────────────────────────────────────

OLD_GEN = NOW - timedelta(hours=8)


def _fb_meta(**kw):
    # 11,04 kWh przy 11 040 W = 1 h; termin za 1 h 15 min → okno awaryjne trwa od 15 min
    base = dict(gen=OLD_GEN, remaining_kwh=11.04, ready_by=NOW + timedelta(minutes=75), fallback_allowed=True)
    base.update(kw)
    return _meta(**base)


def _stale(ex):
    """Brak odpowiedzi chmury od startu, a okres łaski po starcie minął: wiek = od wygenerowania planu."""
    from custom_components.volcast.control.loads_executor import STARTUP_GRACE_S
    ex._confirmed_mono = None
    ex._started_mono -= STARTUP_GRACE_S


def test_fallback_forces_full_power_when_plan_is_stale():
    async def body():
        h = _hass()
        ex, _ = await _started(h)
        await ex.async_on_plan(_schedule(_slot(mode="off", power=None, minutes=60), meta=_fb_meta()))
        assert _calls(h) == []         # plan właśnie przyszedł: świeży, slot off
        _stale(ex)      # brak potwierdzenia od startu: wiek liczony od wygenerowania planu
        await ex.async_tick()
        assert _calls(h) == [
            ("number", "set_value", {"entity_id": CURRENT, "value": 16.0}),
            ("select", "select_option", {"entity_id": START_STOP, "option": "Start charging"}),
        ]
        assert ex.state.fallback_since == NOW.isoformat()
        assert ex.report_for("dev1")["fallback"] is True
        await ex.async_stop()
    asyncio.run(body())


def test_fallback_waits_90_minutes_after_last_confirmation():
    async def body():
        h = _hass()
        ex, clock = await _started(h)
        await ex.async_on_plan(_schedule(_slot(mode="off", power=None, minutes=240), meta=_fb_meta(
            ready_by=NOW + timedelta(hours=3))))
        clock.advance(60 * 60)
        ex.note_plan_confirmed()       # chmura odpowiada (plan bez zmian)
        clock.advance(89 * 60)
        await ex.async_tick()
        assert _calls(h) == []
        clock.advance(2 * 60)          # 91 min bez potwierdzenia
        await ex.async_tick()
        assert [c[1] for c in _calls(h)] == ["set_value", "select_option"]
        await ex.async_stop()
    asyncio.run(body())


def test_fallback_without_any_slot_still_forces():
    async def body():
        h = _hass()
        ex, _ = await _started(h)
        await ex.async_on_plan(_schedule(meta=_fb_meta()))           # plan bez slotów (np. po terminie)
        _stale(ex)
        await ex.async_tick()
        assert [c[1] for c in _calls(h)] == ["set_value", "select_option"]
        await ex.async_stop()
    asyncio.run(body())


@pytest.mark.parametrize("gate", ["consent", "switch", "plan_only", "control_enabled"])
def test_fallback_respects_every_gate(gate):
    async def body():
        h = _hass()
        ex, _ = await _started(h, consent=gate != "consent", switch=gate != "switch",
                               plan_only=gate == "plan_only")
        await ex.async_on_plan(_schedule(_slot(mode="off", power=None), meta=_fb_meta(
            control=gate != "control_enabled")))
        _stale(ex)
        await ex.async_tick()
        assert _calls(h) == [] and ex.state.fallback_since is None
        await ex.async_stop()
    asyncio.run(body())


def test_fallback_never_stops_charging_and_ends_with_fresh_plan():
    async def body():
        h = _hass()
        ex, clock = await _started(h)
        await ex.async_on_plan(_schedule(_slot(mode="off", power=None, minutes=60), meta=_fb_meta()))
        _stale(ex)
        await ex.async_tick()          # awaryjnie: nastawa + start
        h.states.set(STATUS, "Charging")
        h.services.calls.clear()
        for _ in range(3):
            clock.advance(61)
            await ex.async_tick()
        assert all(c[1] != "select_option" for c in _calls(h))       # w trybie awaryjnym nigdy stop
        assert ex.state.fallback_since is not None
        clock.advance(61)
        await ex.async_on_plan(_schedule(_slot(mode="forced", start=clock.utc, minutes=60),
                                         meta=_fb_meta(gen=clock.utc - timedelta(seconds=1))))
        assert ex.state.fallback_since is None
        report = ex.report_for("dev1")
        assert report["fallback"] is True           # zatrzask: chmura musi zobaczyć, że tryb awaryjny był
        await ex.async_ack_report(report)
        assert ex.report_for("dev1")["fallback"] is False
        await ex.async_stop()
    asyncio.run(body())


def test_fallback_latch_survives_until_an_accepted_report():
    async def body():
        h = _hass()
        ex, clock = await _started(h)
        await ex.async_on_plan(_schedule(meta=_fb_meta()))
        stale = ex.report_for("dev1")               # paczka zebrana PRZED trybem awaryjnym
        _stale(ex)
        await ex.async_tick()
        ex.note_plan_confirmed()                    # łączność wraca, zanim poszła telemetria
        await ex.async_tick()
        assert ex.state.fallback_since is None
        await ex.async_ack_report(stale)            # stara paczka nie kasuje zatrzasku
        assert ex.report_for("dev1")["fallback"] is True
        await ex.async_ack_report(ex.report_for("dev1"))
        assert ex.report_for("dev1")["fallback"] is False
        await ex.async_stop()
    asyncio.run(body())


def test_fallback_needs_cloud_permission():
    async def body():
        h = _hass()
        ex, _ = await _started(h)
        # pauza użytkownika albo „tylko PV”: chmura daje fallback_allowed=False
        await ex.async_on_plan(_schedule(_slot(mode="off", power=None), meta=_fb_meta(fallback_allowed=False)))
        _stale(ex)
        await ex.async_tick()
        assert _calls(h) == [] and ex.state.fallback_since is None
        await ex.async_stop()
    asyncio.run(body())


def test_no_fallback_right_after_restart_before_the_cloud_answers():
    async def body():
        from custom_components.volcast.control.loads_executor import STARTUP_GRACE_S
        h = _hass()
        ex, clock = _make(h)
        await ex.async_start(_schedule(_slot(mode="off", power=None, minutes=60), meta=_fb_meta()))
        await ex.async_tick()                       # plan z magazynu, sieć jeszcze nie wstała
        assert _calls(h) == []
        clock.advance(STARTUP_GRACE_S - 1)
        await ex.async_tick()
        assert _calls(h) == []
        clock.advance(2)                            # okres łaski minął, chmura dalej milczy
        await ex.async_tick()
        assert [c[1] for c in _calls(h)] == ["set_value", "select_option"]
        await ex.async_stop()
    asyncio.run(body())


def test_fallback_flag_cleared_when_a_gate_closes():
    async def body():
        h = _hass()
        consent = {"v": True}
        clock = Clock()
        entry = _entry()
        ex = LoadsExecutor(h, entry, writer=EntityServiceWriter(h),
                           roles_provider=lambda key: charger_roles(entry.options, key),
                           store=LoadsStore(h, "e1"), consent_getter=lambda: consent["v"],
                           local_switch_getter=lambda: True, plan_only_getter=lambda: False,
                           clock=lambda: clock.mono, utcnow=lambda: clock.utc, readback_s=0)
        await ex.async_start()
        await ex.async_on_plan(_schedule(meta=_fb_meta()))
        _stale(ex)
        await ex.async_tick()
        assert ex.report_for("dev1")["fallback"] is True
        consent["v"] = False
        await ex.async_tick()
        assert ex.state.fallback_since is None
        await ex.async_ack_report(ex.report_for("dev1"))
        assert ex.report_for("dev1")["fallback"] is False
        await ex.async_stop()
    asyncio.run(body())


def test_fallback_with_zero_power_meta_is_not_forced():
    async def body():
        h = _hass()
        ex, _ = await _started(h)
        await ex.async_on_plan(_schedule(_slot(mode="off", power=None), meta=_fb_meta(max_power_w=0.0, step=0.0)))
        _stale(ex)
        await ex.async_tick()
        assert _calls(h) == [] and ex.state.fallback_since is None
        await ex.async_stop()
    asyncio.run(body())


def test_refresh_confirms_plan_only_on_cloud_answer():
    from custom_components.volcast.control.runtime import refresh_and_tick
    seen = []

    class Fetcher:
        def __init__(self, result):
            self.result = result

        async def async_refresh(self):
            return self.result

    class Battery:
        async def async_tick(self):
            seen.append("battery")

    class Loads:
        def note_plan_confirmed(self):
            seen.append("confirmed")

        async def async_tick(self):
            seen.append("loads")

    for result in ("accepted", "unchanged", "network", "auth", "rejected", "error"):
        seen.clear()
        asyncio.run(refresh_and_tick(Fetcher(result), Battery(), Loads()))
        assert seen == (["confirmed", "battery", "loads"] if result in ("accepted", "unchanged")
                        else ["battery", "loads"]), result
    seen.clear()
    asyncio.run(refresh_and_tick(Fetcher("accepted"), Battery(), None))
    assert seen == ["battery"]


# ── ręczna ingerencja ─────────────────────────────────────────────────────

def _event(eid, old, new, *, user="u1", ctx=None):
    from homeassistant.core import Context

    from .ha_fakes import FakeState
    context = ctx or Context(user_id=user)
    return SimpleNamespace(data={
        "entity_id": eid,
        "old_state": FakeState(eid, old) if old is not None else None,
        "new_state": FakeState(eid, new, context=context) if new is not None else None,
    }, context=context)


async def _charging_forced(h, **kw):
    ex, clock = await _started(h, **kw)
    await ex.async_on_plan(_schedule(_slot(minutes=60)))
    h.states.set(STATUS, "Charging")
    h.services.calls.clear()
    return ex, clock


def test_manual_stop_in_forced_slot_is_reported_and_holds_writes(monkeypatch):
    from custom_components.volcast.control import loads_executor as mod
    signals = []
    monkeypatch.setattr(mod, "async_dispatcher_send", lambda hass, sig: signals.append(sig))

    async def body():
        h = _hass()
        ex, clock = await _charging_forced(h)
        clock.advance(30)
        ex.on_state_event(_event(START_STOP, "No operation", "Stop charging"))
        assert ex.state.manual_event == "stop" and ex.state.manual_at == clock.utc.isoformat()
        assert signals == ["volcast_control_state_updated_e1"]
        await asyncio.sleep(0)         # zapis magazynu w tle
        assert (await ex._store.async_load()).manual_event == "stop"
        h.states.set(STATUS, "Available")
        for _ in range(3):             # ten sam plan: ani start, ani nastawa
            clock.advance(61)
            await ex.async_tick()
        assert _calls(h) == []
        clock.advance(61)              # nowy plan, policzony PO zdarzeniu: wykonanie wraca
        await ex.async_on_plan(_schedule(_slot(start=NOW, minutes=60),
                                         meta=_meta(gen=clock.utc - timedelta(seconds=5))))
        assert [c[1] for c in _calls(h)] == ["select_option"]
        await ex.async_stop()
    asyncio.run(body())


def test_plan_generated_before_manual_event_keeps_hold():
    async def body():
        h = _hass()
        ex, clock = await _charging_forced(h)
        clock.advance(30)
        ex.on_state_event(_event(START_STOP, "No operation", "Stop charging"))
        h.states.set(STATUS, "Available")
        clock.advance(61)              # plan w drodze: wygenerowany przed zdarzeniem
        await ex.async_on_plan(_schedule(_slot(minutes=60), meta=_meta(gen=NOW + timedelta(seconds=10))))
        assert _calls(h) == []
        await ex.async_stop()
    asyncio.run(body())


def test_manual_event_during_cycle_blocks_the_rest_of_it():
    async def body():
        h = _hass()
        ex, _ = await _started(h)
        real = h.services.async_call

        async def call(domain, service, data, blocking=False, context=None):
            await real(domain, service, data, blocking=blocking, context=context)
            if service == "set_value":     # użytkownik zatrzymuje w trakcie naszego cyklu
                ex.on_state_event(_event(START_STOP, "No operation", "Stop charging"))
        h.services.async_call = call
        await ex.async_on_plan(_schedule(_slot()))
        assert [c[1] for c in _calls(h)] == ["set_value"]                   # start już nie poszedł
        assert ex.state.manual_event == "stop"
        await ex.async_stop()
    asyncio.run(body())


def test_long_manual_hold_warns_once_and_keeps_holding(caplog):
    async def body():
        caplog.set_level(logging.WARNING, logger="custom_components.volcast.control.loads_executor")
        h = _hass()
        ex, clock = await _charging_forced(h)
        ex.on_state_event(_event(START_STOP, "No operation", "Stop charging"))
        h.states.set(STATUS, "Available")
        clock.advance(6 * 3600 - 60)
        await ex.async_tick()
        assert "no new plan" not in caplog.text
        for _ in range(3):
            clock.advance(61)
            await ex.async_tick()
        assert caplog.text.count("no new plan") == 1
        assert _calls(h) == []                     # bez samoczynnego wznowienia zapisów
        await ex.async_stop()
    asyncio.run(body())


def test_any_user_change_during_hold_updates_the_event():
    async def body():
        h = _hass()
        ex, clock = await _charging_forced(h)
        ex.on_state_event(_event(START_STOP, "No operation", "Stop charging"))
        clock.advance(10)                          # zmiana zdania przed nowym planem (slot dalej forced)
        ex.on_state_event(_event(START_STOP, "Stop charging", "Start charging"))
        assert ex.state.manual_event == "start" and ex.state.manual_at == clock.utc.isoformat()
        await ex.async_stop()
    asyncio.run(body())


def test_manual_hold_beats_fallback():
    async def body():
        h = _hass()
        ex, clock = await _started(h)
        await ex.async_on_plan(_schedule(_slot(minutes=60), meta=_fb_meta(gen=NOW - timedelta(minutes=1))))
        h.states.set(STATUS, "Charging")
        h.services.calls.clear()
        ex.on_state_event(_event(START_STOP, "No operation", "Stop charging"))
        h.states.set(STATUS, "Available")
        _stale(ex)
        clock.advance(2 * 3600)        # plan stary, termin blisko — ręczny stop i tak wygrywa
        await ex.async_tick()
        assert _calls(h) == [] and ex.state.fallback_since is None
        await ex.async_stop()
    asyncio.run(body())


def test_manual_start_in_off_slot_is_reported_and_not_stopped():
    async def body():
        roles = {"status": STATUS, "setpoint": CURRENT, "start_stop": SWITCH}
        h = _hass(roles=roles)
        ex, clock = await _started(h, roles=roles)
        await ex.async_on_plan(_schedule(_slot(mode="off", power=None, minutes=60)))
        ex.on_state_event(_event(SWITCH, "off", "on"))
        assert ex.state.manual_event == "start"
        h.states.set(STATUS, "Charging")
        clock.advance(61)
        await ex.async_tick()
        assert _calls(h) == []         # ręcznego ładowania nie zatrzymujemy
        await ex.async_stop()
    asyncio.run(body())


def test_manual_status_change_with_actor_is_classified():
    async def body():
        h = _hass()
        ex, _ = await _charging_forced(h)
        ex.on_state_event(_event(STATUS, "Charging", "Available"))
        assert ex.state.manual_event == "stop"
        await ex.async_stop()
    asyncio.run(body())


@pytest.mark.parametrize("case", ["own_write", "no_actor", "stop_in_off", "start_in_forced", "no_slot",
                                  "gate_closed", "other_entity", "no_option", "unavailable"])
def test_not_a_manual_event(case):
    async def body():
        h = _hass(status="Charging")
        ex, _ = await _started(h, consent=case != "gate_closed")
        mode = "off" if case == "stop_in_off" else "forced"
        start = NOW + timedelta(hours=2) if case == "no_slot" else NOW - timedelta(minutes=5)
        await ex.async_on_plan(_schedule(_slot(mode=mode, power=None if mode == "off" else 11100.0,
                                               start=start, minutes=60)))
        if case == "own_write":
            from homeassistant.core import Context
            # nasz zapis „stop” z kontekstem, który ma aktora — odróżnia go tylko pamięć pisarza
            ex._writer = EntityServiceWriter(h, context_factory=lambda: Context(user_id="u1"))
            assert await ex._async_command(ex._charger("dev1"), "stop")
            ev = _event(START_STOP, "No operation", "Stop charging", ctx=h.states.get(START_STOP).context)
        elif case == "no_actor":
            ev = _event(STATUS, "Charging", "Available", user=None)        # auto skończyło / pauza auta
        elif case == "start_in_forced":
            ev = _event(START_STOP, "No operation", "Start charging")
        elif case == "other_entity":
            ev = _event(CURRENT, "16", "10")
        elif case == "no_option":
            ev = _event(START_STOP, "Stop charging", "No operation")
        elif case == "unavailable":
            ev = _event(STATUS, "Charging", "unavailable")
        else:
            ev = _event(START_STOP, "No operation", "Stop charging")
        ex.on_state_event(ev)
        assert ex.state.manual_event is None and ex.state.manual_at is None
        await ex.async_stop()
    asyncio.run(body())


# ── ręczna ingerencja spoza HA (aplikacja producenta, przycisk, karta) ─────

def _no_ctx(eid, old, new):
    """Zmiana zapisana przez integrację ładowarki z jej chmury: kontekst bez aktora i nie nasz."""
    return _event(eid, old, new, user=None)


async def _charging_forced_quiet(h, **kw):
    ex, clock = await _charging_forced(h, **kw)
    from custom_components.volcast.control.loads_executor import OWN_ECHO_S
    clock.advance(OWN_ECHO_S + 1)               # poza oknem echa naszych zapisów
    return ex, clock


@pytest.mark.parametrize("new", ["Paused", "Stopped", "SuspendedEVSE", "charging_paused", "Suspended"])
def test_charger_app_stop_in_forced_slot_is_manual(new):
    async def body():
        h = _hass()
        ex, _ = await _charging_forced_quiet(h)
        ex.on_state_event(_no_ctx(STATUS, "Charging", new))
        assert ex.state.manual_event == "stop", new
        await ex.async_stop()
    asyncio.run(body())


@pytest.mark.parametrize("new", ["Finished", "Charging finished", "Completed", "Full", "charging_complete", "Charged",
                                 "Error", "Fault", "charging_error", "unavailable", "unknown",
                                 "SuspendedEV", "suspended_ev", "Waiting", "Available", "Disconnected", "Unplugged"])
def test_end_error_car_side_and_unplug_are_not_manual(new):
    async def body():
        h = _hass()
        ex, _ = await _charging_forced_quiet(h)
        ex.on_state_event(_no_ctx(STATUS, "Charging", new))
        assert ex.state.manual_event is None, new
        await ex.async_stop()
    asyncio.run(body())


@pytest.mark.parametrize("raw,kind", [
    ("Charging", "charging"), ("Paused", "paused"), ("SuspendedEVSE", "paused"),
    ("Stopped - finished", "end"), ("Stopped (full)", "end"), ("Paused: error", "error"), ("Stopped, fault", "error"),
    ("Paused, disconnected", "unplugged"), ("Stopped (not connected)", "unplugged"), ("SuspendedEV", "car"),
    ("Paused, waiting for car", "car"), ("Idle", "other"), ("unavailable", None),
])
def test_status_kind_priorities(raw, kind):
    from custom_components.volcast.control.loads_executor import status_kind

    from .ha_fakes import FakeState
    assert status_kind(FakeState(STATUS, raw)) == kind
    assert status_kind(None) is None


def test_status_change_right_after_own_write_is_not_manual():
    async def body():
        h = _hass()
        ex, clock = await _charging_forced(h)       # nasza nastawa i start chwilę temu
        clock.advance(10)
        ex.on_state_event(_no_ctx(STATUS, "Charging", "Paused"))      # echo naszego zapisu
        assert ex.state.manual_event is None
        await ex.async_stop()
    asyncio.run(body())


def test_charger_app_stop_on_command_entity_is_manual():
    async def body():
        h = _hass()
        ex, _ = await _charging_forced_quiet(h)
        ex.on_state_event(_no_ctx(START_STOP, "No operation", "Stop charging"))
        assert ex.state.manual_event == "stop"
        await ex.async_stop()
    asyncio.run(body())


def test_new_plan_after_charger_app_stop_does_not_resume():
    async def body():
        h = _hass()
        ex, clock = await _charging_forced_quiet(h)
        ex.on_state_event(_no_ctx(STATUS, "Charging", "Paused"))
        h.states.set(STATUS, "Paused")
        h.services.calls.clear()
        clock.advance(61)                           # plan w drodze (sprzed zdarzenia), slot dalej forced
        await ex.async_on_plan(_schedule(_slot(minutes=60), meta=_meta(gen=NOW + timedelta(seconds=1))))
        assert _calls(h) == []
        clock.advance(61)                           # plan po zdarzeniu: chmura wstrzymała sesję (off)
        await ex.async_on_plan(_schedule(_slot(mode="off", power=None, start=NOW, minutes=60),
                                         meta=_meta(gen=clock.utc)))
        assert _calls(h) == []                      # nie ładuje — stop nie jest potrzebny, start nie idzie
        await ex.async_stop()
    asyncio.run(body())


def test_charger_app_start_in_off_slot_after_our_stop_is_manual():
    async def body():
        from custom_components.volcast.control.loads_executor import OWN_ECHO_S
        h = _hass(status="Charging")
        ex, clock = await _started(h)
        await ex.async_on_plan(_schedule(_slot(mode="off", power=None, minutes=60)))   # autostart → nasz stop
        h.states.set(STATUS, "Stopped")
        clock.advance(OWN_ECHO_S + 1)
        ex.on_state_event(_no_ctx(STATUS, "Stopped", "Charging"))
        assert ex.state.manual_event == "start"
        h.services.calls.clear()
        clock.advance(61)
        h.states.set(STATUS, "Charging")
        await ex.async_tick()
        assert _calls(h) == []                      # ręcznie włączonego ładowania nie zatrzymujemy
        await ex.async_stop()
    asyncio.run(body())


def test_autostart_in_off_slot_without_our_stop_is_not_manual():
    async def body():
        from custom_components.volcast.control.loads_executor import OWN_ECHO_S
        h = _hass(status="Available")
        ex, clock = await _started(h)
        await ex.async_on_plan(_schedule(_slot(mode="off", power=None, minutes=60)))
        clock.advance(OWN_ECHO_S + 1)
        ex.on_state_event(_no_ctx(STATUS, "Available", "Charging"))     # auto ruszyło samo po podłączeniu
        assert ex.state.manual_event is None
        await ex.async_stop()
    asyncio.run(body())


def test_status_start_right_after_our_stop_is_not_manual():
    async def body():
        h = _hass(status="Charging")
        ex, clock = await _started(h)
        await ex.async_on_plan(_schedule(_slot(mode="off", power=None, minutes=60)))
        clock.advance(5)
        ex.on_state_event(_no_ctx(STATUS, "Stopped", "Charging"))       # ładowarka jeszcze nie przyjęła stopu
        assert ex.state.manual_event is None
        await ex.async_stop()
    asyncio.run(body())


def test_state_change_watch_follows_charger_entities(monkeypatch):
    from custom_components.volcast.control import loads_executor as mod
    subs, unsubs = [], []

    def track(hass, entities, action):
        subs.append(list(entities))
        return lambda: unsubs.append(list(entities))
    monkeypatch.setattr(mod, "async_track_state_change_event", track)

    async def body():
        h = _hass()
        ex, _ = await _started(h)
        await ex.async_on_plan(_schedule(_slot()))
        await ex.async_on_plan(_schedule(_slot(), meta=_meta(gen=GEN + timedelta(minutes=1))))
        assert subs == [[STATUS, START_STOP]]                         # bez ponownej subskrypcji
        await ex.async_stop()
        assert unsubs == [[STATUS, START_STOP]]
    asyncio.run(body())


def test_manual_hold_survives_restart():
    async def body():
        h = _hass()
        store = LoadsStore(h, "e1")
        ex, clock = await _charging_forced(h, store=store)
        ex.on_state_event(_event(START_STOP, "No operation", "Stop charging"))
        await asyncio.sleep(0)
        await ex.async_ack_manual(ex.state.manual_at)                  # zgłoszone do chmury
        await ex.async_stop()
        h.states.set(STATUS, "Available")
        h.services.calls.clear()
        ex2, _ = _make(h, store=store, clock=clock)
        clock.advance(120)
        await ex2.async_start(_schedule(_slot(minutes=60)))
        await ex2.async_tick()
        assert _calls(h) == []
        await ex2.async_stop()
    asyncio.run(body())


# ── raport wykonawcy w telemetrii ─────────────────────────────────────────

def test_report_and_ack_send_each_manual_event_once():
    async def body():
        h = _hass()
        ex, clock = await _charging_forced(h)
        assert ex.report_for("dev2") is None
        assert ex.report_for("dev1") == {"last_write_at": NOW.isoformat(), "last_action": "start",
                                         "manual_event": None, "manual_at": None, "fallback": False,
                                         "held_since": None}
        ex.on_state_event(_event(START_STOP, "No operation", "Stop charging"))
        first = ex.report_for("dev1")
        assert first["manual_event"] == "stop" and first["manual_at"] == NOW.isoformat()
        clock.advance(5)               # drugie zdarzenie przed potwierdzeniem pierwszej wysyłki
        ex.on_state_event(_event(STATUS, "Charging", "Available"))
        assert ex.state.manual_at == clock.utc.isoformat()
        await ex.async_ack_manual(first["manual_at"])                  # stara wysyłka nie kasuje nowszego
        assert ex.state.manual_event == "stop"
        await ex.async_ack_manual(ex.state.manual_at)
        assert ex.state.manual_event is None
        assert ex.report_for("dev1")["manual_at"] is None              # manual_at tylko ze zdarzeniem
        await ex.async_stop()
    asyncio.run(body())


def test_telemetry_carries_executor_block_and_acks_after_accepted_flush():
    from .test_telemetry import Cloud, sender

    async def body():
        h = _hass()
        ex, _ = await _charging_forced(h)
        ex.on_state_event(_event(START_STOP, "No operation", "Stop charging"))
        at = ex.state.manual_at

        async def persist():
            return None
        rt = SimpleNamespace(loads_executor=ex, async_persist_control_meta=persist)
        opts = {"entity_price_buy": "sensor.nordpool", "ev_chargers": _entry().options["ev_chargers"]}
        failing = sender(h, Cloud(ok=False), options=opts)
        failing.control_runtime = rt
        assert await failing.async_flush() is False
        assert ex.state.manual_event == "stop"                         # nieprzyjęta: zostaje do wysłania
        cloud = Cloud()
        s = sender(h, cloud, options=opts)
        s.control_runtime = rt
        assert await s.async_flush() is True
        (load,) = cloud.sent[0]["loads"]
        assert load["executor"] == {"last_write_at": NOW.isoformat(), "last_action": "start",
                                    "manual_event": "stop", "manual_at": at, "fallback": False,
                                    "held_since": at}
        assert ex.state.manual_event is None
        assert ex.report_for("dev1")["manual_event"] is None           # następna paczka już bez zdarzenia
        await ex.async_stop()
    asyncio.run(body())


def test_held_since_in_every_report_while_hold_lasts():
    from .ha_fakes import GOODWE_ENTITIES
    from .test_telemetry import Cloud, sender

    async def body():
        h = _hass()
        h.states.set(GOODWE_ENTITIES["soc"], "60", {"unit_of_measurement": "%"})   # każda paczka ma odczyt
        ex, clock = await _charging_forced(h)
        ex.on_state_event(_event(START_STOP, "No operation", "Stop charging"))
        at = ex.state.manual_at

        async def persist():
            return None
        cloud = Cloud()
        s = sender(h, cloud, options={"entity_price_buy": "sensor.nordpool",
                                      "ev_chargers": _entry().options["ev_chargers"]})
        s.control_runtime = SimpleNamespace(loads_executor=ex, async_persist_control_meta=persist)
        for _ in range(3):             # zdarzenie wysłane raz, wstrzymanie zgłaszane w KAŻDEJ paczce
            assert await s.async_flush() is True
        reports = [r["loads"][0]["executor"] for r in cloud.sent]
        assert [r["held_since"] for r in reports] == [at, at, at]
        assert [r["manual_event"] for r in reports] == ["stop", None, None]
        held = datetime.fromisoformat(at)
        assert held.tzinfo is not None and held.utcoffset() == timedelta(0)
        clock.advance(61)              # plan policzony po zdarzeniu: koniec wstrzymania
        await ex.async_on_plan(_schedule(_slot(start=NOW, minutes=60), meta=_meta(gen=clock.utc)))
        assert await s.async_flush() is True
        assert cloud.sent[-1]["loads"][0]["executor"]["held_since"] is None
        await ex.async_stop()
    asyncio.run(body())


def test_held_since_absent_without_hold_and_stale_plan_keeps_it():
    async def body():
        h = _hass()
        ex, clock = await _charging_forced(h)
        assert ex.report_for("dev1")["held_since"] is None
        ex.on_state_event(_event(START_STOP, "No operation", "Stop charging"))
        at = ex.state.manual_at
        await ex.async_ack_manual(at)
        clock.advance(61)              # plan wygenerowany PRZED zdarzeniem: wstrzymanie trwa
        await ex.async_on_plan(_schedule(_slot(minutes=60), meta=_meta(gen=NOW - timedelta(seconds=1))))
        assert ex.report_for("dev1")["held_since"] == at
        clock.advance(61)              # nowszy plan przed potwierdzeniem wysyłki: zdarzenie zostaje, wstrzymanie nie
        ex.on_state_event(_event(STATUS, "Charging", "Available"))
        later = ex.state.manual_at
        clock.advance(61)
        await ex.async_on_plan(_schedule(_slot(minutes=60), meta=_meta(gen=clock.utc)))
        report = ex.report_for("dev1")
        assert report["manual_event"] == "stop" and report["manual_at"] == later
        assert report["held_since"] is None
        await ex.async_stop()
    asyncio.run(body())
