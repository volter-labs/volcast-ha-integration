"""Tests for tracker state persistence across reloads/restarts.

`submissions_today`, `last_submission_time`, and `calibration` used to live
only in process memory — every HA restart or integration reload wiped them
to 0/None, making the `api_status` sensor's `submissions_today` and
`last_submission` attributes useless as health indicators. This module
pins down the persistence behaviour: state survives reload, day rollover
resets the daily counter without losing other fields, malformed stored
state is handled defensively.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import FakeHass, _FakeStore

from custom_components.volcast.production import (
    STATE_STORAGE_KEY,
    STATE_STORAGE_VERSION,
    VolcastProductionTracker,
)


def _make_tracker(queue_store=None, state_store=None):
    tracker = VolcastProductionTracker(
        hass=FakeHass(),
        api_key="test",
        submit_url="https://example.com/api/submit-production",
        energy_entity="sensor.pv_energy",
        power_entity="sensor.pv_power",
        system_capacity_kwp=6.0,
    )
    if queue_store is not None:
        tracker._store = queue_store
    if state_store is not None:
        tracker._state_store = state_store
    return tracker


def _mock_session_ok(accepted: int = 1, calibration: dict | None = None):
    resp = AsyncMock()
    resp.ok = True
    resp.status = 200
    resp.json = AsyncMock(return_value={"accepted": accepted, "rejected": 0, "calibration": calibration})
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.post = MagicMock(return_value=ctx)
    return session


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestStateStoreConstants:
    def test_storage_key_distinct_from_queue(self):
        from custom_components.volcast.production import STORAGE_KEY
        assert STATE_STORAGE_KEY != STORAGE_KEY
        assert STATE_STORAGE_KEY == "volcast_tracker_state"

    def test_version_is_int(self):
        assert isinstance(STATE_STORAGE_VERSION, int) and STATE_STORAGE_VERSION >= 1


# ---------------------------------------------------------------------------
# Save path: successful submit persists state
# ---------------------------------------------------------------------------


class TestSavePath:

    @pytest.mark.asyncio
    async def test_successful_submit_persists_counters(self):
        state_store = _FakeStore()
        tracker = _make_tracker(queue_store=_FakeStore(), state_store=state_store)

        with patch(
            "custom_components.volcast.production.async_get_clientsession",
            return_value=_mock_session_ok(accepted=2, calibration={"bias": 0.85}),
        ):
            ok = await tracker._async_submit(
                [{"date": "2026-05-16", "hour": 7, "actual_kwh": 1.5, "data_method": "energy_delta"}]
            )

        assert ok is True
        saved = await state_store.async_load()
        assert isinstance(saved, dict)
        assert saved["submissions_today"] == 2
        assert saved["calibration"] == {"bias": 0.85}
        assert saved["last_submission_time"] is not None

    @pytest.mark.asyncio
    async def test_failed_submit_does_not_overwrite_state(self):
        """A 5xx / timeout failure must not zero out previously-saved counters."""
        state_store = _FakeStore()
        # Pre-populate with prior session's counters
        await state_store.async_save({
            "submissions_today": 7,
            "last_submission_date": "2026-05-16",
            "last_submission_time": "2026-05-16T05:08:00+00:00",
            "calibration": {"bias": 0.9},
        })

        tracker = _make_tracker(queue_store=_FakeStore(), state_store=state_store)

        # Failed submit
        resp = AsyncMock()
        resp.ok = False
        resp.status = 500
        resp.text = AsyncMock(return_value="err")
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=resp)
        ctx.__aexit__ = AsyncMock(return_value=False)
        session = MagicMock()
        session.post = MagicMock(return_value=ctx)

        with patch("custom_components.volcast.production.async_get_clientsession", return_value=session):
            await tracker._async_submit(
                [{"date": "2026-05-16", "hour": 7, "actual_kwh": 1.5, "data_method": "energy_delta"}]
            )

        saved = await state_store.async_load()
        assert saved["submissions_today"] == 7  # unchanged


# ---------------------------------------------------------------------------
# Load path: restoration on tracker construction + async_start
# ---------------------------------------------------------------------------


class TestLoadPath:

    @pytest.mark.asyncio
    async def test_load_restores_counters_when_dates_match(self):
        state_store = _FakeStore()
        # Derive "today" the same way the tracker does — via _get_local_now()
        # which uses HA's configured timezone (Europe/Warsaw in FakeHass). Using
        # `datetime.now()` directly would drift if the runner's wall-clock
        # timezone differs OR if the test hits the day boundary in HA's tz.
        tracker = _make_tracker(queue_store=_FakeStore(), state_store=state_store)
        today = tracker._get_local_now().strftime("%Y-%m-%d")
        await state_store.async_save({
            "submissions_today": 5,
            "last_submission_date": today,
            "last_submission_time": "2026-05-16T05:08:00+00:00",
            "calibration": {"bias": 0.87},
        })

        await tracker._async_load_state()

        assert tracker.submissions_today == 5
        assert tracker.calibration == {"bias": 0.87}
        assert tracker.last_submission_time == datetime.fromisoformat("2026-05-16T05:08:00+00:00")

    @pytest.mark.asyncio
    async def test_load_resets_counter_on_day_rollover(self):
        """Stored date is yesterday → submissions_today resets to 0,
        but last_submission_time and calibration persist."""
        state_store = _FakeStore()
        await state_store.async_save({
            "submissions_today": 9,
            "last_submission_date": "2020-01-01",  # ancient — definitely not today
            "last_submission_time": "2020-01-01T23:08:00+00:00",
            "calibration": {"bias": 0.5},
        })

        tracker = _make_tracker(queue_store=_FakeStore(), state_store=state_store)
        await tracker._async_load_state()

        assert tracker.submissions_today == 0  # reset
        # Other fields persist — they remain useful across days
        assert tracker.last_submission_time == datetime.fromisoformat("2020-01-01T23:08:00+00:00")
        assert tracker.calibration == {"bias": 0.5}

    @pytest.mark.asyncio
    async def test_load_handles_empty_store(self):
        """Fresh install: no stored state. Counters stay at defaults, no crash."""
        tracker = _make_tracker(queue_store=_FakeStore(), state_store=_FakeStore())
        await tracker._async_load_state()
        assert tracker.submissions_today == 0
        assert tracker.last_submission_time is None
        assert tracker.calibration is None

    @pytest.mark.asyncio
    async def test_load_handles_malformed_state(self):
        """Store returning non-dict (corruption, schema mismatch) must not crash tracker."""
        state_store = _FakeStore()
        await state_store.async_save(["not", "a", "dict"])  # type: ignore
        tracker = _make_tracker(queue_store=_FakeStore(), state_store=state_store)
        await tracker._async_load_state()
        assert tracker.submissions_today == 0
        assert tracker.last_submission_time is None

    @pytest.mark.asyncio
    async def test_load_handles_invalid_iso_timestamp(self):
        state_store = _FakeStore()
        tracker = _make_tracker(queue_store=_FakeStore(), state_store=state_store)
        today = tracker._get_local_now().strftime("%Y-%m-%d")
        await state_store.async_save({
            "submissions_today": 3,
            "last_submission_date": today,
            "last_submission_time": "not-an-iso",
            "calibration": None,
        })

        await tracker._async_load_state()

        # Counter restored; bogus timestamp ignored
        assert tracker.submissions_today == 3
        assert tracker.last_submission_time is None

    @pytest.mark.asyncio
    async def test_load_handles_malformed_submissions_today(self):
        """Storage corruption / older schema may yield non-int submissions_today.
        Should fall back to 0 with a warning, not crash startup."""
        state_store = _FakeStore()
        tracker = _make_tracker(queue_store=_FakeStore(), state_store=state_store)
        today = tracker._get_local_now().strftime("%Y-%m-%d")
        await state_store.async_save({
            "submissions_today": "abc",  # corrupt
            "last_submission_date": today,
            "last_submission_time": None,
            "calibration": None,
        })

        await tracker._async_load_state()

        assert tracker.submissions_today == 0

    @pytest.mark.asyncio
    async def test_load_handles_async_load_raising(self):
        """Storage I/O error during async_load must not crash the tracker."""
        from unittest.mock import AsyncMock

        state_store = _FakeStore()
        state_store.async_load = AsyncMock(side_effect=OSError("disk gone"))
        tracker = _make_tracker(queue_store=_FakeStore(), state_store=state_store)

        await tracker._async_load_state()

        # Tracker survives, counters at defaults
        assert tracker.submissions_today == 0
        assert tracker.last_submission_time is None
        assert tracker.calibration is None
        # And future load is a no-op (won't repeatedly raise)
        await tracker._async_load_state()

    @pytest.mark.asyncio
    async def test_load_is_idempotent(self):
        """Calling _async_load_state twice does not double-restore."""
        state_store = _FakeStore()
        tracker = _make_tracker(queue_store=_FakeStore(), state_store=state_store)
        today = tracker._get_local_now().strftime("%Y-%m-%d")
        await state_store.async_save({
            "submissions_today": 4,
            "last_submission_date": today,
            "last_submission_time": None,
            "calibration": None,
        })

        await tracker._async_load_state()
        assert tracker.submissions_today == 4

        # Mutate counter, call load again — should NOT overwrite (load is idempotent)
        tracker.submissions_today = 99
        await tracker._async_load_state()
        assert tracker.submissions_today == 99


# ---------------------------------------------------------------------------
# Round-trip: reload simulates a real reload cycle
# ---------------------------------------------------------------------------


class TestRoundTripAcrossReload:

    @pytest.mark.asyncio
    async def test_state_survives_simulated_reload(self):
        """Tracker A submits successfully → tracker B (post-reload) sees the counters."""
        shared_state_store = _FakeStore()
        shared_queue_store = _FakeStore()

        # Tracker A: submit succeeds, state gets persisted
        tracker_a = _make_tracker(queue_store=shared_queue_store, state_store=shared_state_store)
        with patch(
            "custom_components.volcast.production.async_get_clientsession",
            return_value=_mock_session_ok(accepted=1, calibration={"bias": 0.92}),
        ):
            await tracker_a._async_submit(
                [{"date": "2026-05-16", "hour": 9, "actual_kwh": 2.3, "data_method": "energy_delta"}]
            )

        # Tracker B: fresh instance with same stores (= reload)
        tracker_b = _make_tracker(queue_store=shared_queue_store, state_store=shared_state_store)
        # Pre-reload assertions: B starts with default counters
        assert tracker_b.submissions_today == 0
        assert tracker_b.calibration is None

        # Simulate async_start's first call
        await tracker_b._async_load_state()
        assert tracker_b.submissions_today == 1
        assert tracker_b.calibration == {"bias": 0.92}
        assert tracker_b.last_submission_time is not None
