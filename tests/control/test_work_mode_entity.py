"""Odczyt trybu pracy falownika z encji select (tylko odczyt)."""
import pytest

from custom_components.volcast.control.work_mode import WorkModeTracker, classify_entity, normalize_option
from custom_components.volcast.core.entity_map import EntityCandidate, resolve_work_mode_entity
from custom_components.volcast.core.profile import profile_from_dict

from tests.core.profile_fixtures import ms_profile

NEUTRAL = ("general",)


@pytest.mark.parametrize("option,expected", [
    ("general", "general"),
    ("General", "general"),
    ("eco", "other:eco"),
    ("eco_charge", "other:eco_charge"),
    ("peak_shaving", "other:peak_shaving"),
    ("Self Use", "other:self_use"),
    ("Peak-Shaving", "other:peak_shaving"),
    ("x" * 40, "other:" + "x" * 24),
    # znaki spoza [a-z0-9_] dają podkreślenia, więc opcja z samych znaków specjalnych NIE jest pusta
    ("---", "other:___"),
])
def test_classify_entity(option, expected):
    assert classify_entity(option, NEUTRAL) == expected


@pytest.mark.parametrize("option", [None, "", "unavailable", "unknown", "Unavailable"])
def test_unusable_options_are_none(option):
    assert classify_entity(option, NEUTRAL) is None


def test_neutral_compared_after_normalising_both_sides():
    assert classify_entity("Self-Use", ("self use",)) == "general"


def test_normalize_option():
    assert normalize_option("Self Use") == "self_use"
    assert normalize_option("") is None
    assert normalize_option("y" * 30) == "y" * 24


def test_tracker_starts_unknown_and_reports_changes():
    t = WorkModeTracker()
    assert t.state == "unknown"
    assert t.update("general") is True and t.state == "general"
    assert t.update("general") is False
    assert t.update("other:eco") is True and t.state == "other:eco"


def test_tracker_keeps_state_on_failed_read():
    t = WorkModeTracker()
    assert t.update(None) is False and t.state == "unknown"
    t.update("other:eco")
    assert t.update(None) is False and t.state == "other:eco"


def _profile():
    p = ms_profile()
    p["work_mode"] = {"read": {"addr": 35000, "type": "u16"}, "neutral_values": [0],
                      "ha": {"domain": "select", "unique_id_regex": "work_mode$", "neutral_options": ["general"]},
                      "poll_s": 300}
    return profile_from_dict(p, origin="package")


def test_resolve_finds_the_select_by_unique_id():
    cands = [EntityCandidate("select.inv_work_mode", "goodwe", "ABC_work_mode"),
             EntityCandidate("sensor.inv_work_mode", "goodwe", "ABC_work_mode"),
             EntityCandidate("select.other", "goodwe", "ABC_other"),
             EntityCandidate("select.foreign_work_mode", "other", "X_work_mode")]
    assert resolve_work_mode_entity(_profile(), "goodwe", cands) == "select.inv_work_mode"


def test_resolve_no_match_or_ambiguous_or_no_block_is_none():
    assert resolve_work_mode_entity(_profile(), "goodwe", []) is None
    two = [EntityCandidate("select.a", "goodwe", "1_work_mode"), EntityCandidate("select.b", "goodwe", "2_work_mode")]
    assert resolve_work_mode_entity(_profile(), "goodwe", two) is None
    assert resolve_work_mode_entity(profile_from_dict(ms_profile(), origin="package"), "goodwe", two) is None


def test_watcher_feeds_tracker_from_raw_state_and_ignores_unusable():
    from types import SimpleNamespace
    from custom_components.volcast.control.work_mode import WorkModeWatcher
    from .ha_fakes import FakeState

    seen = []
    w = WorkModeWatcher("select.inv_work_mode", NEUTRAL, on_change=seen.append)
    assert w.tracker.state == "unknown"
    ev = lambda s: SimpleNamespace(data={"new_state": None if s is None else FakeState("select.inv_work_mode", s)})
    w.on_event(ev("Self Use"))
    assert w.tracker.state == "other:self_use" and seen == ["other:self_use"]
    w.on_event(ev("unavailable"))
    w.on_event(ev(None))
    w.on_event(ev("Self Use"))
    assert w.tracker.state == "other:self_use" and seen == ["other:self_use"]
    w.on_event(ev("general"))
    assert seen == ["other:self_use", "general"]
    w.prime(FakeState("select.inv_work_mode", "eco"))
    assert w.tracker.state == "other:eco"


def test_runtime_wiring_primes_and_subscribes(monkeypatch):
    from types import SimpleNamespace
    from custom_components.volcast.control import runtime as rt_mod
    from .ha_fakes import FakeState

    cand = EntityCandidate("select.inv_work_mode", "goodwe", "ABC_work_mode")
    monkeypatch.setattr(rt_mod, "_entity_candidates", lambda hass, dom: [cand])
    subs = []
    monkeypatch.setattr(rt_mod, "async_track_state_change_event",
                        lambda hass, ids, cb: subs.append((ids, cb)) or (lambda: None))
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda e: FakeState(e, "Eco Mode")))
    choice = SimpleNamespace(profile=_profile(), integration_domain="goodwe")
    rt = SimpleNamespace(work_mode=WorkModeTracker(), unsubs=[])
    monkeypatch.setattr(rt_mod, "async_track_time_interval", lambda hass, action, interval: (lambda: None))
    rt_mod.start_work_mode_watch(hass, choice, rt)
    # subskrypcja stanu + zegar odświeżania encji (select GoodWe nie odpytuje się sam)
    assert rt.work_mode.state == "other:eco_mode" and subs[0][0] == ["select.inv_work_mode"] and len(rt.unsubs) == 2
    # brak encji -> unknown, bez subskrypcji stanu (jest tylko zegar ponowienia)
    monkeypatch.setattr(rt_mod, "_entity_candidates", lambda hass, dom: [])
    monkeypatch.setattr(rt_mod, "async_track_time_interval", lambda hass, action, interval: (lambda: None))
    rt2 = SimpleNamespace(work_mode=WorkModeTracker(), unsubs=[])
    rt_mod.start_work_mode_watch(hass, choice, rt2)
    assert rt2.work_mode.state == "unknown" and len(rt2.unsubs) == 1
