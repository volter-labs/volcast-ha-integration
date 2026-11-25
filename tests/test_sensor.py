"""Tests for sensor data-source attribution."""

from __future__ import annotations

from custom_components.volcast.sensor import VolcastEnergyTodaySensor
from tests.conftest import FakeCoordinator, make_sample_data
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


def test_peak_production_sensor_shows_attribution():
    from custom_components.volcast.binary_sensor import VolcastPeakProductionSensor

    data = make_sample_data()
    data.attribution = "Weather data: Open-Meteo (CC BY 4.0); includes experimental data."
    sensor = VolcastPeakProductionSensor.__new__(VolcastPeakProductionSensor)
    sensor.coordinator = FakeCoordinator(data)
    assert sensor.attribution == data.attribution
    sensor.coordinator = FakeCoordinator(None)
    assert sensor.attribution == "Weather: Open-Meteo"


def test_non_forecast_entities_have_no_attribution():
    """Wszystkie encje spoza prognozy (bez danych prognozy) nie mają atrybucji."""
    from homeassistant.helpers.update_coordinator import CoordinatorEntity

    from custom_components.volcast import binary_sensor, button, sensor

    classes = []
    for mod in (sensor, binary_sensor, button):
        for obj in vars(mod).values():
            if (
                isinstance(obj, type)
                and obj.__module__ == mod.__name__
                and not issubclass(obj, CoordinatorEntity)
            ):
                classes.append(obj)
    assert classes
    for cls in classes:
        assert getattr(cls, "attribution", None) is None, cls.__name__
