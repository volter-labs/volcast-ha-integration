"""Drugi sterownik po stronie HA: automatyzacje piszące do zmapowanych encji, Box, wybór
`controller` (`volcast` | `own_ems`) i tryb „tylko plan”."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from homeassistant.core import Context

from custom_components.volcast.const import SIGNAL_CONTROL_STATE_UPDATED
from custom_components.volcast.control import conflicts as cf_mod
from custom_components.volcast.control.conflicts import ConflictMonitor
from custom_components.volcast.control.runtime import ControlRuntime
from custom_components.volcast.control.store import ControlStore
from custom_components.volcast.core.control.ladder import IDLE, RUNNING, STOPPED

from .ha_fakes import GOODWE_ENTITIES as E, FakeState
from .test_executor import make, ready
from .test_verification import Env, FakeExecutor, foreign_event

ISSUE = "controller_conflict_e1"
SELECT = E["mode"]


class Bus:
    def __init__(self):
        self.listeners: dict[str, list] = {}

    def async_listen(self, event_type, cb):
        self.listeners.setdefault(event_type, []).append(cb)

        def remove():
            self.listeners[event_type].remove(cb)
        return remove

    def fire(self, event_type, data, context):
        for cb in list(self.listeners.get(event_type, ())):
            cb(SimpleNamespace(event_type=event_type, data=data, context=context))


class Clock:
    def __init__(self):
        self.t = 5000.0

    def __call__(self):
        return self.t


class FakeEx:
    """Wykonawca widziany przez monitor: encje zapisu, tryb „tylko plan”, potwierdzone konflikty."""

    def __init__(self, watched=(SELECT, E["power_w"])):
        self.write_entity_ids = frozenset(watched)
        self.plan_only = False
        self.conflict_ack: list = []

    async def async_save_conflict_ack(self, pairs):
        self.conflict_ack = [list(p) for p in pairs]
        return True


class Env6:
    def __init__(self, monkeypatch, *, executor=None, verification=None, hass=None):
        self.created, self.deleted, self.signals = [], [], []
        monkeypatch.setattr(cf_mod.ir, "async_create_issue",
                            lambda hass, domain, issue_id, **kw: self.created.append((issue_id, kw)))
        monkeypatch.setattr(cf_mod.ir, "async_delete_issue",
                            lambda hass, domain, issue_id: self.deleted.append(issue_id))
        monkeypatch.setattr(cf_mod, "async_dispatcher_send", lambda hass, sig: self.signals.append(sig))
        monkeypatch.setattr(cf_mod, "async_dispatcher_connect", lambda hass, sig, cb: (lambda: None))
        self.clock = Clock()
        self.bus = Bus()
        self.hass = hass or SimpleNamespace(data={})
        self.hass.bus = self.bus
        self.tasks: list = []
        self.hass.async_create_task = lambda coro, *a, **k: self.tasks.append(asyncio.ensure_future(coro))
        self.entry = SimpleNamespace(entry_id="e1", options={"control_mode": "entities"})
        _entry_tasks(self.entry, self.tasks)
        self.ex = executor or FakeEx()
        self.mon = ConflictMonitor(self.hass, self.entry, self.ex, verification=verification, clock=self.clock)

    async def start(self):
        """Start monitora; start kasuje zgłoszenie z poprzedniego przebiegu — tego nie liczymy."""
        await self.mon.async_start()
        assert ISSUE in self.deleted
        self.deleted.clear()
        self.created.clear()

    async def settle(self):
        while self.tasks:
            await self.tasks.pop(0)

    async def automation_write(self, automation="automation.night_charge", entity_id=SELECT, *, run=None,
                               service="select_option"):
        run = run or f"run{len(self.bus.listeners)}{self.clock.t}"
        ctx = Context(id=run)
        self.bus.fire("automation_triggered", {"entity_id": automation, "name": "x"}, ctx)
        self.bus.fire("call_service", {"domain": entity_id.split(".")[0], "service": service,
                                       "service_data": {"entity_id": entity_id, "option": "eco"}}, ctx)
        self.clock.t += 60
        await self.settle()


# ── wykrywanie ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_automation_writing_three_times_to_a_mapped_select_is_one_conflict(monkeypatch):
    env = Env6(monkeypatch)
    await env.start()
    for _ in range(3):
        await env.automation_write()
    assert env.mon.conflicts() == [
        {"kind": "automation", "label": "automation.night_charge", "evidence": "3 writes in 24 h"}]
    (issue_id, kw), = {i: (i, k) for i, k in env.created}.values()
    assert issue_id == ISSUE and kw["translation_key"] == "controller_conflict"
    assert kw["translation_placeholders"]["label"] == "automation.night_charge"
    assert SIGNAL_CONTROL_STATE_UPDATED.format(entry_id="e1") in env.signals


@pytest.mark.asyncio
async def test_write_outside_the_map_is_ignored(monkeypatch):
    env = Env6(monkeypatch)
    await env.start()
    await env.automation_write(entity_id="light.kitchen", service="turn_on")
    assert env.mon.conflicts() == [] and env.created == []


@pytest.mark.asyncio
async def test_write_without_an_automation_context_is_ignored(monkeypatch):
    env = Env6(monkeypatch)
    await env.start()
    env.bus.fire("call_service", {"domain": "select", "service": "select_option",
                                  "service_data": {"entity_id": [SELECT]}}, Context(user_id="u1"))
    await env.settle()
    assert env.mon.conflicts() == [] and env.created == []


@pytest.mark.asyncio
async def test_box_active_adds_a_box_entry(monkeypatch):
    env = Env6(monkeypatch)
    await env.start()
    await env.mon.async_set_box_active(True)
    assert [c["kind"] for c in env.mon.conflicts()] == ["box"]
    await env.mon.async_set_box_active(False)
    assert env.mon.conflicts() == [] and env.deleted[-1] == ISSUE


@pytest.mark.asyncio
async def test_evidence_expires_after_24_h_and_the_repair_goes(monkeypatch):
    env = Env6(monkeypatch)
    await env.start()
    await env.automation_write()
    env.clock.t += 24 * 3600
    await env.mon.async_refresh()
    assert env.mon.conflicts() == [] and env.deleted[-1] == ISSUE


@pytest.mark.asyncio
async def test_count_only_changes_are_signalled_at_most_hourly(monkeypatch):
    env = Env6(monkeypatch)
    await env.start()
    await env.automation_write()
    n = len(env.signals)
    await env.automation_write()                       # sam licznik — bez sygnału w tej godzinie
    assert len(env.signals) == n and env.mon.conflicts()[0]["evidence"] == "2 writes in 24 h"
    await env.automation_write(automation="automation.other")   # nowa para — od razu
    assert len(env.signals) == n + 1
    env.clock.t += 3600
    await env.automation_write()
    assert len(env.signals) == n + 2


@pytest.mark.asyncio
async def test_listeners_are_removed_on_stop(monkeypatch):
    env = Env6(monkeypatch)
    await env.start()
    env.mon.stop()
    assert all(not cbs for cbs in env.bus.listeners.values())


# ── drabina ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_new_conflict_while_the_ladder_runs_stops_it_with_controller_conflict(monkeypatch):
    lad = Env(monkeypatch)
    await lad.runner.async_start()
    assert lad.state == (3, RUNNING)
    env = Env6(monkeypatch, verification=lad.runner)
    await env.start()
    await env.mon.async_set_box_active(True)
    assert lad.state == (3, STOPPED) and lad.runner.ladder.state.stop_reason == "controller_conflict"
    assert lad.ex.restores == 1


@pytest.mark.asyncio
async def test_acknowledged_conflict_does_not_stop_the_ladder_again(monkeypatch):
    lad = Env(monkeypatch)
    await lad.runner.async_start()
    ex = FakeEx()
    ex.conflict_ack = [["box", "Volcast Box"]]              # wybór `volcast` sprzed restartu
    env = Env6(monkeypatch, executor=ex, verification=lad.runner)
    await env.start()
    await env.mon.async_set_box_active(True)
    assert lad.state == (3, RUNNING)
    await env.automation_write()                          # nowa para — nowy konflikt
    assert lad.state == (3, STOPPED)


@pytest.mark.asyncio
async def test_plan_only_suppresses_repairs_and_ladder_stops(monkeypatch):
    lad = Env(monkeypatch)
    await lad.runner.async_start()
    ex = FakeEx()
    ex.plan_only = True
    env = Env6(monkeypatch, executor=ex, verification=lad.runner)
    await env.start()
    await env.automation_write()
    assert env.mon.conflicts() and env.created == [] and lad.state == (3, RUNNING)


# ── wybór sterownika (handlery dla chmury) ───────────────────────────────


def _event(eid, state, ctx):
    return SimpleNamespace(data={"entity_id": eid, "new_state": FakeState(eid, state, {}, context=ctx)},
                           context=ctx)


def _entry_tasks(entry, tasks: list):
    """Zadania wpisu (jak `ConfigEntry.async_create_background_task`) do listy testu."""
    entry.async_create_background_task = lambda hass, coro, name=None, **_k: tasks.append(asyncio.ensure_future(coro))
    return entry


def _runtime(monkeypatch, *, verification=None):
    h, ex = make(monkeypatch=monkeypatch)
    updates = []

    def update_entry(entry, *, options=None, **_kw):
        updates.append(dict(options))
        entry.options = dict(options)
    h.config_entries = SimpleNamespace(async_update_entry=update_entry)
    env = Env6(monkeypatch, executor=ex, verification=verification, hass=h)
    env.entry = _entry_tasks(ex._entry, env.tasks)
    env.mon = ConflictMonitor(h, ex._entry, ex, verification=verification, clock=env.clock)
    rt = ControlRuntime(ex, None, None, None, None, dict(E), 8000.0, verification=verification,
                        conflicts=env.mon, hass=h, entry=ex._entry)
    return h, ex, env, rt, updates


@pytest.mark.asyncio
async def test_own_ems_sets_plan_only_restores_and_stops_repairs(monkeypatch):
    h, ex, env, rt, updates = _runtime(monkeypatch)
    await ready(ex)
    await ex.async_tick()
    assert ex.owned and h.states.get(SELECT).state == "sell_power"
    await env.start()
    await env.automation_write()
    assert any(i == ISSUE for i, _ in env.created)

    assert await rt.async_apply_controller_choice("own_ems") == "applied"
    assert ex.plan_only and not ex.owned and h.states.get(SELECT).state == "auto"
    assert updates and "control_mode" not in updates[-1]          # sterowanie wyłączone w opcjach
    assert ISSUE in env.deleted
    saved = await ex._store.async_load()
    assert saved.plan_only is True

    env.created.clear()                                          # (ir wspólne z wykonawcą w atrapie)
    await env.automation_write()
    await ex.async_on_state_event(_event(SELECT, "eco", Context(parent_id="auto1")))
    assert env.created == [] and not ex.paused and env.mon.conflicts()
    n = len(h.services.calls)
    await ex.async_tick()
    assert len(h.services.calls) == n                             # tylko plan: zero zapisów


@pytest.mark.asyncio
async def test_volcast_clears_plan_only_keeps_the_repair_and_resumes_the_ladder(monkeypatch):
    lad = Env(monkeypatch)
    await lad.runner.async_start()
    h, ex, env, rt, _ = _runtime(monkeypatch, verification=lad.runner)
    await ex.async_start()
    await ex.async_set_plan_only(True)
    ex._state.plan_only = False                                  # konflikt dopiero po wyjściu z trybu
    await env.start()
    await env.mon.async_set_box_active(True)
    assert lad.state == (3, STOPPED)
    ex._state.plan_only = True

    assert await rt.async_apply_controller_choice("volcast") == "applied"
    assert ex.plan_only is False
    assert lad.state == (3, RUNNING)                             # drabina wznowiona
    assert env.mon.conflicts() and ISSUE not in env.deleted       # naprawa zostaje do końca dowodu
    assert ["box", "Volcast Box"] in ex.conflict_ack
    saved = await ex._store.async_load()
    assert saved.plan_only is False and ["box", "Volcast Box"] in saved.conflict_ack


@pytest.mark.asyncio
async def test_unknown_controller_choice_is_ignored(monkeypatch):
    _, ex, _, rt, updates = _runtime(monkeypatch)
    await ex.async_start()
    assert await rt.async_apply_controller_choice("box") == "ignored"
    assert updates == [] and not ex.plan_only


@pytest.mark.asyncio
async def test_control_state_payload_merges_conflicts(monkeypatch):
    _, ex, env, rt, _ = _runtime(monkeypatch)
    await ex.async_start()
    await env.start()
    await rt.async_set_box_active(True)
    payload = rt.control_state_payload()
    assert payload["conflicts"] == env.mon.conflicts() and payload["conflicts"][0]["kind"] == "box"
    assert "recommendation" not in payload and "verification" not in payload


def test_store_round_trip_of_conflict_ack(monkeypatch):
    h, ex = make(monkeypatch=monkeypatch)
    store = ControlStore(h, "e1")

    async def go():
        st = await store.async_load()
        st.conflict_ack = [["automation", "automation.a"], ["bogus", "x"], ["box"], ["box", 5]]
        await store.async_save(st)
        return await store.async_load()
    assert asyncio.run(go()).conflict_ack == [["automation", "automation.a"]]


@pytest.mark.asyncio
async def test_plan_only_closes_the_gates_before_the_options_reload(monkeypatch):
    h, ex = make(monkeypatch=monkeypatch)
    await ready(ex)
    await ex.async_tick()
    assert ex.owned and ex._entry.options["control_mode"] == "entities"
    await ex.async_set_plan_only(True)
    await ex.async_tick()                                        # opcje jeszcze „entities”
    assert not ex.owned and h.states.get(SELECT).state == "auto" and not ex.verification_can_write()
    n = len(h.services.calls)
    await ex.async_tick()
    assert len(h.services.calls) == n


@pytest.mark.asyncio
async def test_update_entity_is_not_a_write(monkeypatch):
    env = Env6(monkeypatch)
    await env.start()
    ctx = Context(id="run-refresh")
    env.bus.fire("automation_triggered", {"entity_id": "automation.refresh"}, ctx)
    env.bus.fire("call_service", {"domain": "homeassistant", "service": "update_entity",
                                  "service_data": {"entity_id": [SELECT]}}, ctx)
    await env.settle()
    assert env.mon.conflicts() == [] and env.created == []


# ── own_ems odstawia drabinę po cichu, volcast rusza ją od początku ─────────


class LinkedEx(FakeExecutor):
    """Wykonawca drabiny z trybem „tylko plan” prawdziwego wykonawcy (jak jeden obiekt w HA)."""
    real = None

    @property
    def plan_only(self):
        return self.real is not None and self.real.plan_only


async def _ladder_and_runtime(monkeypatch, *, start=1):
    lad = Env(monkeypatch, executor=LinkedEx(), start=start)
    await lad.runner.async_start()
    h, ex, env, rt, updates = _runtime(monkeypatch, verification=lad.runner)
    lad.ex.real = ex
    await ex.async_start()
    return lad, ex, env, rt


@pytest.mark.asyncio
async def test_own_ems_during_the_trial_parks_the_ladder_quietly(monkeypatch):
    lad, ex, env, rt = await _ladder_and_runtime(monkeypatch)
    assert lad.state == (3, RUNNING)
    signals = len(lad.changes)
    assert await rt.async_apply_controller_choice("own_ems") == "applied"
    assert lad.state == (1, IDLE) and "stop_reason" not in lad.runner.payload()
    assert lad.runner._timer is None and len(lad.changes) == signals + 1     # tylko sygnał zmiany stanu
    # zdarzenia i kroki w trybie „tylko plan” nic nie zmieniają: bez stopu, naprawy i pusha
    await lad.runner.async_on_state_event(foreign_event())
    await lad.runner.async_conflict()
    await lad.step(hours=30)
    assert lad.state == (1, IDLE) and lad.urgent == [] and lad.ex.restores == 0
    assert not any(i.startswith("verification_stopped") for i, _ in env.created)
    assert lad.ex.verification_record["state"] == IDLE


@pytest.mark.asyncio
async def test_own_ems_while_the_control_write_waits_for_the_lock_skips_the_write(monkeypatch):
    h, ex = make(monkeypatch=monkeypatch)
    await ready(ex)
    n = len(h.services.calls)
    assert await ex.async_control_write() is True and len(h.services.calls) == n + 1
    n = len(h.services.calls)
    async with ex._lock:
        task = asyncio.ensure_future(ex.async_control_write())
        await asyncio.sleep(0)
        await ex.async_set_plan_only(True)
    assert await task is None and len(h.services.calls) == n


@pytest.mark.asyncio
async def test_volcast_after_own_ems_restarts_the_ladder_from_its_start(monkeypatch):
    lad, ex, env, rt = await _ladder_and_runtime(monkeypatch, start=4)
    assert lad.ex.control_writes == [1] and lad.state == (5, RUNNING)          # okno próbne trwa
    await rt.async_apply_controller_choice("own_ems")
    assert lad.state == (4, IDLE) and lad.ex.window is None and lad.ex.restores == 1
    assert await rt.async_apply_controller_choice("volcast") == "applied"
    assert not ex.plan_only
    assert lad.ex.control_writes == [1, 1] and lad.state == (5, RUNNING)       # od zapisu kontrolnego
