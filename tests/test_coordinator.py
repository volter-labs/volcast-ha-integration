"""Tests for Volcast coordinator — API response parsing and wh_hours."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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

from unittest.mock import patch

from custom_components.volcast.coordinator import VolcastCoordinator
from custom_components.volcast.const import FORECAST_HISTORY_RETENTION_DAYS
from custom_components.volcast.http_retry import RetryResult


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
        # energy_today is derived from the forecast entry matching HA's local
        # "today" — fixture date must therefore be computed at runtime.
        tz = ZoneInfo("Europe/Warsaw")
        today_str = datetime.now(tz).strftime("%Y-%m-%d")
        raw = _make_api_response(
            forecast=[
                {
                    "date": today_str,
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
        assert result.forecast[0].date == today_str
        assert result.forecast[0].energy_kwh == 25.5
        assert result.forecast[0].peak_power_kw == 4.2
        assert result.forecast[0].confidence == 0.85

    def test_today_energy_from_forecast_not_state(self):
        """Regression: energy_today must come from forecast array (HA local
        timezone), not from server's `state` field which is UTC-date-based.

        Bug exposed on 2026-05-17: Polish user in evening, UTC=2026-05-16,
        local=2026-05-17. Server returned state=8.65 (UTC-today=Polish-yesterday)
        and forecast[2026-05-17]=7.33. HA Today sensor showed 8.65 instead of
        7.33.
        """
        tz = ZoneInfo("Europe/Warsaw")
        today_str = datetime.now(tz).strftime("%Y-%m-%d")
        yesterday_str = (datetime.now(tz) - timedelta(days=1)).strftime("%Y-%m-%d")
        tomorrow_str = (datetime.now(tz) + timedelta(days=1)).strftime("%Y-%m-%d")

        raw = _make_api_response(
            state=99.99,  # bogus value — must NOT leak into energy_today
            forecast=[
                {"date": yesterday_str, "energy_kwh": 8.65, "peak_power_kw": 1.278},
                {"date": today_str, "energy_kwh": 7.33, "peak_power_kw": 0.937},
                {"date": tomorrow_str, "energy_kwh": 23.87, "peak_power_kw": 3.137},
            ],
        )
        result = _parse_response(raw, FakeHass())

        assert result.energy_today == 7.33, (
            f"energy_today should come from forecast[today], got {result.energy_today} "
            f"— likely still reading raw.state"
        )
        assert result.energy_tomorrow == 23.87

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


# ---------------------------------------------------------------------------
# _async_update_data — refactored to delegate to http_with_retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coordinator_uses_http_with_retry_on_success():
    """Coordinator delegates to http_with_retry and parses success result.

    Test scope: verify the helper IS called with the right method/url. The
    parsed VolcastData is intentionally not asserted (sample_payload is flat
    rather than wrapped in {'attributes': {...}} as _parse_response expects);
    parser correctness is covered by the dedicated _parse_response tests above.
    """
    coord = VolcastCoordinator(
        hass=FakeHass(),
        api_key="test",
        api_url="http://stub/forecast",
        update_interval_minutes=15,
    )

    sample_payload: dict[str, Any] = {}  # parser returns empty VolcastData; we don't assert on it

    with patch("custom_components.volcast.coordinator.http_with_retry") as mock_http, \
         patch("custom_components.volcast.coordinator.async_get_clientsession"):
        mock_http.return_value = RetryResult(
            success=True, status=200, data=sample_payload, attempts=2,
        )
        await coord._async_update_data()

    assert mock_http.call_count == 1
    # Verify it was called with method=GET (forecast is GET)
    assert mock_http.call_args.kwargs["method"] == "GET"
    assert "key=test" in mock_http.call_args.kwargs["url"]


@pytest.mark.asyncio
async def test_coordinator_raises_UpdateFailed_after_retry_exhaustion_5xx():
    """When http_with_retry returns failure with 503, coordinator raises UpdateFailed."""
    from homeassistant.helpers.update_coordinator import UpdateFailed

    coord = VolcastCoordinator(
        hass=FakeHass(), api_key="test",
        api_url="http://stub/forecast", update_interval_minutes=15,
    )

    with patch("custom_components.volcast.coordinator.http_with_retry") as mock_http, \
         patch("custom_components.volcast.coordinator.async_get_clientsession"):
        mock_http.return_value = RetryResult(
            success=False, status=503, attempts=4,
            last_error="HTTP 503", retriable=True,
        )
        with pytest.raises(UpdateFailed):
            await coord._async_update_data()


@pytest.mark.asyncio
async def test_coordinator_401_raises_UpdateFailed_immediately():
    """401 from http_with_retry → UpdateFailed (Invalid API key)."""
    from homeassistant.helpers.update_coordinator import UpdateFailed

    coord = VolcastCoordinator(
        hass=FakeHass(), api_key="bad",
        api_url="http://stub/forecast", update_interval_minutes=15,
    )

    with patch("custom_components.volcast.coordinator.http_with_retry") as mock_http, \
         patch("custom_components.volcast.coordinator.async_get_clientsession"):
        mock_http.return_value = RetryResult(
            success=False, status=401, attempts=1,
            last_error="non-retriable HTTP 401", retriable=False,
        )
        with pytest.raises(UpdateFailed, match="API key"):
            await coord._async_update_data()


@pytest.mark.asyncio
async def test_coordinator_403_returns_premium_required_error_data():
    """403 from http_with_retry → return error_data (premium required), NO UpdateFailed."""
    coord = VolcastCoordinator(
        hass=FakeHass(), api_key="non-premium",
        api_url="http://stub/forecast", update_interval_minutes=15,
    )

    with patch("custom_components.volcast.coordinator.http_with_retry") as mock_http, \
         patch("custom_components.volcast.coordinator.async_get_clientsession"):
        mock_http.return_value = RetryResult(
            success=False, status=403, attempts=1,
            last_error="non-retriable HTTP 403", retriable=False,
        )
        # Should NOT raise — should return error_data structure
        result = await coord._async_update_data()
        assert result is not None
        # api_status field carries the error message (existing _error_data behavior)
        assert result.api_status == "Premium required"


# ---------------------------------------------------------------------------
# Forecast history retention (past-day Energy Dashboard forecast)
# ---------------------------------------------------------------------------


class TestForecastHistory:
    """The coordinator must retain past-day forecasts so the Energy Dashboard
    keeps showing them after the API drops those days from its response."""

    def _coord(self) -> VolcastCoordinator:
        return VolcastCoordinator(
            hass=FakeHass(), api_key="k", api_url="http://stub", entry_id=None,
        )

    def _ts(self, days_ago: int, hour: int = 10) -> str:
        tz = ZoneInfo("Europe/Warsaw")
        dt = datetime.now(tz) - timedelta(days=days_ago)
        return datetime(
            dt.year, dt.month, dt.day, hour, tzinfo=tz
        ).isoformat()

    @pytest.mark.asyncio
    async def test_empty_history_returns_none(self):
        coord = self._coord()
        assert coord.get_solar_forecast_wh_hours() is None

    @pytest.mark.asyncio
    async def test_merge_accumulates_past_days(self):
        """Yesterday's forecast must survive a poll that only returns today."""
        coord = self._coord()
        yesterday, today = self._ts(1), self._ts(0)

        await coord._async_merge_forecast_history({yesterday: 3200})
        # Next poll: API no longer returns yesterday, only today.
        await coord._async_merge_forecast_history({today: 4100})

        merged = coord.get_solar_forecast_wh_hours()
        assert merged == {yesterday: 3200, today: 4100}

    @pytest.mark.asyncio
    async def test_merge_newest_wins_for_same_timestamp(self):
        """Intra-day re-polls (nowcast) override today's value, not duplicate it."""
        coord = self._coord()
        today = self._ts(0)

        await coord._async_merge_forecast_history({today: 4000})
        await coord._async_merge_forecast_history({today: 4500})

        assert coord.get_solar_forecast_wh_hours() == {today: 4500}

    @pytest.mark.asyncio
    async def test_prune_drops_entries_beyond_retention(self):
        coord = self._coord()
        stale = self._ts(FORECAST_HISTORY_RETENTION_DAYS + 3)
        fresh = self._ts(1)

        await coord._async_merge_forecast_history({stale: 1000, fresh: 2000})

        merged = coord.get_solar_forecast_wh_hours()
        assert stale not in merged
        assert fresh in merged

    @pytest.mark.asyncio
    async def test_unparseable_key_is_pruned(self):
        coord = self._coord()
        await coord._async_merge_forecast_history({"not-a-timestamp": 5})
        assert coord.get_solar_forecast_wh_hours() is None

    @pytest.mark.asyncio
    async def test_update_data_merges_into_history(self):
        """A successful poll feeds wh_hours into the retained history."""
        tz = ZoneInfo("Europe/Warsaw")
        today_str = datetime.now(tz).strftime("%Y-%m-%d")
        coord = self._coord()

        raw = _make_api_response(
            hourly={today_str: [{"hour": 11, "power_kw": 4.0, "energy_kwh": 3.8}]},
        )
        with patch(
            "custom_components.volcast.coordinator.http_with_retry"
        ) as mock_http, patch(
            "custom_components.volcast.coordinator.async_get_clientsession"
        ):
            mock_http.return_value = RetryResult(
                success=True, status=200, data=raw, attempts=1,
            )
            await coord._async_update_data()

        merged = coord.get_solar_forecast_wh_hours()
        assert merged is not None
        assert 3800 in merged.values()
