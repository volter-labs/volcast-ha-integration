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


def test_attribution_default_when_not_served():
    sensor = _make_sensor(VolcastEnergyTodaySensor, make_sample_data())
    assert sensor.attribution == "Weather: Open-Meteo"


def test_attribution_default_without_data():
    sensor = _make_sensor(VolcastEnergyTodaySensor, None)
    assert sensor.attribution == "Weather: Open-Meteo"


def test_non_forecast_sensors_have_no_attribution():
    from custom_components.volcast.sensor import LastReconciliationSensor, SubmitQueueDepthSensor

    for cls in (SubmitQueueDepthSensor, LastReconciliationSensor):
        assert getattr(cls, "attribution", None) is None
