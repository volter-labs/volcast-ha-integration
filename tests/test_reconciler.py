"""Tests for daily reconciler."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from tests.conftest import FakeHass

from custom_components.volcast.reconciler import (
    DailyReconciler,
    ReconcileResult,
)


# ---------------------------------------------------------------------------
# ReconcileResult dataclass (Task 16)
# ---------------------------------------------------------------------------


def test_reconcile_result_defaults():
    r = ReconcileResult()
    assert r.success is False
    assert r.submitted == 0
    assert r.accepted == 0
    assert r.skipped is False
    assert r.reason is None
    assert r.error is None


# ---------------------------------------------------------------------------
# _fetch_ha_statistics — extended-window hourly delta extraction (Task 17)
# ---------------------------------------------------------------------------


def _make_reconciler(hass=None, tracker=None, energy_entity="sensor.pv_energy"):
    if hass is None:
        hass = FakeHass()
    if tracker is None:
        tracker = MagicMock()
    return DailyReconciler(
        hass=hass,
        tracker=tracker,
        energy_entity=energy_entity,
        api_key="test-key",
        submit_url="https://example.com/api/submit-production",
    )


def _build_cumulative_entries(target_date: date, *, kwh_per_hour: float = 0.5,
                              start_cumulative: float = 100.0, hours: int = 25):
    """Build 25 hourly entries (1 baseline at hour-1 + 24 of target_date) in HA-tz.

    HA recorder returns entries in *local* tz; the reconciler treats the
    baseline-seed entry as "previous day" and ignores it for hour mapping
    but uses its sum to compute the first delta.
    """
    tz = ZoneInfo("Europe/Warsaw")
    base_dt = datetime.combine(target_date, datetime.min.time(), tzinfo=tz) - timedelta(hours=1)
    entries = []
    cumulative = start_cumulative
    for h in range(hours):
        cumulative += kwh_per_hour
        entries.append({
            "start": base_dt + timedelta(hours=h),
            "end": base_dt + timedelta(hours=h + 1),
            "sum": cumulative,
        })
    return entries


@pytest.mark.asyncio
async def test_fetch_ha_statistics_returns_hourly_deltas():
    """25 entries (1h baseline + 24 of target_date) → 24 deltas of 0.5 kWh each."""
    target = date(2026, 5, 10)
    entries = _build_cumulative_entries(target, kwh_per_hour=0.5)

    with patch(
        "custom_components.volcast.reconciler.statistics_during_period",
        return_value={"sensor.pv_energy": entries},
    ):
        reconciler = _make_reconciler()
        hourly = await reconciler._fetch_ha_statistics(target)

    assert len(hourly) == 24
    for h in range(24):
        assert h in hourly, f"hour {h} missing"
        assert abs(hourly[h] - 0.5) < 1e-6, f"hour {h} delta={hourly[h]}"


@pytest.mark.asyncio
async def test_fetch_ha_statistics_handles_counter_reset():
    """A negative delta (energy counter reset to 0) is clamped to 0.

    Models a realistic reset: at hour 12 the counter drops to ~0.5 (one
    hour of fresh production from the new zero), then continues from there.
    """
    target = date(2026, 5, 10)
    entries = _build_cumulative_entries(target, kwh_per_hour=0.5)
    # Reset at target-day hour 12 (index 13 = baseline + 12 + 1):
    # counter snaps back to a small post-reset value, subsequent entries
    # accumulate from the new baseline.
    for h in range(13, 25):
        entries[h]["sum"] = 0.5 * (h - 12)  # hour 12: 0.5, hour 13: 1.0, ...

    with patch(
        "custom_components.volcast.reconciler.statistics_during_period",
        return_value={"sensor.pv_energy": entries},
    ):
        reconciler = _make_reconciler()
        hourly = await reconciler._fetch_ha_statistics(target)

    # Reset hour clamped to 0 (negative raw delta);
    # subsequent hours resume normal 0.5 kWh/h deltas from the new baseline.
    assert hourly[12] == 0.0
    assert abs(hourly[13] - 0.5) < 1e-6
    assert abs(hourly[14] - 0.5) < 1e-6


@pytest.mark.asyncio
async def test_fetch_ha_statistics_empty_when_no_data():
    """Empty stats response → empty dict (recorder unavailable / no data)."""
    target = date(2026, 5, 10)

    with patch(
        "custom_components.volcast.reconciler.statistics_during_period",
        return_value={},
    ):
        reconciler = _make_reconciler()
        hourly = await reconciler._fetch_ha_statistics(target)

    assert hourly == {}
