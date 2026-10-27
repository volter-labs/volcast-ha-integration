"""Testy binary_sensor: dostępność przy nieudanym odświeżeniu."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.volcast.binary_sensor import (
    IntegrationHealthyBinarySensor,
    VolcastPeakProductionSensor,
)
from tests.conftest import FakeCoordinator, make_sample_data


def _peak_sensor(coordinator):
    entry = MagicMock()
    entry.entry_id = "test_entry"
    return VolcastPeakProductionSensor(coordinator, entry, 50)


def test_peak_available_with_data_after_failed_refresh():
    coord = FakeCoordinator(make_sample_data())
    coord.last_update_success = False
    assert _peak_sensor(coord).available is True


def test_peak_unavailable_without_data():
    assert _peak_sensor(FakeCoordinator(None)).available is False


def test_healthy_off_when_refresh_failed_even_with_data():
    coord = FakeCoordinator(make_sample_data())
    coord.last_update_success = False
    tracker = MagicMock()
    tracker._queue = []
    sensor = IntegrationHealthyBinarySensor(coord, tracker, None, entry_id="x")
    assert sensor.is_on is False


def test_healthy_off_during_grace_window():
    coord = FakeCoordinator(make_sample_data())
    coord.last_update_success = True
    coord.consecutive_failures = 2
    tracker = MagicMock()
    tracker._queue = []
    sensor = IntegrationHealthyBinarySensor(coord, tracker, None, entry_id="x")
    assert sensor.is_on is False
    coord.consecutive_failures = 0
    assert sensor.is_on is True
