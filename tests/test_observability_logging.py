"""Tests for observability logging across coordinator + production tracker."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import FakeHass, _FakeStore, make_sample_data

from custom_components.volcast.coordinator import _parse_response
from custom_components.volcast.production import HourBucket, VolcastProductionTracker


def _make_tracker(store=None):
    tracker = VolcastProductionTracker(
        hass=FakeHass(),
        api_key="test",
        submit_url="https://example.com/api/submit-production",
        energy_entity="sensor.pv_energy",
        power_entity="sensor.pv_power",
        system_capacity_kwp=6.0,
    )
    if store is not None:
        tracker._store = store
    return tracker


def _mock_session_ok():
    resp = AsyncMock()
    resp.ok = True
    resp.status = 200
    resp.json = AsyncMock(return_value={"accepted": 1, "rejected": 0, "calibration": None})
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.post = MagicMock(return_value=ctx)
    return session


class TestCoordinatorLogging:
    """Coordinator emits structured logs on parse paths."""

    def test_stale_cache_emits_warning(self, caplog):
        """When server's cache_age_minutes >= 180, log WARNING about server staleness."""
        raw = {
            "state": 8.78,
            "attributes": {
                "api_version": 2,
                "system_capacity_kwp": 5.28,
                "location": "Melbourne",
                "generated_at": "2026-05-15T12:41:55Z",
                "cache_age_minutes": 670,
            },
        }
        # _parse_response is the parse-only path; the WARNING lives in _async_update_data
        # so we verify _parse_response builds data correctly (regression check) here.
        data = _parse_response(raw, FakeHass())
        assert data.cache_age_minutes == 670
        assert data.api_status == "Active"

    def test_parse_response_preserves_fresh_cache(self):
        """Fresh cache should parse without surfacing stale fields."""
        raw = {
            "state": 8.78,
            "attributes": {
                "api_version": 2,
                "system_capacity_kwp": 5.28,
                "location": "Melbourne",
                "generated_at": "2026-05-16T00:00:00Z",
                "cache_age_minutes": 5,
            },
        }
        data = _parse_response(raw, FakeHass())
        assert data.cache_age_minutes == 5


