"""Tests for Volcast sensor attributes — regression + new detailedHourly/detailedForecast."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from custom_components.volcast.coordinator import (
    DailyForecast,
    DetailedEntry,
    HourlyEntry,
    VolcastData,
)
from custom_components.volcast.sensor import (
    VolcastBaseSensor,
    VolcastEnergyTodaySensor,
    VolcastEnergyTomorrowSensor,
    VolcastEnergyDaySensor,
    VolcastPowerNowSensor,
    VolcastApiStatusSensor,
)
from tests.conftest import FakeCoordinator, FakeHass, make_sample_data


# ---------------------------------------------------------------------------
# Helpers to instantiate sensors without full HA wiring
# ---------------------------------------------------------------------------

def _make_sensor(sensor_cls, data: VolcastData | None, **kwargs):
    """Create a sensor instance with mocked coordinator and hass."""
    coordinator = FakeCoordinator(data)
    from unittest.mock import MagicMock

    entry = MagicMock()
    entry.entry_id = "test_entry"

    sensor = sensor_cls.__new__(sensor_cls)
    sensor.coordinator = coordinator
    sensor.hass = FakeHass()
    sensor._entry_id = entry.entry_id
    sensor._attr_has_entity_name = True
    sensor._attr_unique_id = "test"
    sensor._attr_device_info = {}

    if hasattr(sensor_cls, '_days_ahead'):
        sensor._days_ahead = kwargs.get("days_ahead", 0)

    return sensor


# ---------------------------------------------------------------------------
# REGRESSION: Existing attributes must remain unchanged
# ---------------------------------------------------------------------------

DATE_TODAY = "2026-03-20"
DATE_TOMORROW = "2026-03-21"
TZ = ZoneInfo("Europe/Warsaw")


class TestExistingAttributesRegression:
    """Verify that attributes that existed before our change are still present
    and have the same format."""

    @patch("custom_components.volcast.sensor.datetime")
    def test_energy_today_has_hours_attribute(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 3, 20, 12, 0, tzinfo=TZ)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        data = make_sample_data()
        sensor = _make_sensor(VolcastEnergyTodaySensor, data)
        attrs = sensor.extra_state_attributes

        # `hours` must exist (existing attribute)
        assert "hours" in attrs
        assert isinstance(attrs["hours"], list)
        assert len(attrs["hours"]) == 24  # full day

        # Each entry must have the original keys
        for entry in attrs["hours"]:
            assert "hour" in entry
            assert "power_kw" in entry
            assert "energy_kwh" in entry

    @patch("custom_components.volcast.sensor.datetime")
    def test_energy_today_has_forecast_attribute(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 3, 20, 12, 0, tzinfo=TZ)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        data = make_sample_data()
        sensor = _make_sensor(VolcastEnergyTodaySensor, data)
        attrs = sensor.extra_state_attributes

        # `forecast` must exist (existing attribute on today sensor)
        assert "forecast" in attrs
        assert isinstance(attrs["forecast"], list)
        for entry in attrs["forecast"]:
            assert "date" in entry
            assert "energy_kwh" in entry
            assert "peak_power_kw" in entry

    @patch("custom_components.volcast.sensor.datetime")
    def test_energy_today_has_day_metadata(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 3, 20, 12, 0, tzinfo=TZ)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        data = make_sample_data()
        sensor = _make_sensor(VolcastEnergyTodaySensor, data)
        attrs = sensor.extra_state_attributes

        assert attrs["date"] == DATE_TODAY
        assert attrs["peak_power_kw"] == 4.2
        assert attrs["confidence"] == 0.85
        assert attrs["sunshine_hours"] == 8.5
        assert attrs["cloud_cover_pct"] == 25.0

    @patch("custom_components.volcast.sensor.datetime")
    def test_energy_today_native_value_unchanged(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 3, 20, 12, 0, tzinfo=TZ)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        data = make_sample_data(energy_today=25.557)
        sensor = _make_sensor(VolcastEnergyTodaySensor, data)
        assert sensor.native_value == 25.56  # rounded to 2 dp

    @patch("custom_components.volcast.sensor.datetime")
    def test_energy_tomorrow_has_hours(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 3, 20, 12, 0, tzinfo=TZ)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        data = make_sample_data()
        sensor = _make_sensor(VolcastEnergyTomorrowSensor, data)
        attrs = sensor.extra_state_attributes

        assert "hours" in attrs
        assert attrs["date"] == DATE_TOMORROW

    @patch("custom_components.volcast.sensor.datetime")
    def test_api_status_attributes_unchanged(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 3, 20, 12, 0, tzinfo=TZ)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        data = make_sample_data()
        sensor = _make_sensor(VolcastApiStatusSensor, data)
        attrs = sensor.extra_state_attributes

        assert "cache_age_minutes" in attrs
        assert "generated_at" in attrs
        assert "location" in attrs
        assert "system_capacity_kwp" in attrs
        assert "api_version" in attrs
        # API status sensor should NOT have detailedHourly (it doesn't use _day_attributes)
        assert "detailedHourly" not in attrs


# ---------------------------------------------------------------------------
# NEW: detailedHourly attribute
# ---------------------------------------------------------------------------


class TestDetailedHourlyAttribute:
    """Tests for the new detailedHourly attribute (ISO timestamps)."""

    @patch("custom_components.volcast.sensor.datetime")
    def test_detailed_hourly_present_on_today_sensor(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 3, 20, 12, 0, tzinfo=TZ)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        data = make_sample_data()
        sensor = _make_sensor(VolcastEnergyTodaySensor, data)
        attrs = sensor.extra_state_attributes

        assert "detailedHourly" in attrs
        dh = attrs["detailedHourly"]
        assert isinstance(dh, list)
        assert len(dh) == 24  # same count as hours

    @patch("custom_components.volcast.sensor.datetime")
    def test_detailed_hourly_has_iso_timestamps(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 3, 20, 12, 0, tzinfo=TZ)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        data = make_sample_data()
        sensor = _make_sensor(VolcastEnergyTodaySensor, data)
        dh = sensor.extra_state_attributes["detailedHourly"]

        for entry in dh:
            assert "period_start" in entry
            assert "power_kw" in entry
            assert "energy_kwh" in entry
            # period_start should be a valid ISO string
            assert "2026-03-20T" in entry["period_start"]
            assert "+01:00" in entry["period_start"] or "+02:00" in entry["period_start"]

    @patch("custom_components.volcast.sensor.datetime")
    def test_detailed_hourly_values_match_hours(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 3, 20, 12, 0, tzinfo=TZ)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        data = make_sample_data()
        sensor = _make_sensor(VolcastEnergyTodaySensor, data)
        attrs = sensor.extra_state_attributes

        hours = attrs["hours"]
        dh = attrs["detailedHourly"]

        # Same number of entries
        assert len(hours) == len(dh)

        # Values must match
        for h, d in zip(hours, dh):
            assert h["power_kw"] == d["power_kw"]
            assert h["energy_kwh"] == d["energy_kwh"]

    @patch("custom_components.volcast.sensor.datetime")
    def test_detailed_hourly_on_tomorrow_sensor(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 3, 20, 12, 0, tzinfo=TZ)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        data = make_sample_data()
        sensor = _make_sensor(VolcastEnergyTomorrowSensor, data)
        attrs = sensor.extra_state_attributes

        assert "detailedHourly" in attrs
        # Tomorrow's data exists
        dh = attrs["detailedHourly"]
        assert len(dh) > 0
        assert "2026-03-21T" in dh[0]["period_start"]

    @patch("custom_components.volcast.sensor.datetime")
    def test_detailed_hourly_empty_when_no_data(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 3, 20, 12, 0, tzinfo=TZ)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        from tests.conftest import make_sample_data as _make_data_fn
        # Use empty data — no hourly entries
        data = VolcastData(
            energy_today=0,
            energy_tomorrow=0,
            forecast=[],
            hourly={},
            detailed={},
            wh_hours={},
            system_capacity_kwp=None,
            location="",
            generated_at="",
            cache_age_minutes=0,
            api_version=0,
            api_status="Active",
        )
        sensor = _make_sensor(VolcastEnergyTodaySensor, data)
        attrs = sensor.extra_state_attributes

        assert "detailedHourly" in attrs
        assert attrs["detailedHourly"] == []


# ---------------------------------------------------------------------------
# NEW: detailedForecast attribute (5-min data)
# ---------------------------------------------------------------------------


class TestDetailedForecastAttribute:
    """Tests for the new detailedForecast attribute (5-min granularity)."""

    @patch("custom_components.volcast.sensor.datetime")
    def test_detailed_forecast_present_when_data_available(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 3, 20, 12, 0, tzinfo=TZ)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        data = make_sample_data(include_detailed=True)
        sensor = _make_sensor(VolcastEnergyTodaySensor, data)
        attrs = sensor.extra_state_attributes

        assert "detailedForecast" in attrs
        df = attrs["detailedForecast"]
        assert isinstance(df, list)
        assert len(df) == 12  # 12 five-minute entries in fixture

    @patch("custom_components.volcast.sensor.datetime")
    def test_detailed_forecast_has_iso_timestamps(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 3, 20, 12, 0, tzinfo=TZ)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        data = make_sample_data(include_detailed=True)
        sensor = _make_sensor(VolcastEnergyTodaySensor, data)
        df = sensor.extra_state_attributes["detailedForecast"]

        for entry in df:
            assert "period_start" in entry
            assert "power_w" in entry
            assert "energy_wh" in entry
            assert "2026-03-20T10:" in entry["period_start"]

    @patch("custom_components.volcast.sensor.datetime")
    def test_detailed_forecast_absent_when_no_5min_data(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 3, 20, 12, 0, tzinfo=TZ)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        data = make_sample_data(include_detailed=False)
        sensor = _make_sensor(VolcastEnergyTodaySensor, data)
        attrs = sensor.extra_state_attributes

        # detailedForecast should NOT be present when there's no 5-min data
        assert "detailedForecast" not in attrs

    @patch("custom_components.volcast.sensor.datetime")
    def test_detailed_forecast_values_correct(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 3, 20, 12, 0, tzinfo=TZ)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        data = make_sample_data(include_detailed=True)
        sensor = _make_sensor(VolcastEnergyTodaySensor, data)
        df = sensor.extra_state_attributes["detailedForecast"]

        # First entry
        assert df[0]["power_w"] == 3800
        assert df[0]["energy_wh"] == 317
        # Verify 5-minute increments
        assert "T10:00:" in df[0]["period_start"]
        assert "T10:05:" in df[1]["period_start"]
        assert "T10:10:" in df[2]["period_start"]


# ---------------------------------------------------------------------------
# Null/None data safety
# ---------------------------------------------------------------------------


class TestNullDataSafety:
    """Sensors must handle None data gracefully (coordinator not yet loaded)."""

    def test_energy_today_none_data(self):
        sensor = _make_sensor(VolcastEnergyTodaySensor, None)
        assert sensor.native_value is None
        assert sensor.extra_state_attributes == {}

    def test_energy_tomorrow_none_data(self):
        sensor = _make_sensor(VolcastEnergyTomorrowSensor, None)
        assert sensor.native_value is None
        assert sensor.extra_state_attributes == {}

    def test_api_status_none_data(self):
        sensor = _make_sensor(VolcastApiStatusSensor, None)
        assert sensor.native_value == "Unavailable"
        assert sensor.extra_state_attributes == {}

    @patch("custom_components.volcast.sensor.datetime")
    def test_power_now_none_data(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 3, 20, 12, 0, tzinfo=TZ)
        sensor = _make_sensor(VolcastPowerNowSensor, None)
        assert sensor.native_value is None


# ---------------------------------------------------------------------------
# PowerNow sensor
# ---------------------------------------------------------------------------


class TestPowerNowSensor:
    """Regression tests for power_now interpolation."""

    @patch("custom_components.volcast.sensor.datetime")
    def test_power_now_from_detailed(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 3, 20, 10, 7, tzinfo=TZ)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        data = make_sample_data(include_detailed=True)
        sensor = _make_sensor(VolcastPowerNowSensor, data)
        # 10:07 → slot 10:05 → power_w=3850
        assert sensor.native_value == 3850

    @patch("custom_components.volcast.sensor.datetime")
    def test_power_now_hourly_interpolation(self, mock_dt):
        # At 10:30 (halfway between hour 10 and 11)
        mock_dt.now.return_value = datetime(2026, 3, 20, 10, 30, tzinfo=TZ)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        data = make_sample_data(include_detailed=False)
        sensor = _make_sensor(VolcastPowerNowSensor, data)
        value = sensor.native_value

        # hour 10: 3.8 kW, hour 11: 4.2 kW, midpoint: 4.0 kW = 4000 W
        assert value == 4000

    @patch("custom_components.volcast.sensor.datetime")
    def test_power_now_zero_at_night(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 3, 20, 22, 0, tzinfo=TZ)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        data = make_sample_data(include_detailed=False)
        sensor = _make_sensor(VolcastPowerNowSensor, data)
        # hour 22 has 0 kW, hour 23 also 0 kW
        assert sensor.native_value == 0


# ---------------------------------------------------------------------------
# Diagnostic sensors (Task 20b): SubmitQueueDepth + LastReconciliation
# + IntegrationHealthy binary sensor
# ---------------------------------------------------------------------------


from datetime import timezone  # noqa: E402  (kept here so existing tests don't import it)
from unittest.mock import MagicMock  # noqa: E402

from custom_components.volcast.reconciler import ReconcileResult  # noqa: E402


class TestSubmitQueueDepthSensor:
    """Diagnostic sensor for the production retry queue depth + last attempt."""

    def test_state_is_queue_length(self):
        from custom_components.volcast.sensor import SubmitQueueDepthSensor

        tracker = MagicMock()
        tracker._queue = [
            {"date": "2026-05-09", "hour": 14},
            {"date": "2026-05-09", "hour": 15},
        ]
        tracker._last_submit_status = "HTTP 502"
        tracker._last_submit_attempts = 4
        tracker.last_submission_time = None

        sensor = SubmitQueueDepthSensor(tracker, entry_id="x")
        assert sensor.state == 2
        attrs = sensor.extra_state_attributes
        assert attrs["last_attempt_status"] == "HTTP 502"
        assert attrs["last_attempt_count"] == 4
        # First reading exposed for "what's blocking the queue?" UX
        assert attrs["oldest_date"] == "2026-05-09"
        assert attrs["oldest_hour"] == 14

    def test_empty_queue_state_zero_no_oldest_attrs(self):
        from custom_components.volcast.sensor import SubmitQueueDepthSensor

        tracker = MagicMock()
        tracker._queue = []
        tracker._last_submit_status = "ok"
        tracker._last_submit_attempts = 1
        tracker.last_submission_time = None

        sensor = SubmitQueueDepthSensor(tracker, entry_id="x")
        assert sensor.state == 0
        attrs = sensor.extra_state_attributes
        assert "oldest_date" not in attrs
        assert "oldest_hour" not in attrs

    def test_last_submission_time_iso_when_present(self):
        from custom_components.volcast.sensor import SubmitQueueDepthSensor

        tracker = MagicMock()
        tracker._queue = []
        tracker._last_submit_status = "ok"
        tracker._last_submit_attempts = 1
        tracker.last_submission_time = datetime(2026, 5, 10, 11, 5, tzinfo=timezone.utc)

        sensor = SubmitQueueDepthSensor(tracker, entry_id="x")
        attrs = sensor.extra_state_attributes
        assert attrs["last_attempt_at"] == "2026-05-10T11:05:00+00:00"


class TestLastReconciliationSensor:
    """Diagnostic sensor for the daily reconciler's last run."""

    def test_state_is_iso_timestamp(self):
        from custom_components.volcast.sensor import LastReconciliationSensor

        reconciler = MagicMock()
        reconciler._last_run_at = datetime(2026, 5, 10, 0, 30, tzinfo=timezone.utc)
        reconciler._last_result = ReconcileResult(success=True, submitted=2)
        reconciler._last_target_date = "2026-05-09"

        sensor = LastReconciliationSensor(reconciler, entry_id="x")
        assert sensor.state == "2026-05-10T00:30:00+00:00"
        attrs = sensor.extra_state_attributes
        assert attrs["hours_submitted_last_run"] == 2
        assert attrs["last_target_date"] == "2026-05-09"
        assert attrs["last_run_status"] == "ok"

    def test_state_none_before_first_run(self):
        from custom_components.volcast.sensor import LastReconciliationSensor

        reconciler = MagicMock()
        reconciler._last_run_at = None
        reconciler._last_result = None
        reconciler._last_target_date = None

        sensor = LastReconciliationSensor(reconciler, entry_id="x")
        assert sensor.state is None
        attrs = sensor.extra_state_attributes
        assert attrs["last_run_status"] == "never"

    def test_status_skipped_uses_reason(self):
        from custom_components.volcast.sensor import LastReconciliationSensor

        reconciler = MagicMock()
        reconciler._last_run_at = datetime(2026, 5, 10, 0, 30, tzinfo=timezone.utc)
        reconciler._last_result = ReconcileResult(skipped=True, reason="no_stats")
        reconciler._last_target_date = "2026-05-09"

        sensor = LastReconciliationSensor(reconciler, entry_id="x")
        assert sensor.extra_state_attributes["last_run_status"] == "skipped_no_stats"

    def test_status_failed_when_not_success(self):
        from custom_components.volcast.sensor import LastReconciliationSensor

        reconciler = MagicMock()
        reconciler._last_run_at = datetime(2026, 5, 10, 0, 30, tzinfo=timezone.utc)
        reconciler._last_result = ReconcileResult(
            success=False, submitted=2, error="HTTP 503",
        )
        reconciler._last_target_date = "2026-05-09"

        sensor = LastReconciliationSensor(reconciler, entry_id="x")
        assert sensor.extra_state_attributes["last_run_status"] == "failed"


