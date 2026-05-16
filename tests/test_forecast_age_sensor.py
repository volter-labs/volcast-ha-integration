"""Tests for VolcastForecastAgeSensor.

This sensor exposes the WALL-CLOCK age of the server's forecast generation
timestamp, distinct from the server-self-reported `cache_age_minutes`.
Useful for automations that need to detect server-side staleness
(eg. the 11h freeze observed during investigation) without waiting for
the next poll cycle to refresh the server-reported value.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from custom_components.volcast.coordinator import VolcastData
from custom_components.volcast.sensor import VolcastForecastAgeSensor
from tests.conftest import FakeCoordinator, FakeHass


def _make_data(generated_at: str = "", cache_age_minutes: int = 0) -> VolcastData:
    return VolcastData(
        energy_today=0,
        energy_tomorrow=0,
        forecast=[],
        hourly={},
        detailed={},
        wh_hours={},
        system_capacity_kwp=None,
        location="",
        generated_at=generated_at,
        cache_age_minutes=cache_age_minutes,
        api_version=2,
        api_status="Active",
    )


def _make_sensor(data: VolcastData | None):
    """Build sensor bypassing __init__ (HA wiring would fail in tests)."""
    coordinator = FakeCoordinator(data)
    entry = MagicMock()
    entry.entry_id = "test_entry"

    sensor = VolcastForecastAgeSensor.__new__(VolcastForecastAgeSensor)
    sensor.coordinator = coordinator
    sensor.hass = FakeHass()
    sensor._attr_has_entity_name = True
    sensor._attr_unique_id = "test"
    sensor._attr_device_info = {}
    sensor._entry_id = entry.entry_id
    return sensor


class TestNativeValue:

    def test_fresh_forecast_small_age(self):
        """generated_at 5 min ago → native_value ≈ 5."""
        now = datetime.now(timezone.utc)
        gen = (now - timedelta(minutes=5)).isoformat()
        sensor = _make_sensor(_make_data(generated_at=gen))
        value = sensor.native_value
        assert isinstance(value, int)
        assert 4 <= value <= 6  # small jitter tolerance

    def test_stale_forecast_large_age(self):
        """generated_at 670 min ago (the real observed freeze) → native_value ≈ 670."""
        now = datetime.now(timezone.utc)
        gen = (now - timedelta(minutes=670)).isoformat()
        sensor = _make_sensor(_make_data(generated_at=gen))
        value = sensor.native_value
        assert isinstance(value, int)
        assert 669 <= value <= 671

    def test_z_suffix_iso_parses(self):
        """Volcast backend returns ISO 8601 with 'Z' suffix — must parse."""
        now = datetime.now(timezone.utc)
        # Build ISO 8601 with Z suffix, no microseconds for cleanliness
        gen = (now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        sensor = _make_sensor(_make_data(generated_at=gen))
        value = sensor.native_value
        assert value is not None
        assert 29 <= value <= 31

    def test_naive_datetime_assumed_utc(self):
        """Defensive: ISO without tz offset → assume UTC."""
        now = datetime.now(timezone.utc)
        gen = (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S")
        sensor = _make_sensor(_make_data(generated_at=gen))
        value = sensor.native_value
        assert value is not None
        assert 9 <= value <= 11

    def test_future_timestamp_clamps_to_zero(self):
        """Server clock ahead of ours (negative delta) → 0, not negative."""
        now = datetime.now(timezone.utc)
        gen = (now + timedelta(minutes=30)).isoformat()
        sensor = _make_sensor(_make_data(generated_at=gen))
        assert sensor.native_value == 0

    def test_empty_generated_at_returns_none(self):
        sensor = _make_sensor(_make_data(generated_at=""))
        assert sensor.native_value is None

    def test_no_data_returns_none(self):
        sensor = _make_sensor(None)
        assert sensor.native_value is None

    def test_malformed_iso_returns_none(self):
        sensor = _make_sensor(_make_data(generated_at="not-an-iso-string"))
        assert sensor.native_value is None


class TestExtraStateAttributes:

    def test_surfaces_generated_at_and_server_cache_age(self):
        now = datetime.now(timezone.utc)
        gen = (now - timedelta(minutes=42)).isoformat()
        sensor = _make_sensor(_make_data(generated_at=gen, cache_age_minutes=40))
        attrs = sensor.extra_state_attributes
        assert attrs["generated_at"] == gen
        # Server-reported and wall-clock can diverge slightly — that's the point
        assert attrs["server_reported_cache_age_minutes"] == 40

    def test_no_data_returns_empty_attrs(self):
        sensor = _make_sensor(None)
        assert sensor.extra_state_attributes == {}


class TestSensorRegistration:
    """The sensor is registered alongside the existing four diagnostic sensors."""

    @pytest.mark.asyncio
    async def test_async_setup_entry_includes_forecast_age(self):
        """async_setup_entry must add VolcastForecastAgeSensor to its entity list."""
        from custom_components.volcast.sensor import async_setup_entry

        added: list = []

        def _capture(entities):
            added.extend(entities)

        hass = FakeHass()
        hass.data = {"volcast": {"test_entry": {"coordinator": FakeCoordinator(_make_data())}}}
        entry = MagicMock()
        entry.entry_id = "test_entry"
        entry.options = {}

        await async_setup_entry(hass, entry, _capture)  # type: ignore[arg-type]

        classes = [type(e).__name__ for e in added]
        assert "VolcastForecastAgeSensor" in classes

        # Verify the entity is wired with the expected diagnostic properties,
        # not just present in the list.
        forecast_age = next(e for e in added if type(e).__name__ == "VolcastForecastAgeSensor")
        descr = forecast_age.entity_description
        assert descr.key == "forecast_age"
        assert descr.translation_key == "forecast_age"
        # entity_category / unit / state_class are MagicMocks in test stubs;
        # assert by identity against the same stub the production code uses.
        from homeassistant.const import EntityCategory, UnitOfTime
        from homeassistant.components.sensor import SensorStateClass
        assert descr.entity_category is EntityCategory.DIAGNOSTIC
        assert descr.native_unit_of_measurement is UnitOfTime.MINUTES
        assert descr.state_class is SensorStateClass.MEASUREMENT


class TestSelfRefreshTimer:
    """The sensor's value advances between coordinator polls via a 1-min self-refresh."""

    @pytest.mark.asyncio
    async def test_added_to_hass_registers_timer(self):
        """async_added_to_hass must subscribe to a 1-minute interval timer.

        Uses `unittest.mock.patch` as a context manager so the sensor-module
        attribute is auto-restored on exit — prevents leakage into later
        tests that would otherwise still see the mocked function.
        """
        from datetime import timedelta
        from unittest.mock import MagicMock, patch

        unsub = MagicMock()
        sensor = _make_sensor(_make_data())

        with patch(
            "custom_components.volcast.sensor.async_track_time_interval",
            return_value=unsub,
        ) as track_mock:
            await sensor.async_added_to_hass()

            track_mock.assert_called_once()
            # Third positional is the timedelta(minutes=1)
            assert timedelta(minutes=1) in track_mock.call_args.args
            assert sensor._unsub_refresh is unsub

    @pytest.mark.asyncio
    async def test_will_remove_unsubscribes_timer(self):
        from unittest.mock import MagicMock

        sensor = _make_sensor(_make_data())
        unsub = MagicMock()
        sensor._unsub_refresh = unsub

        await sensor.async_will_remove_from_hass()

        unsub.assert_called_once()
        assert sensor._unsub_refresh is None

    @pytest.mark.asyncio
    async def test_will_remove_when_no_timer_is_safe(self):
        """Removal before async_added_to_hass should not crash."""
        sensor = _make_sensor(_make_data())
        sensor._unsub_refresh = None
        # Should not raise
        await sensor.async_will_remove_from_hass()
        assert sensor._unsub_refresh is None

    @pytest.mark.asyncio
    async def test_refresh_handler_writes_state(self):
        """The timer callback should call async_write_ha_state to push fresh value."""
        from unittest.mock import MagicMock
        sensor = _make_sensor(_make_data())
        sensor.async_write_ha_state = MagicMock()
        await sensor._async_handle_refresh(None)
        sensor.async_write_ha_state.assert_called_once()
