"""Tests for VolcastForecastAgeSensor (wall-clock age of the server's forecast)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from custom_components.volcast.coordinator import VolcastData
from custom_components.volcast.sensor import VolcastForecastAgeSensor
from tests.conftest import FakeCoordinator, FakeHass


def _make_data(generated_at: str = "", cache_age_minutes: int = 0) -> VolcastData:
    return VolcastData(
        energy_today=0, energy_tomorrow=0, forecast=[], hourly={}, detailed={},
        wh_hours={}, system_capacity_kwp=None, location="",
        generated_at=generated_at, cache_age_minutes=cache_age_minutes,
        api_version=2, api_status="Active",
    )


def _make_sensor(data: VolcastData | None, *, last_success=None, failures: int = 0):
    """Czujnik zbudowany z pominięciem __init__ (okablowanie HA nie działa w testach)."""
    coordinator = FakeCoordinator(data)
    coordinator.last_update_success_time = last_success
    coordinator.consecutive_failures = failures
    sensor = VolcastForecastAgeSensor.__new__(VolcastForecastAgeSensor)
    sensor.coordinator = coordinator
    sensor.hass = FakeHass()
    sensor._attr_unique_id = "test"
    sensor._unsub_refresh = None
    return sensor


def _ago(minutes: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


class TestNativeValue:
    def test_fresh_forecast_small_age(self):
        value = _make_sensor(_make_data(_ago(5).isoformat())).native_value
        assert isinstance(value, int) and 4 <= value <= 6

    def test_stale_forecast_large_age(self):
        value = _make_sensor(_make_data(_ago(670).isoformat())).native_value
        assert 669 <= value <= 671

    def test_z_suffix_iso_parses(self):
        gen = _ago(30).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert 29 <= _make_sensor(_make_data(gen)).native_value <= 31

    def test_offset_iso_parses(self):
        gen = _ago(20).astimezone(timezone(timedelta(hours=2))).isoformat()
        assert gen.endswith("+02:00")
        assert 19 <= _make_sensor(_make_data(gen)).native_value <= 21

    def test_naive_datetime_assumed_utc(self):
        gen = _ago(10).strftime("%Y-%m-%dT%H:%M:%S")
        assert 9 <= _make_sensor(_make_data(gen)).native_value <= 11

    def test_future_timestamp_clamps_to_zero(self):
        gen = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        assert _make_sensor(_make_data(gen)).native_value == 0

    def test_empty_generated_at_returns_none(self):
        assert _make_sensor(_make_data("")).native_value is None

    def test_no_data_returns_none(self):
        assert _make_sensor(None).native_value is None

    def test_malformed_iso_returns_none(self):
        assert _make_sensor(_make_data("not-an-iso-string")).native_value is None


class TestExtraStateAttributes:
    def test_surfaces_server_and_local_diagnostics(self):
        gen = _ago(42).isoformat()
        sensor = _make_sensor(_make_data(gen, cache_age_minutes=40),
                              last_success=_ago(7), failures=3)
        attrs = sensor.extra_state_attributes
        assert attrs["generated_at"] == gen
        assert attrs["server_reported_cache_age_minutes"] == 40
        assert 6 <= attrs["minutes_since_last_success"] <= 8
        assert attrs["consecutive_failures"] == 3

    def test_no_success_yet_gives_none(self):
        attrs = _make_sensor(_make_data(_ago(1).isoformat())).extra_state_attributes
        assert attrs["minutes_since_last_success"] is None
        assert attrs["consecutive_failures"] == 0

    def test_no_data_returns_empty_attrs(self):
        assert _make_sensor(None).extra_state_attributes == {}


class TestSensorRegistration:
    @pytest.mark.asyncio
    async def test_setup_entry_adds_forecast_age_with_diagnostic_description(self):
        from homeassistant.components.sensor import SensorStateClass
        from homeassistant.const import EntityCategory, UnitOfTime

        from custom_components.volcast.sensor import async_setup_entry

        added: list = []
        hass = FakeHass()
        hass.data = {"volcast": {"test_entry": {"coordinator": FakeCoordinator(_make_data())}}}
        entry = MagicMock()
        entry.entry_id = "test_entry"
        await async_setup_entry(hass, entry, added.extend)

        sensor = next(e for e in added if type(e).__name__ == "VolcastForecastAgeSensor")
        d = sensor.entity_description
        assert d.key == d.translation_key == "forecast_age"
        assert d.entity_category is EntityCategory.DIAGNOSTIC
        assert d.native_unit_of_measurement is UnitOfTime.MINUTES
        assert d.state_class is SensorStateClass.MEASUREMENT
        assert d.icon == "mdi:clock-alert-outline"
        assert sensor._attr_unique_id == "test_entry_forecast_age"

    @pytest.mark.asyncio
    async def test_setup_entry_without_coordinator_skips_it(self):
        from custom_components.volcast.sensor import async_setup_entry

        added: list = []
        hass = FakeHass()
        hass.data = {"volcast": {"test_entry": {}}}
        entry = MagicMock()
        entry.entry_id = "test_entry"
        await async_setup_entry(hass, entry, added.extend)
        assert all(type(e).__name__ != "VolcastForecastAgeSensor" for e in added)


class TestSelfRefreshTimer:
    @pytest.mark.asyncio
    async def test_added_to_hass_registers_one_minute_timer(self):
        unsub = MagicMock()
        sensor = _make_sensor(_make_data())
        with patch("custom_components.volcast.sensor.async_track_time_interval",
                   return_value=unsub) as track:
            await sensor.async_added_to_hass()
        track.assert_called_once()
        assert timedelta(minutes=1) in track.call_args.args
        assert sensor._unsub_refresh is unsub

    @pytest.mark.asyncio
    async def test_will_remove_unsubscribes_timer(self):
        sensor = _make_sensor(_make_data())
        unsub = MagicMock()
        sensor._unsub_refresh = unsub
        await sensor.async_will_remove_from_hass()
        unsub.assert_called_once()
        assert sensor._unsub_refresh is None

    @pytest.mark.asyncio
    async def test_will_remove_without_timer_is_safe(self):
        sensor = _make_sensor(_make_data())
        await sensor.async_will_remove_from_hass()
        assert sensor._unsub_refresh is None

    @pytest.mark.asyncio
    async def test_refresh_handler_writes_state(self):
        sensor = _make_sensor(_make_data())
        sensor.async_write_ha_state = MagicMock()
        await sensor._async_handle_refresh(None)
        sensor.async_write_ha_state.assert_called_once()
