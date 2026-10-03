"""Tests for sensor data-source attribution."""

from __future__ import annotations

from custom_components.volcast.sensor import VolcastEnergyTodaySensor
from tests.conftest import make_sample_data
from tests.test_sensor_attributes import _make_sensor


def test_attribution_follows_coordinator_data():
    data = make_sample_data()
    data.attribution = "Weather data: Open-Meteo (CC BY 4.0); includes experimental data."
    sensor = _make_sensor(VolcastEnergyTodaySensor, data)
    assert sensor.attribution == data.attribution


def test_attribution_none_when_not_served():
    sensor = _make_sensor(VolcastEnergyTodaySensor, make_sample_data())
    assert sensor.attribution is None


def test_attribution_none_without_data():
    sensor = _make_sensor(VolcastEnergyTodaySensor, None)
    assert sensor.attribution is None
