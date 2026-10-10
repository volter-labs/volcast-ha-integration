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


def test_normalize_readings_reports_dropped_keys():
    from custom_components.volcast.core.control.readings import normalize_readings_report
    raw = {"soc": RawState("55", "%"), "pv_power_w": RawState("unavailable", "W"),
           "power_w": RawState("garbage", "W"), "not_in_profile": RawState("1", "W")}
    values, dropped = normalize_readings_report(raw, GW, "goodwe")
    assert values == {"soc": 55.0}
    assert dropped == ("not_in_profile", "power_w", "pv_power_w")
    # Stara nazwa zostaje cienkim wrapperem — same wartości.
    assert normalize_readings(raw, GW, "goodwe") == values


def test_pick_source_order_entity_register_manual_none():
    from custom_components.volcast.core.control.readings import pick_source
    assert pick_source("soc", 50.0, 51.0, None) == "entity"
    assert pick_source("soc", None, 51.0, None) == "register"
    assert pick_source("battery_capacity_kwh", None, None, 10.0) == "manual"
    assert pick_source("soc", None, None, None) == "none"
    # Nieczytelne wartości to brak odczytu, nie źródło.
    assert pick_source("soc", float("nan"), True, "") == "none"
    assert pick_source("sensor.x", 1.0, None, None) is None          # klucz spoza zamkniętej listy


def test_readings_sources_full_measured_map_counters_only_when_known():
    from custom_components.volcast.core.control.readings import (MEASURED_KEYS, SOURCE_KEYS, SOURCES,
                                                                 readings_sources)
    out = readings_sources(entity={"soc": 50.0, "mode": "auto"}, register={"pv_power_w": 100.0},
                           manual={"battery_capacity_kwh": 10.0}, declared=("load_energy_total_kwh",))
    assert set(MEASURED_KEYS) <= set(out) and set(out) <= set(SOURCE_KEYS)
    assert out["soc"] == "entity" and out["pv_power_w"] == "register" and out["grid_power_w"] == "none"
    assert out["battery_capacity_kwh"] == "manual" and out["load_energy_total_kwh"] == "none"
    assert "battery_charge_total_kwh" not in out and "mode" not in out
    assert set(out.values()) <= set(SOURCES)
    assert len(SOURCE_KEYS) == 19 and len(MEASURED_KEYS) == 12


def test_readings_sources_fit_server_meta_ceiling():
    import json
    from custom_components.volcast.core.control.readings import SOURCE_KEYS, readings_sources
    everything = dict.fromkeys(SOURCE_KEYS, 1.0)
    out = readings_sources(entity={}, register=everything, manual={}, declared=())
    assert len(out) == 19
    meta = {"access": "direct", "model": "mode_setpoint", "readings_source": out}
    assert len(json.dumps(meta).encode()) <= 1500
