"""Tests for the sparse-hour delta=0 fix.

When an hour fires only ONE energy state_changed event (eg. dawn first ramp,
overnight inverter daily-reset edge), the previous behaviour set both
energy_start and energy_latest to the same value, producing delta=0 — a false
zero that hid real production data. This module pins down the new behaviour:
single-event hours without carry-over fall through to method 2 (trapezoidal
power integration) when power readings are available.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tests.conftest import FakeHass, _FakeStore

from custom_components.volcast.production import HourBucket, VolcastProductionTracker


def _make_tracker():
    tracker = VolcastProductionTracker(
        hass=FakeHass(),
        api_key="test",
        submit_url="https://example.com/api/submit-production",
        energy_entity="sensor.pv_energy",
        power_entity="sensor.pv_power",
        system_capacity_kwp=6.0,
    )
    tracker._store = _FakeStore()
    return tracker


# ---------------------------------------------------------------------------
# Regression: the dawn-edge case from production data analysis
# ---------------------------------------------------------------------------


class TestSingleEventDawnHour:
    """Sun rises mid-hour: 1 energy reading + several power readings."""

    def test_single_event_no_carry_falls_through_to_power(self):
        """Hour 7 example: energy fires once at 0.1, power fires 5 times.

        Pre-fix: method 1 returned (0, energy_delta) → real ~0.5 kWh production
        reported as 0.
        Post-fix: method 1 detects single-event + no carry-over, falls through
        to method 2 → real positive kWh.
        """
        tracker = _make_tracker()
        bucket = HourBucket(hour=7)
        # Single energy event — both start and latest get same value
        bucket.energy_start = 0.1
        bucket.energy_latest = 0.1
        bucket.energy_event_count = 1
        bucket.energy_start_carried = False
        # Power ramped from 105 → 241 W over the hour
        bucket.power_readings = [
            (0.0, 105.0),
            (900.0, 150.0),
            (1800.0, 200.0),
            (2700.0, 220.0),
            (3600.0, 241.0),
        ]

        kwh, method = tracker._compute_energy(bucket)

        assert method == "power_average"
        assert kwh is not None
        assert kwh > 0, "real production must not report as 0"

    def test_single_event_with_carry_uses_delta(self):
        """When energy_start was carried over from previous hour's last reading,
        a single in-hour event IS a real delta — use method 1."""
        tracker = _make_tracker()
        bucket = HourBucket(hour=7)
        # Carried over from previous hour's 0.0 (midnight reset)
        bucket.energy_start = 0.0
        bucket.energy_start_carried = True
        # Single event this hour took us to 0.1
        bucket.energy_latest = 0.1
        bucket.energy_event_count = 1

        kwh, method = tracker._compute_energy(bucket)

        assert method == "energy_delta"
        assert kwh == pytest.approx(0.1)


class TestExistingPathsUnchanged:
    """Regression — every previously-working path stays working."""

    def test_two_events_use_delta(self):
        tracker = _make_tracker()
        bucket = HourBucket(hour=10)
        bucket.energy_start = 1.0
        bucket.energy_latest = 2.5
        bucket.energy_event_count = 2

        kwh, method = tracker._compute_energy(bucket)
        assert method == "energy_delta"
        assert kwh == pytest.approx(1.5)

    def test_many_events_use_delta(self):
        tracker = _make_tracker()
        bucket = HourBucket(hour=12)
        bucket.energy_start = 4.2
        bucket.energy_latest = 6.1
        bucket.energy_event_count = 20

        kwh, method = tracker._compute_energy(bucket)
        assert method == "energy_delta"
        assert kwh == pytest.approx(1.9)

    def test_negative_delta_falls_through_to_power(self):
        """Counter reset should still fall through to method 2 (existing behaviour)."""
        tracker = _make_tracker()
        bucket = HourBucket(hour=0)
        bucket.energy_start = 12.8  # carried from end of yesterday
        bucket.energy_start_carried = True
        bucket.energy_latest = 0.0  # reset at midnight
        bucket.energy_event_count = 1
        bucket.power_readings = [(0.0, 0.0), (3600.0, 0.0)]

        kwh, method = tracker._compute_energy(bucket)
        assert method == "power_average"
        assert kwh == pytest.approx(0.0)

    def test_capacity_glitch_skipped(self):
        tracker = _make_tracker()
        bucket = HourBucket(hour=14)
        bucket.energy_start = 0.0
        bucket.energy_latest = 1000.0  # nonsense
        bucket.energy_event_count = 2

        kwh, method = tracker._compute_energy(bucket)
        assert kwh is None
        assert method == "energy_delta"

    def test_no_energy_only_power_uses_method2(self):
        tracker = _make_tracker()
        bucket = HourBucket(hour=11)
        bucket.power_readings = [(0.0, 1000.0), (3600.0, 2000.0)]

        kwh, method = tracker._compute_energy(bucket)
        assert method == "power_average"
        assert kwh == pytest.approx(1.5)

    def test_no_data_returns_none(self):
        tracker = _make_tracker()
        bucket = HourBucket(hour=3)
        kwh, method = tracker._compute_energy(bucket)
        assert kwh is None
        assert method == ""


# ---------------------------------------------------------------------------
# State-change wiring: verify the bucket's metadata gets populated correctly
# ---------------------------------------------------------------------------


class TestBucketMetadataPopulation:
    """`_async_state_changed` must populate energy_event_count and energy_start_carried."""

    def test_energy_event_increments_count(self):
        tracker = _make_tracker()

        # Pre-seed: bucket exists, no events yet
        from custom_components.volcast.production import HourBucket as HB
        tracker._current_bucket = HB(hour=10)
        tracker._get_local_now = MagicMock(return_value=_dt_at_hour(10))

        evt = _energy_event("sensor.pv_energy", "1.0")
        tracker._async_state_changed(evt)
        assert tracker._current_bucket.energy_event_count == 1
        assert tracker._current_bucket.energy_start == 1.0
        assert tracker._current_bucket.energy_latest == 1.0
        assert tracker._current_bucket.energy_start_carried is False

        evt2 = _energy_event("sensor.pv_energy", "1.5")
        tracker._async_state_changed(evt2)
        assert tracker._current_bucket.energy_event_count == 2
        assert tracker._current_bucket.energy_latest == 1.5

    def test_hour_rollover_with_carry_sets_flag(self):
        tracker = _make_tracker()
        from custom_components.volcast.production import HourBucket as HB

        # Hour 6 bucket finishes with energy_latest = 12.8
        tracker._current_bucket = HB(hour=6, energy_start=12.8, energy_latest=12.8, energy_event_count=2)
        tracker._get_local_now = MagicMock(return_value=_dt_at_hour(7))

        # Power event during hour 7 triggers rollover
        evt = _energy_event("sensor.pv_power", "100.0")
        tracker._async_state_changed(evt)

        assert tracker._previous_bucket is not None
        assert tracker._previous_bucket.hour == 6
        assert tracker._current_bucket.hour == 7
        assert tracker._current_bucket.energy_start == 12.8
        assert tracker._current_bucket.energy_start_carried is True
        # No energy event yet
        assert tracker._current_bucket.energy_event_count == 0

    def test_hour_rollover_no_carry_leaves_flag_false(self):
        tracker = _make_tracker()
        from custom_components.volcast.production import HourBucket as HB

        # Previous hour had no energy events at all
        tracker._current_bucket = HB(hour=2)  # energy_latest=None
        tracker._get_local_now = MagicMock(return_value=_dt_at_hour(3))

        evt = _energy_event("sensor.pv_power", "0.0")
        tracker._async_state_changed(evt)

        assert tracker._current_bucket.hour == 3
        assert tracker._current_bucket.energy_start is None
        assert tracker._current_bucket.energy_start_carried is False


# ---------------------------------------------------------------------------
# Flush rollover carry-over (CodeRabbit PR2 outside-diff finding)
# ---------------------------------------------------------------------------


class TestFlushRolloverCarryOver:
    """When _async_check_flush rolls _current_bucket into a new hour bucket,
    it must mirror the state-changed rollover's carry-over so the FIRST event
    of the new hour is treated as carried (not single-event-zero)."""

    @pytest.mark.asyncio
    async def test_flush_rollover_carries_energy_latest(self):
        tracker = _make_tracker()
        from custom_components.volcast.production import HourBucket as HB

        # _current_bucket matches prev_hour (=7) with energy data
        tracker._current_bucket = HB(
            hour=7, energy_start=0.0, energy_latest=1.5, energy_event_count=3,
        )
        # Force tracker to think it's hour 8 and minute >= 5
        tracker._get_local_now = MagicMock(return_value=_dt_at_hour(8, minute=10))
        # Stub out submit
        from unittest.mock import AsyncMock
        tracker._async_submit = AsyncMock(return_value=True)

        await tracker._async_check_flush(_dt_at_hour(8, minute=10))

        # The new current bucket (hour 8) must have carried-over energy_start
        assert tracker._current_bucket is not None
        assert tracker._current_bucket.hour == 8
        assert tracker._current_bucket.energy_start == 1.5
        assert tracker._current_bucket.energy_start_carried is True
        # Event count must remain 0 — carry-over is not a real event
        assert tracker._current_bucket.energy_event_count == 0

    @pytest.mark.asyncio
    async def test_flush_rollover_no_carry_when_no_energy_latest(self):
        """If the flushed bucket had no energy events, nothing to carry."""
        tracker = _make_tracker()
        from custom_components.volcast.production import HourBucket as HB

        tracker._current_bucket = HB(hour=7)  # all defaults — no energy events
        tracker._get_local_now = MagicMock(return_value=_dt_at_hour(8, minute=10))

        from unittest.mock import AsyncMock
        tracker._async_submit = AsyncMock(return_value=True)

        await tracker._async_check_flush(_dt_at_hour(8, minute=10))

        assert tracker._current_bucket is not None
        assert tracker._current_bucket.hour == 8
        assert tracker._current_bucket.energy_start is None
        assert tracker._current_bucket.energy_start_carried is False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dt_at_hour(hour: int, minute: int = 30):
    from datetime import datetime, timezone
    return datetime(2026, 5, 16, hour, minute, tzinfo=timezone.utc)


def _energy_event(entity_id: str, state: str):
    new_state = MagicMock()
    new_state.state = state
    evt = MagicMock()
    evt.data = {"new_state": new_state, "entity_id": entity_id}
    return evt
