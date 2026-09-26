import pytest

from custom_components.volcast.core.control.readings import RawState, manual_reading, normalize_readings
from custom_components.volcast.core.profile import load_builtin

GW = load_builtin("goodwe-et")


def test_fahrenheit_battery_temp_is_celsius():
    out = normalize_readings({"battery_temp_c": RawState("82.4", "°F")}, GW, "goodwe")
    assert out["battery_temp_c"] == pytest.approx(28.0)


def test_normalize_drops_unreadable_and_keeps_rest():
    raw = {
        "soc": RawState("55", "%"),
        "pv_power_w": RawState("unavailable", "W"),
        "grid_power_w": RawState("1.2", "kW"),          # transform negate, kW → W
        "mode": RawState("sell_power"),
        "export_limit_enabled": RawState("on"),
        "soc_min": RawState("80", "%"),                  # DoD 80 → próg 20
        "power_w": RawState("garbage", "W"),
        "not_in_profile": RawState("1", "W"),
    }
    out = normalize_readings(raw, GW, "goodwe")
    assert out == {"soc": 55.0, "grid_power_w": -1200.0, "mode": "sell_power",
                   "export_limit_enabled": 1.0, "soc_min": 20.0}


def test_unknown_select_option_is_not_a_reading():
    assert normalize_readings({"mode": RawState("eco_charge")}, GW, "goodwe") == {}


def test_manual_reading_units_and_negate():
    assert manual_reading("load_power_w", RawState("2", "kW")) == 2000.0
    assert manual_reading("grid_power_w", RawState("300", "W"), negate=True) == -300.0
    assert manual_reading("soc", RawState("unknown", "%")) is None

def test_manual_reading_fahrenheit_and_incompatible_unit():
    assert manual_reading("battery_temp_c", RawState("82.4", "°F")) == pytest.approx(28.0)
    assert manual_reading("soc", RawState("55", "W")) is None      # obca jednostka = brak odczytu


def test_manual_reading_negate_zero_is_plain_zero():
    value = manual_reading("grid_power_w", RawState("0", "W"), negate=True)
    assert value == 0.0 and str(value) == "0.0"


def test_one_bad_unit_does_not_drop_other_readings():
    out = normalize_readings({"soc": RawState("55", "W"), "battery_temp_c": RawState("301.15", "K")},
                             GW, "goodwe")
    assert out == {"battery_temp_c": pytest.approx(28.0)}


def test_unknown_integration_domain_gives_no_readings():
    assert normalize_readings({"soc": RawState("55", "%")}, GW, "not_a_domain") == {}
