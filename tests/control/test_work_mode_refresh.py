"""Tryb encji: select trybu pracy GoodWe nie odpytuje się sam (`should_poll=False`), więc integracja wymusza
odświeżenie (`homeassistant.update_entity`) co `work_mode.poll_s` — inaczej późniejsza zmiana trybu
nie dociera."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from custom_components.volcast.control import runtime as rt_mod
from custom_components.volcast.control.work_mode import WorkModeTracker
from custom_components.volcast.core.entity_map import EntityCandidate
from tests.control.test_work_mode_direct import _wm_profile

from .ha_fakes import FakeState

EID = "select.inv_work_mode"
CAND = EntityCandidate(EID, "goodwe", "ABC_work_mode")


class _Hass:
    """Stan encji + wywołania usług; zadania w tle zbierane do ręcznego uruchomienia."""

    def __init__(self, option="General") -> None:
        self.option = option
        self.calls: list[tuple] = []
        self.tasks: list = []
        self.on_update = None                     # symulacja: update_entity odczytuje falownik i zmienia stan
        self.states = SimpleNamespace(get=lambda e: FakeState(e, self.option))
        self.services = SimpleNamespace(async_call=self._call)

    async def _call(self, domain, service, data, blocking=False, **kw):
        self.calls.append((domain, service, dict(data), blocking))
        if self.on_update is not None:
            self.on_update()

    def async_create_background_task(self, coro, name, *a, **kw):
        self.tasks.append(coro)

    async def run_tasks(self) -> None:
        while self.tasks:
            await self.tasks.pop(0)


@pytest.fixture
def wired(monkeypatch):
    """Podmienia zegary i subskrypcję stanu; zwraca rejestr zegarów/subskrypcji."""
    reg = SimpleNamespace(timers=[], subs=[], stopped=[], cands=[CAND])
    monkeypatch.setattr(rt_mod, "_entity_candidates", lambda hass, dom: list(reg.cands))

    def track_state(hass, ids, cb):
        reg.subs.append((ids, cb))
        return lambda: None

    def track_time(hass, action, interval):
        idx = len(reg.timers)
        reg.timers.append((action, interval))
        return lambda: reg.stopped.append(idx)

    monkeypatch.setattr(rt_mod, "async_track_state_change_event", track_state)
    monkeypatch.setattr(rt_mod, "async_track_time_interval", track_time)
    return reg


def _rt(direct=None, entry_id="e1"):
    return SimpleNamespace(work_mode=WorkModeTracker(), unsubs=[], direct=direct,
                           entry=SimpleNamespace(entry_id=entry_id))


def _choice():
    return SimpleNamespace(profile=_wm_profile(poll_s=900), integration_domain="goodwe")


def _update_calls(hass):
    return [c for c in hass.calls if c[:2] == ("homeassistant", "update_entity")]


@pytest.mark.asyncio
async def test_refresh_timer_registered_at_poll_interval_and_refreshes_right_away(wired):
    hass, rt = _Hass(), _rt()
    rt_mod.start_work_mode_watch(hass, _choice(), rt)
    assert [i for _a, i in wired.timers] == [timedelta(seconds=900)]
    await hass.run_tasks()
    assert _update_calls(hass) == [("homeassistant", "update_entity", {"entity_id": EID}, False)]


@pytest.mark.asyncio
async def test_each_tick_requests_update_of_the_resolved_entity(wired):
    hass, rt = _Hass(), _rt()
    rt_mod.start_work_mode_watch(hass, _choice(), rt)
    await hass.run_tasks()
    tick = wired.timers[0][0]
    tick(None)
    tick(None)
    await hass.run_tasks()
    assert _update_calls(hass) == [("homeassistant", "update_entity", {"entity_id": EID}, False)] * 3


@pytest.mark.asyncio
async def test_no_refresh_while_entity_unresolved_then_starts_after_resolve(wired):
    wired.cands = []
    hass, rt = _Hass(), _rt()
    rt_mod.start_work_mode_watch(hass, _choice(), rt)
    assert len(wired.timers) == 1                                # tylko zegar ponowienia rozwiązania encji
    retry = wired.timers[0][0]
    retry(None)
    await hass.run_tasks()
    assert hass.calls == []
    wired.cands = [CAND]
    retry(None)
    await hass.run_tasks()
    assert [i for _a, i in wired.timers][1:] == [timedelta(seconds=900)]
    assert _update_calls(hass) == [("homeassistant", "update_entity", {"entity_id": EID}, False)]


@pytest.mark.asyncio
async def test_no_refresh_in_direct_mode(wired):
    hass, rt = _Hass(), _rt(direct=object())                     # rejestr z połączenia bezpośredniego karmi tracker
    rt_mod.start_work_mode_watch(hass, _choice(), rt)
    await hass.run_tasks()
    assert wired.timers == [] and hass.calls == []


@pytest.mark.asyncio
async def test_refresh_timer_stops_on_unload(wired):
    hass, rt = _Hass(), _rt()
    rt_mod.start_work_mode_watch(hass, _choice(), rt)
    await hass.run_tasks()
    for unsub in rt.unsubs:
        unsub()
    assert wired.stopped == [0]


@pytest.mark.asyncio
async def test_refresh_errors_are_swallowed(wired):
    hass, rt = _Hass(), _rt()

    async def boom(*a, **kw):
        raise RuntimeError("service not ready")
    hass.services = SimpleNamespace(async_call=boom)
    rt_mod.start_work_mode_watch(hass, _choice(), rt)
    await hass.run_tasks()                                       # bez wyjątku
    wired.timers[0][0](None)
    await hass.run_tasks()
    no_tasks = SimpleNamespace(states=hass.states, services=hass.services)   # brak async_create_background_task
    rt2 = _rt()
    rt_mod.start_work_mode_watch(no_tasks, _choice(), rt2)
    wired.timers[-1][0](None)


def test_refresh_action_is_an_event_loop_callback(monkeypatch, wired):
    def mark(f):
        if not hasattr(f, "__self__"):                           # metoda związana (watcher) nie przyjmie atrybutu
            f._hass_callback = True
        return f
    monkeypatch.setattr(rt_mod, "callback", mark)
    hass, rt = _Hass(), _rt()
    rt_mod.start_work_mode_watch(hass, _choice(), rt)
    for t in hass.tasks:
        t.close()
    assert getattr(wired.timers[0][0], "_hass_callback", False)


@pytest.fixture
def issues(monkeypatch):
    calls = []
    monkeypatch.setattr(rt_mod.ir, "async_create_issue",
                        lambda hass, domain, iid, **kw: calls.append(("create", iid)))
    monkeypatch.setattr(rt_mod.ir, "async_delete_issue", lambda hass, domain, iid: calls.append(("delete", iid)))
    return calls


@pytest.mark.asyncio
async def test_mode_switched_later_is_seen_after_refresh_and_raises_then_clears_repairs(wired, issues):
    hass, rt = _Hass(option="General"), _rt()
    rt_mod.wire_work_mode(hass, rt, None)
    rt_mod.start_work_mode_watch(hass, _choice(), rt)
    await hass.run_tasks()
    assert rt.work_mode.state == "general"
    on_event = wired.subs[0][1]

    def emit():                                                  # GoodWe async_update -> nowy stan encji
        on_event(SimpleNamespace(data={"new_state": FakeState(EID, hass.option)}))
    hass.on_update = emit
    issues.clear()
    hass.option = "Eco"                                          # przełączenie w aplikacji producenta
    wired.timers[0][0](None)
    await hass.run_tasks()
    assert rt.work_mode.state == "other:eco" and issues == [("create", "inverter_work_mode_e1")]
    hass.option = "General"                                      # użytkownik wyłączył tryb
    wired.timers[0][0](None)
    await hass.run_tasks()
    assert rt.work_mode.state == "general" and issues[-1] == ("delete", "inverter_work_mode_e1")