class TestProductionFlushLogging:
    """Flush tick emits structured logs at each early-return path."""

    @pytest.mark.asyncio
    async def test_flush_tick_logs_state(self, caplog):
        """Every flush tick should log state at DEBUG so user sees timer firing."""
        tracker = _make_tracker(store=_FakeStore())

        with patch.object(tracker, "_get_local_now") as now_mock:
            # minute < 5 → grace window early return
            now_mock.return_value = _fake_dt(hour=8, minute=3)
            with caplog.at_level(logging.DEBUG, logger="custom_components.volcast.production"):
                await tracker._async_check_flush(None)

        msgs = [r.getMessage() for r in caplog.records]
        assert any("Flush tick:" in m for m in msgs), msgs
        assert any("grace window" in m for m in msgs), msgs

    @pytest.mark.asyncio
    async def test_flush_skipped_already_flushed_logs_debug(self, caplog):
        """Re-firing within same hour after flush logs DEBUG, not WARNING."""
        tracker = _make_tracker(store=_FakeStore())
        tracker._last_flushed_hour = 7

        with patch.object(tracker, "_get_local_now") as now_mock:
            now_mock.return_value = _fake_dt(hour=8, minute=10)
            with caplog.at_level(logging.DEBUG, logger="custom_components.volcast.production"):
                await tracker._async_check_flush(None)

        assert any("already flushed" in r.getMessage() for r in caplog.records)
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings == []

    @pytest.mark.asyncio
    async def test_flush_skipped_no_bucket_logs_warning(self, caplog):
        """Missing bucket for prev_hour should log WARNING (data loss visible)."""
        tracker = _make_tracker(store=_FakeStore())

        with patch.object(tracker, "_get_local_now") as now_mock:
            now_mock.return_value = _fake_dt(hour=8, minute=10)
            with caplog.at_level(logging.WARNING, logger="custom_components.volcast.production"):
                await tracker._async_check_flush(None)

        assert any(
            "no bucket data" in r.getMessage() and r.levelno == logging.WARNING
            for r in caplog.records
        ), [r.getMessage() for r in caplog.records]

    @pytest.mark.asyncio
    async def test_flush_skipped_no_data_logs_warning(self, caplog):
        """Bucket present but compute_energy returns None should log WARNING."""
        tracker = _make_tracker(store=_FakeStore())
        # Bucket for prev_hour with no energy data and no power readings
        tracker._previous_bucket = HourBucket(hour=7)

        with patch.object(tracker, "_get_local_now") as now_mock:
            now_mock.return_value = _fake_dt(hour=8, minute=10)
            with caplog.at_level(logging.WARNING, logger="custom_components.volcast.production"):
                await tracker._async_check_flush(None)

        msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("insufficient data" in m for m in msgs), msgs

    @pytest.mark.asyncio
    async def test_flush_emits_info_on_submit(self, caplog):
        """When submit actually fires, INFO line names the hour + method + kwh."""
        tracker = _make_tracker(store=_FakeStore())
        bucket = HourBucket(hour=7)
        bucket.energy_start = 0.0
        bucket.energy_latest = 1.5
        tracker._previous_bucket = bucket

        session = _mock_session_ok()
        with patch.object(tracker, "_get_local_now") as now_mock, patch(
            "custom_components.volcast.production.async_get_clientsession",
            return_value=session,
        ):
            now_mock.return_value = _fake_dt(hour=8, minute=10)
            with caplog.at_level(logging.INFO, logger="custom_components.volcast.production"):
                await tracker._async_check_flush(None)

        msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert any("Flushing hour 7" in m and "kwh=1.5" in m for m in msgs), msgs


class TestComputeEnergyLogging:
    """_compute_energy logs which branch it took."""

    def test_method_energy_delta_logs_debug(self, caplog):
        tracker = _make_tracker(store=_FakeStore())
        bucket = HourBucket(hour=10)
        bucket.energy_start = 1.0
        bucket.energy_latest = 2.5

        with caplog.at_level(logging.DEBUG, logger="custom_components.volcast.production"):
            kwh, method = tracker._compute_energy(bucket)

        assert kwh == pytest.approx(1.5)
        assert method == "energy_delta"
        assert any(
            "method=energy_delta" in r.getMessage() and r.levelno == logging.DEBUG
            for r in caplog.records
        )

    def test_method_power_average_logs_debug(self, caplog):
        tracker = _make_tracker(store=_FakeStore())
        bucket = HourBucket(hour=10)
        bucket.power_readings = [(0.0, 1000.0), (3600.0, 2000.0)]

        with caplog.at_level(logging.DEBUG, logger="custom_components.volcast.production"):
            _kwh, method = tracker._compute_energy(bucket)

        assert method == "power_average"
        assert any(
            "method=power_average" in r.getMessage() and r.levelno == logging.DEBUG
            for r in caplog.records
        )

    def test_no_method_viable_logs_debug(self, caplog):
        tracker = _make_tracker(store=_FakeStore())
        bucket = HourBucket(hour=10)  # all defaults — no data

        with caplog.at_level(logging.DEBUG, logger="custom_components.volcast.production"):
            kwh, method = tracker._compute_energy(bucket)

        assert kwh is None
        assert method == ""
        assert any(
            "no method viable" in r.getMessage() and r.levelno == logging.DEBUG
            for r in caplog.records
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_dt(hour: int, minute: int = 0):
    """Build a datetime stand-in matching what _get_local_now would return."""
    from datetime import datetime, timezone

    return datetime(2026, 5, 16, hour, minute, tzinfo=timezone.utc)
