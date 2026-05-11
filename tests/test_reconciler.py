"""Tests for daily reconciler."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from tests.conftest import FakeHass, _FakeStore

from custom_components.volcast.production import VolcastProductionTracker
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


def _make_real_tracker(
    accepted: dict[str, list[int]] | None = None,
    hass=None,
) -> VolcastProductionTracker:
    """Build a real tracker with pre-populated _accepted (no Store load needed).

    Used by reconcile_day tests so the reconciler's
    `await tracker._load_accepted_store()` and `await tracker._mark_accepted()`
    calls hit the real coroutines, not MagicMock awaitables.
    """
    if hass is None:
        hass = FakeHass()
    tracker = VolcastProductionTracker(
        hass=hass,
        api_key="test-key",
        submit_url="https://example.com/api/submit-production",
        energy_entity="sensor.pv_energy",
        power_entity="sensor.pv_power",
    )
    tracker._store = _FakeStore()
    tracker._accepted_store = _FakeStore()
    if accepted is not None:
        tracker._accepted = {k: list(v) for k, v in accepted.items()}
    tracker._accepted_loaded = True  # skip Store load on next call
    return tracker


def _build_cumulative_entries(target_date: date, *, kwh_per_hour: float = 0.5,
                              start_cumulative: float = 100.0, hours: int = 25,
                              timestamp_format: str = "datetime"):
    """Build 25 hourly entries (1 baseline at hour-1 + 24 of target_date) in HA-tz.

    HA recorder returns entries in *local* tz; the reconciler treats the
    baseline-seed entry as "previous day" and ignores it for hour mapping
    but uses its sum to compute the first delta.

    timestamp_format:
      - 'datetime' (default): legacy HA — entry["start"]/"end" are datetimes.
      - 'float': modern HA (>=2023.x, confirmed on 2025.10.3) — Unix epoch
        seconds (float). This is the format that crashed beta3.
    """
    tz = ZoneInfo("Europe/Warsaw")
    base_dt = datetime.combine(target_date, datetime.min.time(), tzinfo=tz) - timedelta(hours=1)
    entries = []
    cumulative = start_cumulative
    for h in range(hours):
        cumulative += kwh_per_hour
        start_dt = base_dt + timedelta(hours=h)
        end_dt = base_dt + timedelta(hours=h + 1)
        entries.append({
            "start": start_dt.timestamp() if timestamp_format == "float" else start_dt,
            "end": end_dt.timestamp() if timestamp_format == "float" else end_dt,
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


@pytest.mark.asyncio
async def test_fetch_ha_statistics_float_timestamps_modern_ha():
    """Modern HA (>=2023.x, confirmed on 2025.10.3) returns 'start'/'end' as
    Unix epoch floats, not datetime. Beta3 crashed with AttributeError —
    this test is the regression guard."""
    target = date(2026, 5, 10)
    entries = _build_cumulative_entries(target, kwh_per_hour=0.5, timestamp_format="float")

    # Regression guard for the fixture itself: assert it really produces floats.
    assert isinstance(entries[0]["start"], float)

    with patch(
        "custom_components.volcast.reconciler.statistics_during_period",
        return_value={"sensor.pv_energy": entries},
    ):
        reconciler = _make_reconciler()
        hourly = await reconciler._fetch_ha_statistics(target)

    assert len(hourly) == 24
    for h in range(24):
        assert h in hourly, f"hour {h} missing"
        assert abs(hourly[h] - 0.5) < 1e-6


# ---------------------------------------------------------------------------
# reconcile_day — core logic with skip gates + POST + mark_accepted (Task 18)
# ---------------------------------------------------------------------------


def _today_local(reconciler: DailyReconciler) -> date:
    """Today's date in the reconciler's local tz — matches `reconcile_day`'s reference."""
    return datetime.now(reconciler._tz).date()


@pytest.mark.asyncio
async def test_reconcile_day_skips_out_of_window():
    """target_date older than RECONCILE_WINDOW_HOURS (36h) is skipped."""
    tracker = _make_real_tracker()
    reconciler = _make_reconciler(tracker=tracker)
    target = _today_local(reconciler) - timedelta(days=3)  # > 36h

    result = await reconciler.reconcile_day(target)

    assert result.skipped is True
    assert result.reason == "out_of_window"
    assert result.success is False