class TestIntegrationHealthyBinarySensor:
    """Single-signal health binary sensor — wireable into automations."""

    def test_healthy_when_all_subsystems_ok(self):
        from custom_components.volcast.binary_sensor import IntegrationHealthyBinarySensor

        coord = MagicMock()
        coord.last_update_success = True
        tracker = MagicMock()
        tracker._queue = []
        reconciler = MagicMock()
        reconciler._last_result = ReconcileResult(success=True)

        sensor = IntegrationHealthyBinarySensor(coord, tracker, reconciler, entry_id="x")
        assert sensor.is_on is True

    def test_unhealthy_when_queue_above_24(self):
        from custom_components.volcast.binary_sensor import IntegrationHealthyBinarySensor

        coord = MagicMock()
        coord.last_update_success = True
        tracker = MagicMock()
        tracker._queue = [{} for _ in range(30)]
        reconciler = None  # power-only user

        sensor = IntegrationHealthyBinarySensor(coord, tracker, reconciler, entry_id="x")
        assert sensor.is_on is False

    def test_healthy_for_power_only_user_no_reconciler(self):
        """reconciler=None (power-only user) → reconcile_ok defaults to True."""
        from custom_components.volcast.binary_sensor import IntegrationHealthyBinarySensor

        coord = MagicMock()
        coord.last_update_success = True
        tracker = MagicMock()
        tracker._queue = []

        sensor = IntegrationHealthyBinarySensor(coord, tracker, None, entry_id="x")
        assert sensor.is_on is True

    def test_unhealthy_when_forecast_failing(self):
        from custom_components.volcast.binary_sensor import IntegrationHealthyBinarySensor

        coord = MagicMock()
        coord.last_update_success = False
        tracker = MagicMock()
        tracker._queue = []
        reconciler = MagicMock()
        reconciler._last_result = ReconcileResult(success=True)

        sensor = IntegrationHealthyBinarySensor(coord, tracker, reconciler, entry_id="x")
        assert sensor.is_on is False

    def test_healthy_when_reconciler_skipped(self):
        """A skipped reconcile (e.g. out_of_window, no_stats) is NOT a failure."""
        from custom_components.volcast.binary_sensor import IntegrationHealthyBinarySensor

        coord = MagicMock()
        coord.last_update_success = True
        tracker = MagicMock()
        tracker._queue = []
        reconciler = MagicMock()
        reconciler._last_result = ReconcileResult(skipped=True, reason="no_stats")

        sensor = IntegrationHealthyBinarySensor(coord, tracker, reconciler, entry_id="x")
        assert sensor.is_on is True

    def test_diagnostic_attributes_exposed(self):
        """Per-subsystem flags exposed for UI diagnostics."""
        from custom_components.volcast.binary_sensor import IntegrationHealthyBinarySensor

        coord = MagicMock()
        coord.last_update_success = True
        tracker = MagicMock()
        tracker._queue = []
        reconciler = MagicMock()
        reconciler._last_result = ReconcileResult(success=True)

        sensor = IntegrationHealthyBinarySensor(coord, tracker, reconciler, entry_id="x")
        attrs = sensor.extra_state_attributes
        assert attrs["forecast_ok"] is True
        assert attrs["submit_ok"] is True
        assert attrs["reconcile_ok"] is True
        assert "last_check" in attrs


