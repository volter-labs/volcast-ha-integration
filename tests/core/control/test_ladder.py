"""Drabina weryfikacji urządzenia (czysta maszyna stanów): szczeble 1–5, zgoda, stopy, ponowienie,
zapis w magazynie i blok `verification` kontraktu sterowania."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from custom_components.volcast.core.control.ladder import (
    IDLE, RUNNING, STOP_REASONS, STOPPED, VERIFIED, WAITING, Ladder, LadderParams, device_key)

T0 = datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc)
P = LadderParams(trial_hours=24, window_minutes=15, window_power_w=500)
KEY = "3f9a1c0e7b2d4a6f3f9a1c0e7b2d4a6f"
FIXTURE = Path(__file__).parents[2] / "fixtures" / "control_block.json"

# ── kształt bloku `verification` według kontraktu (§3.1, §3.3) ──
_KEY_RE = re.compile(r"^[a-f0-9]{16,32}$")
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")
_STATES = {"idle", "running", "waiting", "stopped", "verified"}
_ALLOWED = {"device_key", "rung", "state", "since", "next_at", "stop_reason", "stop_detail", "trial", "window",
            "migrated"}


def _int(v, lo=0, hi=None):
    return isinstance(v, int) and not isinstance(v, bool) and v >= lo and (hi is None or v <= hi)


def _num(v, lo=None):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and (lo is None or v >= lo)


def shape_errors(block) -> list[str]:
    errs = []
    if not isinstance(block, dict):
        return ["not an object"]
    errs += [f"unknown {k}" for k in block if k not in _ALLOWED]
    key = block.get("device_key")
    if not (isinstance(key, str) and _KEY_RE.fullmatch(key) and re.search("[a-f]", key)):
        errs.append("device_key")
    if not _int(block.get("rung"), 0, 5):
        errs.append("rung")
    if block.get("state") not in _STATES:
        errs.append("state")
    for name, required in (("since", True), ("next_at", False)):
        v = block.get(name)
        if v is None and not required:
            continue
        if not (isinstance(v, str) and len(v) <= 40 and _ISO_RE.fullmatch(v)):
            errs.append(name)
    if "stop_reason" in block and (block["stop_reason"] not in STOP_REASONS or block.get("state") != "stopped"):
        errs.append("stop_reason")
    if "migrated" in block and block["migrated"] is not True:
        errs.append("migrated")
    if "stop_detail" in block and not (isinstance(block["stop_detail"], str) and len(block["stop_detail"]) <= 120):
        errs.append("stop_detail")
    trial = block.get("trial")
    if trial is not None and not (isinstance(trial, dict) and _int(trial.get("would_write"))
                                  and _int(trial.get("foreign_writes")) and _num(trial.get("hours_done"), 0)
                                  and set(trial) <= {"would_write", "foreign_writes", "hours_done"}):
        errs.append("trial")
    window = block.get("window")
    if window is not None and not (isinstance(window, dict) and _int(window.get("target_w"), 0, 100000)
                                   and set(window) <= {"target_w", "measured_w", "deviation_pct"}
                                   and ("measured_w" not in window or _int(window["measured_w"], -10**6))
                                   and ("deviation_pct" not in window or _num(window["deviation_pct"]))):
        errs.append("window")
    return errs


def started(start=1, key=KEY) -> Ladder:
    lad = Ladder(start, P, device_key=key)
    lad.tick(T0)
    return lad


def in_trial() -> Ladder:
    lad = started(1)
    lad.identify_ok(T0)
    lad.read_ok(T0)
    return lad


def to_trial_end(lad: Ladder, *, consent=True) -> datetime:
    lad.identify_ok(T0)
    lad.read_ok(T0)
    lad.consent(consent, T0)
    end = T0 + timedelta(hours=24)
    lad.tick(end)
    return end


def to_window(lad: Ladder) -> datetime:
    t = to_trial_end(lad)
    lad.write_result(True, t)
    lad.window_open(t)
    return t


# ── przebieg ─────────────────────────────────────────────────────────────


def test_fixture_verification_block_matches_the_contract_shape():
    block = json.loads(FIXTURE.read_text(encoding="utf-8"))["verification"]
    assert shape_errors(block) == []


def test_new_ladder_is_idle_at_its_start_rung_until_the_first_tick():
    lad = Ladder(1, P, device_key=KEY)
    assert (lad.state.rung, lad.state.state) == (1, IDLE)
    lad.tick(T0)
    assert (lad.state.rung, lad.state.state, lad.state.since) == (1, RUNNING, T0)
    assert shape_errors(lad.to_payload()) == []


def test_full_draft_run_with_simulated_time_ends_verified():
    lad = started(1)
    lad.identify_ok(T0 + timedelta(seconds=10))
    assert (lad.state.rung, lad.state.state) == (2, RUNNING)
    lad.read_ok(T0 + timedelta(seconds=20))
    assert (lad.state.rung, lad.state.state) == (3, RUNNING)
    assert lad.state.next_at == T0 + timedelta(hours=24, seconds=20)
    lad.would_write()
    lad.would_write()
    lad.consent(True, T0 + timedelta(hours=1))
    lad.tick(T0 + timedelta(hours=12, seconds=20))
    assert (lad.state.rung, lad.state.state) == (3, RUNNING)
    assert lad.to_payload()["trial"] == {"would_write": 2, "foreign_writes": 0, "hours_done": 12.0}
    end = T0 + timedelta(hours=24, seconds=20)
    lad.tick(end)
    assert (lad.state.rung, lad.state.state) == (4, RUNNING)
    assert lad.to_payload()["trial"]["hours_done"] == 24.0
    lad.write_result(True, end)
    assert (lad.state.rung, lad.state.state) == (5, WAITING)
    lad.window_open(end + timedelta(minutes=1))
    w0 = end + timedelta(minutes=1)
    assert (lad.state.rung, lad.state.state, lad.state.next_at) == (5, RUNNING, w0 + timedelta(minutes=15))
    for i, (p, soc) in enumerate([(480.0, 50.0), (500.0, 50.0), (510.0, 51.0)]):
        lad.window_sample(p, soc, w0 + timedelta(minutes=5 * i))
    lad.tick(w0 + timedelta(minutes=15))
    assert (lad.state.rung, lad.state.state) == (5, VERIFIED)
    payload = lad.to_payload()
    assert payload["window"] == {"target_w": 500, "measured_w": 497, "deviation_pct": 0.7}
    assert payload["state"] == "verified" and "next_at" not in payload and "stop_reason" not in payload
    assert shape_errors(payload) == []
    assert lad.verified


def test_verified_profile_starts_at_the_control_write():
    lad = Ladder(4, P, device_key=KEY)
    lad.consent(True, T0)
    lad.tick(T0)
    assert (lad.state.rung, lad.state.state, lad.state.next_at) == (4, RUNNING, None)
    assert "trial" not in lad.to_payload()


def test_verified_profile_without_consent_waits_at_the_control_write():
    lad = started(4)
    assert (lad.state.rung, lad.state.state) == (4, WAITING)
    lad.consent(False, T0 + timedelta(hours=1))         # dalej czeka — to nie cofnięcie zgody
    assert (lad.state.rung, lad.state.state) == (4, WAITING)
    lad.consent(True, T0 + timedelta(hours=2))
    assert (lad.state.rung, lad.state.state, lad.state.since) == (4, RUNNING, T0 + timedelta(hours=2))
    assert shape_errors(lad.to_payload()) == []


@pytest.mark.parametrize("start", [0, 2, 3, 5, 9])
def test_start_rung_outside_identify_or_trial_is_refused(start):
    with pytest.raises(ValueError):
        Ladder(start, P, device_key=KEY)


def test_foreign_write_in_trial_stops_the_ladder():
    lad = started(1)
    lad.identify_ok(T0)
    lad.read_ok(T0)
    lad.foreign_write(T0 + timedelta(hours=2))
    assert (lad.state.rung, lad.state.state, lad.state.stop_reason) == (3, STOPPED, "foreign_write")
    payload = lad.to_payload()
    assert payload["trial"]["foreign_writes"] == 1 and payload["stop_reason"] == "foreign_write"
    assert payload["stop_detail"] and len(payload["stop_detail"]) <= 120
    assert shape_errors(payload) == []
    lad.foreign_write(T0 + timedelta(hours=3))           # zatrzymanej nic już nie rusza
    assert lad.state.since == T0 + timedelta(hours=2)


def test_without_consent_the_ladder_waits_before_the_control_write_and_consent_moves_it_on():
    lad = started(1)
    end = to_trial_end(lad, consent=False)
    assert (lad.state.rung, lad.state.state, lad.state.next_at) == (4, WAITING, None)
    assert lad.to_payload()["trial"]["hours_done"] == 24.0          # wynik próby zostaje w bloku
    lad.tick(end + timedelta(hours=5))
    assert (lad.state.rung, lad.state.state) == (4, WAITING)
    lad.consent(True, end + timedelta(hours=6))
    assert (lad.state.rung, lad.state.state, lad.state.since) == (4, RUNNING, end + timedelta(hours=6))


def test_foreign_write_while_waiting_for_consent_does_not_stop():
    lad = started(1)
    end = to_trial_end(lad, consent=False)
    lad.foreign_write(end + timedelta(hours=1))
    assert (lad.state.rung, lad.state.state, lad.state.foreign_writes) == (4, WAITING, 0)


def test_consent_during_the_trial_does_not_shorten_it():
    lad = in_trial()
    lad.consent(True, T0 + timedelta(hours=1))
    assert (lad.state.rung, lad.state.state) == (3, RUNNING)


@pytest.mark.parametrize("readback, reason", [(False, "readback_mismatch"), (None, "read_failed")])
def test_control_write_read_back_decides(readback, reason):
    lad = started(1)
    t = to_trial_end(lad)
    lad.write_result(readback, t + timedelta(seconds=5))
    assert (lad.state.rung, lad.state.state, lad.state.stop_reason) == (4, STOPPED, reason)


@pytest.mark.parametrize("samples", [
    [(300.0, 50.0), (320.0, 51.0), (340.0, 52.0)],        # moc o ~36 % za niska
    [(500.0, 50.0), (500.0, 50.0), (500.0, 49.0)],        # moc dobra, SoC spada
    [],                                                   # brak pomiaru
])
def test_window_deviation_or_flat_soc_stops_the_ladder(samples):
    lad = started(1)
    w0 = to_window(lad)
    for i, (p, soc) in enumerate(samples):
        lad.window_sample(p, soc, w0 + timedelta(minutes=5 * i))
    lad.tick(w0 + timedelta(minutes=15))
    assert (lad.state.rung, lad.state.state, lad.state.stop_reason) == (5, STOPPED, "window_deviation")
    assert shape_errors(lad.to_payload()) == []


def test_window_with_flat_soc_passes():
    lad = started(1)
    w0 = to_window(lad)
    lad.window_sample(500.0, 50.0, w0)
    lad.window_sample(490.0, 50.0, w0 + timedelta(minutes=5))
    lad.tick(w0 + timedelta(minutes=15))
    assert lad.state.state == VERIFIED


def test_time_window_profile_is_verified_by_the_control_write():
    lad = Ladder(1, LadderParams(24, 15, 500, window=False), device_key=KEY)
    lad.tick(T0)
    t = to_trial_end(lad)
    lad.write_result(True, t)
    assert (lad.state.rung, lad.state.state) == (4, VERIFIED) and "window" not in lad.to_payload()


def test_window_deviation_at_the_limit_passes():
    lad = started(1)
    w0 = to_window(lad)
    lad.window_sample(350.0, 50.0, w0)
    lad.window_sample(350.0, 51.0, w0 + timedelta(minutes=5))
    lad.tick(w0 + timedelta(minutes=15))
    assert lad.state.state == VERIFIED and lad.to_payload()["window"]["deviation_pct"] == 30.0


def test_samples_outside_a_running_window_are_ignored():
    lad = started(1)
    t = to_trial_end(lad)
    lad.write_result(True, t)
    lad.window_sample(9999.0, 10.0, t)                   # czeka na okno — próbka nie liczy się
    lad.window_open(t)
    lad.window_sample(500.0, 50.0, t)
    lad.window_sample(500.0, 51.0, t + timedelta(minutes=5))
    lad.tick(t + timedelta(minutes=15))
    assert lad.state.state == VERIFIED


@pytest.mark.parametrize("stop_at", [3, 4, 5])
def test_retry_returns_to_the_rung_that_stopped(stop_at):
    lad = started(1)
    if stop_at == 3:
        lad.identify_ok(T0)
        lad.read_ok(T0)
        lad.consent(True, T0)
        lad.foreign_write(T0 + timedelta(hours=1))
    elif stop_at == 4:
        to_trial_end(lad)
        lad.write_result(False, T0 + timedelta(hours=24))
    else:
        w0 = to_window(lad)
        lad.tick(w0 + timedelta(minutes=15))
    assert (lad.state.rung, lad.state.state) == (stop_at, STOPPED)
    t = T0 + timedelta(days=3)
    lad.retry(t)
    assert lad.state.rung == stop_at and lad.state.stop_reason is None and lad.state.since == t
    assert lad.state.state == {3: RUNNING, 4: RUNNING, 5: WAITING}[stop_at]
    if stop_at == 3:
        assert lad.state.next_at == t + timedelta(hours=24)
        assert lad.to_payload()["trial"] == {"would_write": 0, "foreign_writes": 0, "hours_done": 0.0}


def test_retry_above_the_trial_without_consent_waits_before_the_control_write():
    lad = started(1)
    to_trial_end(lad)
    lad.write_result(False, T0 + timedelta(hours=24))
    lad.consent(False, T0 + timedelta(hours=25))
    lad.retry(T0 + timedelta(hours=26))
    assert (lad.state.rung, lad.state.state) == (4, WAITING)


def test_retry_only_acts_on_a_stopped_ladder():
    lad = started(1)
    lad.retry(T0 + timedelta(hours=1))
    assert (lad.state.rung, lad.state.state, lad.state.since) == (1, RUNNING, T0)


@pytest.mark.parametrize("rung", [4, 5])
def test_consent_revoked_on_a_writing_rung_stops(rung):
    lad = started(1)
    t = to_trial_end(lad)
    if rung == 5:
        lad.write_result(True, t)
        lad.window_open(t)
    lad.consent(False, t + timedelta(minutes=1))
    assert (lad.state.rung, lad.state.state, lad.state.stop_reason) == (rung, STOPPED, "consent_revoked")


def test_consent_revoked_at_the_trial_rung_only_waits():
    lad = in_trial()
    lad.consent(True, T0)
    lad.consent(False, T0 + timedelta(hours=1))
    assert (lad.state.rung, lad.state.state) == (3, RUNNING)


def test_read_rung_times_out_as_read_failed():
    lad = started(1)
    lad.identify_ok(T0)
    lad.tick(T0 + timedelta(minutes=9))
    assert lad.state.state == RUNNING
    lad.tick(T0 + timedelta(minutes=10))
    assert (lad.state.rung, lad.state.state, lad.state.stop_reason) == (2, STOPPED, "read_failed")


@pytest.mark.parametrize("event, reason", [("conflict", "controller_conflict"), ("abort", "user_abort")])
def test_conflict_and_abort_stop_a_running_ladder(event, reason):
    lad = in_trial()
    getattr(lad, event)(T0 + timedelta(hours=1))
    assert (lad.state.rung, lad.state.state, lad.state.stop_reason) == (3, STOPPED, reason)


def test_verified_device_is_not_stopped_by_trial_events():
    lad = started(1)
    w0 = to_window(lad)
    lad.window_sample(500.0, 50.0, w0)
    lad.window_sample(500.0, 52.0, w0 + timedelta(minutes=5))
    lad.tick(w0 + timedelta(minutes=15))
    for event in ("foreign_write", "conflict", "abort"):
        getattr(lad, event)(w0 + timedelta(hours=1))
    lad.consent(False, w0 + timedelta(hours=1))
    assert lad.state.state == VERIFIED


def test_changed_device_restarts_idle_at_the_start_rung_or_stops_mid_ladder():
    lad = in_trial()
    lad.device_changed("a" * 32, T0 + timedelta(hours=1))
    assert (lad.state.rung, lad.state.state, lad.state.stop_reason) == (1, STOPPED, "identify_changed")
    assert lad.device_key == "a" * 32
    lad.retry(T0 + timedelta(hours=2))
    assert (lad.state.rung, lad.state.state) == (1, RUNNING)

    done = started(1)
    w0 = to_window(done)
    done.window_sample(500.0, 50.0, w0)
    done.window_sample(500.0, 51.0, w0 + timedelta(minutes=1))
    done.tick(w0 + timedelta(minutes=15))
    assert done.verified
    done.device_changed("b" * 32, w0 + timedelta(hours=1))
    assert (done.state.rung, done.state.state, done.device_key) == (1, IDLE, "b" * 32)
    assert not done.verified
    done.device_changed("b" * 32, w0 + timedelta(hours=2))  # ten sam klucz — bez zmian
    assert done.state.state == IDLE


def test_migrated_ladder_is_verified_and_says_so():
    lad = Ladder(4, P, device_key=KEY)
    lad.mark_migrated(T0)
    assert lad.verified and lad.migrated and (lad.state.rung, lad.state.since) == (4, T0)
    payload = lad.to_payload()
    assert payload["migrated"] is True and payload["state"] == "verified" and shape_errors(payload) == []
    back = Ladder.from_record(lad.to_record(), P)
    assert back.migrated and back.verified and back.to_payload() == payload
    back.device_changed("c" * 32, T0 + timedelta(hours=1))
    assert not back.migrated and "migrated" not in back.to_payload()


def test_params_from_profile_override_only_valid_fields():
    base = LadderParams(trial_hours=24, window_minutes=15, window_power_w=500)
    assert base.with_overrides({"trial_hours": 2, "window_power_w": 800}) == LadderParams(2, 15, 800)
    assert base.with_overrides({"trial_hours": True, "window_minutes": "5", "x": 1}) == base
    assert base.with_overrides({"trial_hours": 0, "window_minutes": 999}) == base
    assert base.with_overrides(None) == base


# ── zapis w magazynie ─────────────────────────────────────────────────────


def test_record_round_trip_keeps_the_ladder():
    lad = started(1)
    lad.identify_ok(T0)
    lad.read_ok(T0)
    lad.would_write()
    lad.tick(T0 + timedelta(hours=3))
    rec = lad.to_record()
    json.dumps(rec)                                         # magazyn HA zapisuje JSON
    back = Ladder.from_record(rec, P)
    assert back is not None and back.state == lad.state and back.device_key == KEY
    assert back.to_payload() == lad.to_payload()


def test_record_with_a_running_window_comes_back_waiting_before_the_control_write():
    lad = started(1)
    to_window(lad)
    back = Ladder.from_record(lad.to_record(), P)
    assert (back.state.rung, back.state.state, back.state.next_at) == (4, WAITING, None)


@pytest.mark.parametrize("bad", [
    None, [], {}, {"device_key": KEY}, {"v": 1, "device_key": "123", "start": 1, "rung": 1, "state": "running",
                                        "since": T0.isoformat()},
    {"v": 1, "device_key": KEY, "start": 1, "rung": 7, "state": "running", "since": T0.isoformat()},
    {"v": 1, "device_key": KEY, "start": 3, "rung": 3, "state": "idle", "since": T0.isoformat()},
    {"v": 1, "device_key": KEY, "start": 1, "rung": 1, "state": "dancing", "since": T0.isoformat()},
    {"v": 1, "device_key": KEY, "start": 1, "rung": 1, "state": "running", "since": "yesterday"},
    {"v": 1, "device_key": KEY, "start": 1, "rung": 3, "state": "stopped", "since": T0.isoformat(),
     "stop_reason": "gremlins"},
])
def test_malformed_record_is_rejected(bad):
    assert Ladder.from_record(bad, P) is None


# ── klucz urządzenia ──────────────────────────────────────────────────────


def test_device_key_is_salted_lowercase_hex_with_a_letter():
    salt = bytes(range(16))
    k = device_key(salt, "direct|goodwe-et|fp")
    assert re.fullmatch(r"[a-f0-9]{32}", k) and re.search("[a-f]", k)
    assert k == device_key(salt, "direct|goodwe-et|fp")
    assert k != device_key(bytes(16), "direct|goodwe-et|fp")
    assert k != device_key(salt, "direct|goodwe-et|other")
    assert "fp" not in k and "goodwe" not in k


def test_device_key_never_digits_only(monkeypatch):
    from custom_components.volcast.core.control import ladder as mod

    class _Digits:
        def hexdigest(self):
            return "1" * 64

    monkeypatch.setattr(mod.hashlib, "sha256", lambda data=b"": _Digits())
    k = device_key(b"s" * 16, "x")
    assert re.fullmatch(r"[a-f0-9]{32}", k) and re.search("[a-f]", k)