@pytest.mark.asyncio
async def test_reconcile_day_skips_no_stats():
    """Empty hourly stats → skip with reason='no_stats'."""
    tracker = _make_real_tracker()
    reconciler = _make_reconciler(tracker=tracker)
    target = _today_local(reconciler) - timedelta(days=1)

    with patch.object(DailyReconciler, "_fetch_ha_statistics", return_value={}):
        result = await reconciler.reconcile_day(target)

    assert result.skipped is True
    assert result.reason == "no_stats"


@pytest.mark.asyncio
async def test_reconcile_day_no_gaps_no_submit():
    """When _accepted has every hour the stats reports, no POST is made."""
    tracker = _make_real_tracker()
    reconciler = _make_reconciler(tracker=tracker)
    target = _today_local(reconciler) - timedelta(days=1)
    tracker._accepted = {target.isoformat(): list(range(6, 19))}

    full_day = {h: 1.0 for h in range(6, 19)}  # 13 daylight hours

    with patch.object(DailyReconciler, "_fetch_ha_statistics", return_value=full_day), \
         patch("custom_components.volcast.reconciler.http_with_retry") as mock_http:
        result = await reconciler.reconcile_day(target)

    assert result.success is True
    assert result.submitted == 0
    assert mock_http.call_count == 0


@pytest.mark.asyncio
async def test_reconcile_day_one_gap_filled():
    """13 stat hours, 12 already accepted → POST 1 reading with is_reconciliation=true."""
    from custom_components.volcast.http_retry import RetryResult

    tracker = _make_real_tracker()
    reconciler = _make_reconciler(tracker=tracker)
    target = _today_local(reconciler) - timedelta(days=1)
    # Missing hour 18 only
    tracker._accepted = {target.isoformat(): list(range(6, 18))}

    full_day = {h: 1.0 for h in range(6, 19)}

    with patch.object(DailyReconciler, "_fetch_ha_statistics", return_value=full_day), \
         patch("custom_components.volcast.reconciler.http_with_retry") as mock_http, \
         patch("custom_components.volcast.reconciler.async_get_clientsession",
               return_value=MagicMock()):
        mock_http.return_value = RetryResult(
            success=True, status=200, attempts=1,
            data={"accepted": 1, "rejected": 0, "rejections": []},
        )
        result = await reconciler.reconcile_day(target)

    assert result.success is True
    assert result.submitted == 1
    assert result.accepted == 1

    # Verify POST payload shape
    call_kwargs = mock_http.call_args.kwargs
    assert call_kwargs["payload"]["is_reconciliation"] is True
    assert len(call_kwargs["payload"]["readings"]) == 1
    reading = call_kwargs["payload"]["readings"][0]
    assert reading["hour"] == 18
    assert reading["date"] == target.isoformat()
    # data_method MUST be a value allowed by the backend CHECK constraint —
    # specifically 'energy_delta' or 'power_average'. beta4 used a custom
    # 'energy_delta_reconciliation' string that caused HTTP 500 on every
    # reconciliation submit. Regression guard.
    assert reading["data_method"] in {"energy_delta", "power_average"}, (
        f"data_method must satisfy backend CHECK constraint; got {reading['data_method']!r}"
    )
    assert reading["data_method"] == "energy_delta"
    # Headers match the production submit path
    assert call_kwargs["headers"]["X-API-Key"] == "test-key"
    assert call_kwargs["headers"]["Content-Type"] == "application/json"
    assert call_kwargs["method"] == "POST"

    # Hour 18 marked accepted on success
    assert 18 in tracker._accepted[target.isoformat()]


