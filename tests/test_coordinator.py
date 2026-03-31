"""Tests for Volcast coordinator — API response parsing and wh_hours."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from custom_components.volcast.coordinator import (
    DailyForecast,
    DetailedEntry,
    HourlyEntry,
    VolcastData,
    _build_wh_hours,
    _error_data,
    _parse_response,
)
from tests.conftest import FakeHass


# ---------------------------------------------------------------------------
# _parse_response
# ---------------------------------------------------------------------------


def _make_api_response(
    *,
    state: float = 25.5,
    forecast: list[dict] | None = None,
    hourly: dict | None = None,
    detailed: dict | None = None,
    api_version: int = 1,
    extra_attrs: dict | None = None,
) -> dict[str, Any]:
    """Build a fake API JSON response."""
    attrs: dict[str, Any] = {
        "api_version": api_version,
        "system_capacity_kwp": 6.5,
        "location": "Warsaw, PL",
        "generated_at": "2026-03-20T06:00:00Z",
        "cache_age_minutes": 5,
    }
    if forecast is not None:
        attrs["forecast"] = forecast
    if hourly is not None:
        attrs["hourly"] = hourly
    if detailed is not None:
        attrs["detailed"] = detailed
    if extra_attrs:
        attrs.update(extra_attrs)
    return {"state": state, "attributes": attrs}


class TestParseResponse:
    """Tests for _parse_response."""

    def test_basic_daily_forecast(self):
        raw = _make_api_response(
            forecast=[
                {
                    "date": "2026-03-20",
                    "energy_kwh": 25.5,
                    "peak_power_kw": 4.2,
                    "confidence": 0.85,
                    "sunshine_hours": 8.5,
                    "cloud_cover_pct": 25,
                },
            ],
        )
        hass = FakeHass()
        result = _parse_response(raw, hass)

        assert isinstance(result, VolcastData)
        assert result.energy_today == 25.5
        assert result.api_status == "Active"
        assert result.system_capacity_kwp == 6.5
        assert len(result.forecast) == 1
        assert result.forecast[0].date == "2026-03-20"
        assert result.forecast[0].energy_kwh == 25.5
        assert result.forecast[0].peak_power_kw == 4.2
        assert result.forecast[0].confidence == 0.85

    def test_hourly_parsing(self):
        raw = _make_api_response(
            hourly={
                "2026-03-20": [
                    {"hour": 10, "power_kw": 3.5, "energy_kwh": 3.2},
                    {"hour": 11, "power_kw": 4.0, "energy_kwh": 3.8},
                ],
            },
        )
        result = _parse_response(raw, FakeHass())

        assert "2026-03-20" in result.hourly
        hours = result.hourly["2026-03-20"]
        assert len(hours) == 2
        assert hours[0].hour == 10
        assert hours[0].power_kw == 3.5
        assert hours[0].energy_kwh == 3.2
        assert hours[1].hour == 11

    def test_detailed_parsing(self):
        raw = _make_api_response(
            api_version=2,
            detailed={
                "2026-03-20": [
                    {"time": "10:00", "power_w": 3800, "energy_wh": 317},
                    {"time": "10:05", "power_w": 3850, "energy_wh": 321},
                ],
            },
        )
        result = _parse_response(raw, FakeHass())

        assert "2026-03-20" in result.detailed
        entries = result.detailed["2026-03-20"]
        assert len(entries) == 2
        assert entries[0].time == "10:00"
        assert entries[0].power_w == 3800
        assert entries[1].time == "10:05"

    def test_empty_response(self):
        raw = _make_api_response(state=0)
        result = _parse_response(raw, FakeHass())

        assert result.energy_today == 0
        assert result.forecast == []
        assert result.hourly == {}
        assert result.detailed == {}
        assert result.api_status == "Active"

    def test_missing_optional_fields_use_defaults(self):
        raw = _make_api_response(
            forecast=[{"date": "2026-03-20"}],  # all optional fields missing
            hourly={"2026-03-20": [{}]},  # entry with no fields
        )
        result = _parse_response(raw, FakeHass())

        assert result.forecast[0].energy_kwh == 0
        assert result.forecast[0].peak_power_kw == 0
        assert result.forecast[0].confidence == 0
        assert result.forecast[0].sunshine_hours == 0
        assert result.forecast[0].cloud_cover_pct == 50  # default

        assert result.hourly["2026-03-20"][0].hour == 0
        assert result.hourly["2026-03-20"][0].power_kw == 0


# ---------------------------------------------------------------------------
# _build_wh_hours
# ---------------------------------------------------------------------------


class TestBuildWhHours:
    """Tests for _build_wh_hours."""

    def test_hourly_only(self):
        tz = ZoneInfo("Europe/Warsaw")
        hourly = {
            "2026-03-20": [
                HourlyEntry(hour=10, power_kw=3.5, energy_kwh=3.2),
                HourlyEntry(hour=11, power_kw=4.0, energy_kwh=3.8),
            ],
        }
        result = _build_wh_hours(hourly, {}, tz)

        assert len(result) == 2
        # energy_kwh * 1000 = Wh
        values = list(result.values())
        assert 3200 in values
        assert 3800 in values

    def test_detailed_aggregation_overrides_hourly(self):
        tz = ZoneInfo("Europe/Warsaw")
        hourly = {
            "2026-03-20": [
                HourlyEntry(hour=10, power_kw=3.5, energy_kwh=3.2),
            ],
        }
        # 3 five-minute entries in hour 10
        detailed = {
            "2026-03-20": [
                DetailedEntry(time="10:00", power_w=3500, energy_wh=100),
                DetailedEntry(time="10:05", power_w=3600, energy_wh=110),
                DetailedEntry(time="10:10", power_w=3700, energy_wh=120),
            ],
        }
        result = _build_wh_hours(hourly, detailed, tz)

        assert len(result) == 1
        # Should aggregate detailed: 100 + 110 + 120 = 330, NOT hourly 3200
        values = list(result.values())
        assert values[0] == 330

    def test_empty_input(self):
        result = _build_wh_hours({}, {}, ZoneInfo("UTC"))
        assert result == {}


# ---------------------------------------------------------------------------
# _error_data
# ---------------------------------------------------------------------------


class TestErrorData:
    """Tests for _error_data."""

    def test_returns_valid_empty_data(self):
        result = _error_data("Premium required")
        assert result.api_status == "Premium required"
        assert result.energy_today == 0
        assert result.forecast == []
        assert result.hourly == {}
        assert result.detailed == {}