# ---------------------------------------------------------------------------
# NEW: pv_estimate field on detailedForecast — HAEO Solcast-extractor compat
# ---------------------------------------------------------------------------


class TestDetailedForecastPvEstimate:
    """Tests for the pv_estimate field (kW) added to each detailedForecast
    entry so HAEO's solcast_solar extractor recognises Volcast sensors.

    See docs/plans/2026-05-30-haeo-compatibility-design.md.
    """

    @patch("custom_components.volcast.sensor.datetime")
    def test_detailed_forecast_entries_have_pv_estimate(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 3, 20, 12, 0, tzinfo=TZ)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        data = make_sample_data(include_detailed=True)
        sensor = _make_sensor(VolcastEnergyTodaySensor, data)
        attrs = sensor.extra_state_attributes

        assert "detailedForecast" in attrs
        df = attrs["detailedForecast"]
        assert isinstance(df, list)
        assert len(df) > 0

        for entry in df:
            assert "pv_estimate" in entry, f"missing pv_estimate in {entry!r}"
            assert isinstance(entry["pv_estimate"], float)

    @patch("custom_components.volcast.sensor.datetime")
    def test_pv_estimate_equals_power_w_div_1000(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 3, 20, 12, 0, tzinfo=TZ)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        data = make_sample_data(include_detailed=True)
        sensor = _make_sensor(VolcastEnergyTodaySensor, data)
        df = sensor.extra_state_attributes["detailedForecast"]

        for entry in df:
            assert entry["pv_estimate"] == pytest.approx(
                entry["power_w"] / 1000.0, rel=1e-6
            ), f"pv_estimate {entry['pv_estimate']} != power_w/1000 for {entry!r}"

    @patch("custom_components.volcast.sensor.datetime")
    def test_existing_keys_still_present_regression(self, mock_dt):
        """Adding pv_estimate must not remove existing keys (backward compat
        for user automations / template sensors)."""
        mock_dt.now.return_value = datetime(2026, 3, 20, 12, 0, tzinfo=TZ)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        data = make_sample_data(include_detailed=True)
        sensor = _make_sensor(VolcastEnergyTodaySensor, data)
        df = sensor.extra_state_attributes["detailedForecast"]

        for entry in df:
            assert "period_start" in entry
            assert "power_w" in entry
            assert "energy_wh" in entry

    @patch("custom_components.volcast.sensor.datetime")
    def test_tomorrow_sensor_omits_detailed_forecast_when_empty(self, mock_dt):
        """Tomorrow sensor has no detailed entries in the default fixture, so
        detailedForecast should be absent. pv_estimate symmetry across sensors
        is guaranteed by code-path identity (both Today and Tomorrow sensors
        call self._day_attributes() → self._detailed_forecast()), not by this test."""
        mock_dt.now.return_value = datetime(2026, 3, 20, 12, 0, tzinfo=TZ)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        data = make_sample_data(include_detailed=True)
        sensor = _make_sensor(VolcastEnergyTomorrowSensor, data)
        attrs = sensor.extra_state_attributes
        # Tomorrow has no detailed entries in fixture → detailedForecast
        # should be absent (the code skips it when list is empty).
        assert attrs.get("detailedForecast", []) == [] or "detailedForecast" not in attrs