@pytest.mark.asyncio
async def test_reconcile_day_zero_kwh_excluded():
    """Hours with kWh < MIN_REPORT_KWH (nighttime) are NOT submitted."""
    from custom_components.volcast.http_retry import RetryResult

    tracker = _make_real_tracker()
    reconciler = _make_reconciler(tracker=tracker)
    target = _today_local(reconciler) - timedelta(days=1)

    stats = {h: 0.0 for h in range(0, 6)}  # nighttime zeros
    stats.update({h: 1.0 for h in range(6, 19)})  # 13 daylight hours

    with patch.object(DailyReconciler, "_fetch_ha_statistics", return_value=stats), \
         patch("custom_components.volcast.reconciler.http_with_retry") as mock_http, \
         patch("custom_components.volcast.reconciler.async_get_clientsession",
               return_value=MagicMock()):
        mock_http.return_value = RetryResult(
            success=True, status=200, attempts=1,
            data={"accepted": 13, "rejected": 0, "rejections": []},
        )
        result = await reconciler.reconcile_day(target)

    submitted_hours = [
        r["hour"] for r in mock_http.call_args.kwargs["payload"]["readings"]
    ]
    assert all(h >= 6 for h in submitted_hours), "no nighttime hours submitted"
    assert len(submitted_hours) == 13
    assert result.submitted == 13


# ---------------------------------------------------------------------------
# _setup_reconciler — trigger wiring (Task 19)
# ---------------------------------------------------------------------------


class _RecordingConfigEntry:
    """Test double for ConfigEntry that records async_on_unload registrations."""

    entry_id = "test_entry_id"
    data = {"api_key": "fake"}
    options: dict = {}

    def __init__(self):
        self.unload_callbacks: list = []

    def add_update_listener(self, _):
        return lambda: None

    def async_on_unload(self, cb):
        self.unload_callbacks.append(cb)


def _make_setup_tracker(hass) -> VolcastProductionTracker:
    """Tracker for _setup_reconciler tests — Stores stubbed but no async_start."""
    tracker = VolcastProductionTracker(
        hass=hass,
        api_key="test-key",
        submit_url="https://example.com/api/submit-production",
        energy_entity="sensor.pv_energy",
        power_entity="sensor.pv_power",
    )
    tracker._store = _FakeStore()
    tracker._accepted_store = _FakeStore()
    return tracker


@pytest.mark.asyncio
async def test_setup_reconciler_registers_scheduled_trigger():
    """_setup_reconciler calls async_track_time_change(hour=0, minute=30, second=0)."""
    from custom_components.volcast import _setup_reconciler

    hass = FakeHass(is_running=False)
    entry = _RecordingConfigEntry()
    tracker = _make_setup_tracker(hass)

    with patch("custom_components.volcast.async_track_time_change") as mock_track:
        mock_track.return_value = lambda: None  # cancel callback
        reconciler = _setup_reconciler(
            hass=hass,
            entry=entry,
            tracker=tracker,
            energy_entity="sensor.pv_energy",
            api_key="k",
            submit_url="http://x",
        )

    assert reconciler is not None
    # Exactly one call with the daily-00:30 schedule
    assert mock_track.call_count == 1
    call_kwargs = mock_track.call_args.kwargs
    assert call_kwargs["hour"] == 0
    assert call_kwargs["minute"] == 30
    assert call_kwargs["second"] == 0
    # Cancel callbacks registered for unload — schedule + bus listener
    # (is_running=False here, so on-startup wires via bus.async_listen_once
    # and that cancel is also registered for clean unload).
    assert len(entry.unload_callbacks) == 2


@pytest.mark.asyncio
async def test_setup_reconciler_listens_for_started_when_not_running():
    """When hass.is_running is False, the on-startup hook goes via bus.async_listen_once."""
    from custom_components.volcast import _setup_reconciler

    hass = FakeHass(is_running=False)
    entry = _RecordingConfigEntry()
    tracker = _make_setup_tracker(hass)

    with patch("custom_components.volcast.async_track_time_change",
               return_value=lambda: None):
        _setup_reconciler(
            hass=hass, entry=entry, tracker=tracker,
            energy_entity="sensor.pv_energy", api_key="k", submit_url="http://x",
        )

    # Bus listener registered for HOMEASSISTANT_STARTED
    listener_events = [evt for evt, _ in hass.bus.listeners]
    assert "homeassistant_started" in listener_events
    # No task created yet (waiting for the started event)
    assert hass.created_tasks == []


