"""Tests for sensor data-source attribution."""

from __future__ import annotations

from custom_components.volcast.control_entities import VolcastPriceSensor
from custom_components.volcast.coordinator import DEFAULT_ATTRIBUTION
from custom_components.volcast.sensor import VolcastBaseSensor, VolcastEnergyTodaySensor
from tests.conftest import make_sample_data
from tests.test_sensor_attributes import _make_sensor

TEXT = "Weather data: Open-Meteo (CC BY 4.0); includes experimental data.\n© 2024-6 Google LLC"


def test_attribution_follows_coordinator_data():
    data = make_sample_data()
    data.attribution = TEXT
    sensor = _make_sensor(VolcastEnergyTodaySensor, data)
    assert sensor.attribution == TEXT


def test_default_attribution_when_not_served():
    sensor = _make_sensor(VolcastEnergyTodaySensor, make_sample_data())
    assert sensor.attribution == DEFAULT_ATTRIBUTION == "Weather: Open-Meteo"


def test_default_attribution_without_data():
    sensor = _make_sensor(VolcastEnergyTodaySensor, None)
    assert sensor.attribution == DEFAULT_ATTRIBUTION


def test_non_forecast_sensors_do_not_get_attribution():
    """Czujniki ceny i sterowania oraz dane na żywo nie dziedziczą po bazie czujników prognozy."""
    from custom_components.volcast.binary_sensor import IntegrationHealthyBinarySensor
    from custom_components.volcast.button import VolcastSyncButton
    from custom_components.volcast.control.direct_sensors import DirectSensor
    from custom_components.volcast.control_entities import (
        VolcastControlStatusSensor, VolcastControlSwitch, VolcastPlanSensor)
    from custom_components.volcast.discovery_entities import VolcastDiscoverySensor
    from custom_components.volcast.verification_entities import VolcastVerificationSensor

    # na żywo (DirectSensor), sterowanie, wykrywanie, ceny, zdrowie, weryfikacja, przycisk
    for cls in (DirectSensor, VolcastControlStatusSensor, VolcastPlanSensor, VolcastControlSwitch,
                VolcastDiscoverySensor, VolcastPriceSensor, IntegrationHealthyBinarySensor,
                VolcastVerificationSensor, VolcastSyncButton):
        assert "attribution" not in vars(cls), cls
        assert getattr(cls, "_attr_attribution", None) is None, cls
        assert not issubclass(cls, VolcastBaseSensor), cls
