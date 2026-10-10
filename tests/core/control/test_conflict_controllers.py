"""Drugi sterownik: lista `conflicts` z dowodów i licznik zapisów automatyzacji (czysta logika)."""
from __future__ import annotations

from custom_components.volcast.core.control.conflict import (
    AUTOMATION_CONTEXTS_MAX, AUTOMATION_WINDOW_S, MAX_CONFLICTS, AutomationWriteTracker, controllers_from_evidence,
    entry_conflicts)
from custom_components.volcast.core.control.recommend import recommend

WATCHED = frozenset({"select.goodwe_ems_mode", "number.goodwe_ems_power_limit"})


def _kinds(out):
    return [c["kind"] for c in out]


def test_no_evidence_no_conflicts():
    assert controllers_from_evidence({}, (), None, False) == []


def test_automation_writes_become_one_entry_with_the_count():
    out = controllers_from_evidence({"automation.night_charge": 3}, (), None, False)
    assert out == [{"kind": "automation", "label": "automation.night_charge", "evidence": "3 writes in 24 h"}]


def test_box_active_adds_a_box_entry():
    out = controllers_from_evidence({}, (), None, True)
    assert _kinds(out) == ["box"]
    assert out[0]["label"] and len(out[0]["evidence"]) <= 120


def test_address_clash_and_lan_client_entries():
    out = controllers_from_evidence({}, ("goodwe", "goodwe", "unknown"), "stray_frames", False)
    assert out[:2] == [
        {"kind": "entry", "label": "goodwe", "evidence": "another entry uses the same inverter address"},
        {"kind": "entry", "label": "unknown", "evidence": "conflict check failed"}]
    assert out[2]["kind"] == "lan_client" and out[2]["evidence"]


def test_order_is_deterministic_and_capped_at_eight():
    writes = {f"automation.a{i:02d}": i for i in range(1, 12)}
    out = controllers_from_evidence(writes, ("modbus",), "in_use", True)
    assert len(out) == MAX_CONFLICTS == 8
    assert _kinds(out)[:3] == ["entry", "lan_client", "box"]
    # automatyzacje: najwięcej zapisów pierwsze, przy remisie po etykiecie
    assert [c["label"] for c in out[3:]] == [f"automation.a{i:02d}" for i in (11, 10, 9, 8, 7)]
    assert out == controllers_from_evidence(dict(reversed(list(writes.items()))), ("modbus",), "in_use", True)


def test_shapes_follow_the_contract_limits():
    long = "automation." + "x" * 100
    out = controllers_from_evidence({long: 2, "": 4, "automation.zero": 0}, ("d" * 80,), None, False)
    assert [c["kind"] for c in out] == ["entry", "automation"]
    assert all(len(c["label"]) <= 64 and len(c["evidence"]) <= 120 for c in out)


def test_recommendation_entries_use_the_same_texts():
    assert entry_conflicts(("goodwe",)) == tuple(controllers_from_evidence({}, ("goodwe",), None, False))
    rec = recommend(None, (), None, None, conflicts=("goodwe",))
    assert list(rec.conflicts) == list(entry_conflicts(("goodwe",)))


# ── zapisy automatyzacji ─────────────────────────────────────────────────


def test_automation_writing_three_times_to_a_mapped_entity_counts_three():
    t = AutomationWriteTracker()
    for i in range(3):
        t.note_trigger(f"run{i}", "automation.night_charge", now=100.0 + i)
        assert t.note_call(f"run{i}", None, ["select.goodwe_ems_mode"], WATCHED, now=100.5 + i) \
            == "automation.night_charge"
    assert t.counts(now=200.0) == {"automation.night_charge": 3}


def test_parent_context_of_the_call_matches_the_automation_run():
    t = AutomationWriteTracker()
    t.note_trigger("run1", "automation.a", now=0.0)
    assert t.note_call("child", "run1", "number.goodwe_ems_power_limit", WATCHED, now=1.0) == "automation.a"


def test_write_outside_the_map_is_ignored():
    t = AutomationWriteTracker()
    t.note_trigger("run1", "automation.a", now=0.0)
    assert t.note_call("run1", None, ["light.kitchen"], WATCHED, now=1.0) is None
    assert t.counts(now=2.0) == {}


def test_write_without_an_automation_context_is_ignored():
    t = AutomationWriteTracker()
    t.note_trigger("run1", "automation.a", now=0.0)
    assert t.note_call("user-ctx", None, ["select.goodwe_ems_mode"], WATCHED, now=1.0) is None
    assert t.note_call("user-ctx", "other", ["select.goodwe_ems_mode"], WATCHED, now=1.0) is None
    assert t.counts(now=2.0) == {}


def test_writes_expire_after_24_h():
    t = AutomationWriteTracker()
    t.note_trigger("run1", "automation.a", now=0.0)
    t.note_call("run1", None, ["select.goodwe_ems_mode"], WATCHED, now=1.0)
    assert t.counts(now=AUTOMATION_WINDOW_S) == {"automation.a": 1}
    assert t.counts(now=AUTOMATION_WINDOW_S + 2.0) == {}
    # kontekst starszy niż okno też już nie wiąże zapisu z automatyzacją
    assert t.note_call("run1", None, ["select.goodwe_ems_mode"], WATCHED, now=AUTOMATION_WINDOW_S + 3.0) is None


def test_context_buffer_keeps_only_the_newest_runs():
    t = AutomationWriteTracker()
    for i in range(AUTOMATION_CONTEXTS_MAX + 5):
        t.note_trigger(f"run{i}", "automation.busy", now=float(i))
    assert t.note_call("run0", None, ["select.goodwe_ems_mode"], WATCHED, now=500.0) is None
    assert t.note_call(f"run{AUTOMATION_CONTEXTS_MAX + 4}", None, ["select.goodwe_ems_mode"], WATCHED,
                       now=500.0) == "automation.busy"


def test_bad_inputs_are_ignored():
    t = AutomationWriteTracker()
    t.note_trigger(None, "automation.a", now=0.0)
    t.note_trigger("run1", None, now=0.0)
    t.note_trigger("run2", "light.not_an_automation", now=0.0)
    for ctx in ("run1", "run2"):
        assert t.note_call(ctx, None, ["select.goodwe_ems_mode"], WATCHED, now=1.0) is None
    t.note_trigger("run3", "automation.a", now=0.0)
    assert t.note_call("run3", None, None, WATCHED, now=1.0) is None
    assert t.note_call("run3", None, [None, 5], WATCHED, now=1.0) is None