@pytest.mark.asyncio
async def test_setup_reconciler_runs_immediately_when_running():
    """When hass.is_running is True, the on-startup hook runs via async_create_task."""
    from custom_components.volcast import _setup_reconciler

    hass = FakeHass(is_running=True)
    entry = _RecordingConfigEntry()
    tracker = _make_setup_tracker(hass)

    with patch("custom_components.volcast.async_track_time_change",
               return_value=lambda: None):
        _setup_reconciler(
            hass=hass, entry=entry, tracker=tracker,
            energy_entity="sensor.pv_energy", api_key="k", submit_url="http://x",
        )

    # Task created (the _on_started coroutine), bus NOT used
    assert len(hass.created_tasks) == 1
    assert hass.bus.listeners == []


# ---------------------------------------------------------------------------
# _last_run_at / _last_result tracking — Task 20a
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_day_records_last_run_on_skip():
    """After a skipped reconcile_day, _last_run_at and _last_result populated."""
    tracker = _make_real_tracker()
    reconciler = _make_reconciler(tracker=tracker)
    target = _today_local(reconciler) - timedelta(days=3)  # out_of_window

    assert reconciler._last_run_at is None
    assert reconciler._last_result is None
    assert reconciler._last_target_date is None

    result = await reconciler.reconcile_day(target)

    assert reconciler._last_run_at is not None
    assert reconciler._last_run_at.tzinfo is not None  # tz-aware (UTC)
    assert reconciler._last_target_date == target.isoformat()
    assert reconciler._last_result is result
    assert reconciler._last_result.skipped is True
    assert reconciler._last_result.reason == "out_of_window"


@pytest.mark.asyncio
async def test_reconcile_day_records_last_run_on_success():
    """After a successful POST, _last_result reflects the success."""
    from custom_components.volcast.http_retry import RetryResult

    tracker = _make_real_tracker()
    reconciler = _make_reconciler(tracker=tracker)
    target = _today_local(reconciler) - timedelta(days=1)
    tracker._accepted = {target.isoformat(): list(range(6, 18))}  # missing 18

    full_day = {h: 1.0 for h in range(6, 19)}
    with patch.object(DailyReconciler, "_fetch_ha_statistics", return_value=full_day), \
         patch("custom_components.volcast.reconciler.http_with_retry") as mock_http, \
         patch("custom_components.volcast.reconciler.async_get_clientsession",
               return_value=MagicMock()):
        mock_http.return_value = RetryResult(
            success=True, status=200, attempts=1,
            data={"accepted": 1, "rejected": 0, "rejections": []},
        )
        await reconciler.reconcile_day(target)

    assert reconciler._last_result is not None
    assert reconciler._last_result.success is True
    assert reconciler._last_result.submitted == 1
    assert reconciler._last_target_date == target.isoformat()


@pytest.mark.asyncio
async def test_reconcile_day_catches_impl_exception():
    """When _reconcile_day_impl raises, _last_result is populated with failure
    (NOT None). Beta3 regression: AttributeError leaked from the impl, leaving
    _last_result=None, which made integration_healthy report HEALTHY despite
    the reconciler being broken."""
    tracker = _make_real_tracker()
    reconciler = _make_reconciler(tracker=tracker)
    target = _today_local(reconciler) - timedelta(days=1)

    boom = RuntimeError("simulated recorder crash")
    with patch.object(DailyReconciler, "_reconcile_day_impl", side_effect=boom):
        result = await reconciler.reconcile_day(target)

    # Wrapper must NOT propagate the exception.
    assert result is not None
    assert result.success is False
    assert "RuntimeError" in (result.error or "")
    assert "simulated recorder crash" in (result.error or "")
    # Diagnostic state must be populated so integration_healthy reflects truth.
    assert reconciler._last_result is not None
    assert reconciler._last_result.success is False
    assert reconciler._last_run_at is not None
    assert reconciler._last_target_date == target.isoformat()
