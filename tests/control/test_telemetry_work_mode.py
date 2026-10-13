"""Tryb pracy falownika w telemetrii (`driver.meta.work_mode`) i zgłoszenie w Naprawach."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from custom_components.volcast.control import runtime as rt_mod
from custom_components.volcast.control import telemetry as tm
from custom_components.volcast.control.work_mode import WorkModeTracker, classify_direct
from custom_components.volcast.core.control.readings import SOURCE_KEYS
from custom_components.volcast.core.entity_map import EntityCandidate
from custom_components.volcast.core.profile import profile_from_dict
from tests.control.test_work_mode_direct import _wm_profile
from tests.core.profile_fixtures import ms_profile

from .ha_fakes import FakeState


def _plain():
    return profile_from_dict(ms_profile())


def _direct(profile, **kw):
    return tm.direct_driver_block(profile=profile, access="direct", capabilities=None, local_switch=False,
                                  limits=None, **kw)


# ── telemetria ──


def test_direct_block_has_work_mode_when_profile_declares_it():
    assert _direct(_wm_profile(), work_mode="unknown")["meta"] == {"work_mode": "unknown"}
    out = _direct(_wm_profile(), readings_source={"soc": "register"}, work_mode="other:3")
    assert out["meta"] == {"readings_source": {"soc": "register"}, "work_mode": "other:3"}


def test_direct_block_without_profile_block_has_no_key():
    out = _direct(_plain(), readings_source={"soc": "register"}, work_mode="general")
    assert "work_mode" not in out["meta"]
    assert "meta" not in _direct(_plain(), work_mode="general")


def test_direct_block_defaults_to_unknown_when_tracker_missing():
    assert _direct(_wm_profile())["meta"]["work_mode"] == "unknown"


def test_entity_driver_block_work_mode():
    choice = SimpleNamespace(profile=_wm_profile(), integration_domain=None)
    out = tm.driver_block(choice=choice, control_mode=None, mapped_keys=[], local_switch=False, limits=None,
                          work_mode="general")
    assert out["meta"]["work_mode"] == "general"
    choice = SimpleNamespace(profile=_plain(), integration_domain=None)
    out = tm.driver_block(choice=choice, control_mode=None, mapped_keys=[], local_switch=False, limits=None,
                          work_mode="general")
    assert "meta" not in out


def test_meta_with_all_sources_fits_ceiling():
    src = dict.fromkeys(SOURCE_KEYS, "register")
    meta = _direct(_wm_profile(), readings_source=src, work_mode="other:" + "x" * 24)["meta"]
    assert len(json.dumps(meta).encode()) <= 1500


# ── Naprawy ──


@pytest.fixture
def issues(monkeypatch):
    calls = []
    monkeypatch.setattr(rt_mod.ir, "async_create_issue",
                        lambda hass, domain, iid, **kw: calls.append(("create", iid, kw)))
    monkeypatch.setattr(rt_mod.ir, "async_delete_issue", lambda hass, domain, iid: calls.append(("delete", iid)))
    return calls


def _wired():
    rt = SimpleNamespace(work_mode=WorkModeTracker(), unsubs=[])
    rt_mod.wire_work_mode(object(), rt, None)
    return rt


def test_issue_created_on_transition_and_deleted_on_general(issues):
    rt = _wired()
    assert rt.work_mode.update("general") is True
    assert [c[0] for c in issues] == ["delete"]           # general zawsze porządkuje (idempotentne)
    issues.clear()
    rt.work_mode.update("other:3")
    rt.work_mode.update("other:3")                        # ta sama wartość: bez nowej zmiany
    assert [c[0] for c in issues] == ["create"]
    assert issues[0][1] == "inverter_work_mode"
    assert issues[0][2]["is_fixable"] is False and issues[0][2]["translation_key"] == "inverter_work_mode"
    rt.work_mode.update(None)                             # nieudany odczyt: bez zmian
    assert [c[0] for c in issues] == ["create"]
    rt.work_mode.update("general")
    assert issues[-1] == ("delete", "inverter_work_mode")


def test_issue_created_on_first_read_other(issues):
    rt = _wired()                                         # unknown -> other (restart HA z falownikiem w Eco)
    rt.work_mode.update("other:1")
    assert [c[0] for c in issues] == ["create"]


def test_unknown_never_touches_issue(issues):
    rt = _wired()
    rt.work_mode.update("unknown")
    assert issues == []


# ── wspólny tracker (tryb bezpośredni) ──


def test_direct_connection_shares_runtime_tracker(issues):
    conn = SimpleNamespace(work_mode=WorkModeTracker())
    rt = SimpleNamespace(work_mode=WorkModeTracker(), unsubs=[])
    rt_mod.wire_work_mode(object(), rt, conn)
    assert conn.work_mode is rt.work_mode
    conn.work_mode.update(classify_direct(3, (0,)))       # tak karmi go DirectConnection
    assert rt.work_mode.state == "other:3" and [c[0] for c in issues] == ["create"]


# ── ponowienie rozwiązania encji ──


def test_entity_retry_resolves_when_entity_appears_then_stops(monkeypatch):
    cands: list = []
    monkeypatch.setattr(rt_mod, "_entity_candidates", lambda hass, dom: list(cands))
    subs, ticks, stopped = [], [], []
    monkeypatch.setattr(rt_mod, "async_track_state_change_event",
                        lambda hass, ids, cb: subs.append(ids) or (lambda: None))

    def track(hass, action, interval):
        ticks.append(action)
        return lambda: stopped.append(True)
    monkeypatch.setattr(rt_mod, "async_track_time_interval", track)
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda e: FakeState(e, "Eco Mode")))
    choice = SimpleNamespace(profile=_wm_profile(), integration_domain="goodwe")
    rt = SimpleNamespace(work_mode=WorkModeTracker(), unsubs=[])
    rt_mod.start_work_mode_watch(hass, choice, rt)
    assert rt.work_mode.state == "unknown" and len(ticks) == 1 and subs == []
    ticks[0](None)                                         # encji jeszcze nie ma
    assert subs == [] and stopped == []
    cands.append(EntityCandidate("select.inv_work_mode", "goodwe", "ABC_work_mode"))
    ticks[0](None)
    assert rt.work_mode.state == "other:eco_mode" and subs == [["select.inv_work_mode"]] and stopped == [True]
    ticks[0](None)                                         # po rozwiązaniu nic więcej
    assert len(subs) == 1
