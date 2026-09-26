import pytest

from custom_components.volcast.core.entity_map import (
    EntityCandidate, entity_value, entity_writes, resolve_entities,
)
from custom_components.volcast.core.params import Params, TouProgram
from custom_components.volcast.core.profile import load_builtin, profile_from_dict
from tests.core.profile_fixtures import tw_profile

GW = load_builtin("goodwe-et")
SN = "GOLDENSERIAL0000"


def _c(eid, key, platform="goodwe"):
    return EntityCandidate(eid, platform, f"goodwe-{key}-{SN}")


CANDS = [
    _c("sensor.battery_state_of_charge", "battery_soc"),
    _c("select.goodwe_ems_mode", "ems_mode"),
    _c("number.goodwe_ems_power_limit", "ems_power_limit"),
    _c("number.goodwe_depth_of_discharge_on_grid", "battery_discharge_depth"),
    _c("number.goodwe_grid_export_limit", "grid_export_limit"),
    _c("sensor.other_battery_soc", "battery_soc", platform="solarman"),   # obca platforma
]


def test_resolve_by_platform_and_unique_id_not_entity_id():
    r = resolve_entities(GW, "goodwe", CANDS)
    assert r.mapped["soc"] == "sensor.battery_state_of_charge"
    assert r.mapped["mode"] == "select.goodwe_ems_mode"
    assert "export_limit_enabled" in r.missing and not r.ambiguous


def test_ambiguous_entity_is_not_mapped():
    r = resolve_entities(GW, "goodwe", CANDS + [_c("sensor.soc_copy", "battery_soc")])
    assert "soc" not in r.mapped and r.ambiguous["soc"] == ["sensor.battery_state_of_charge", "sensor.soc_copy"]


def test_writes_in_profile_order_with_dod_inversion_and_mode_last():
    mapped = resolve_entities(GW, "goodwe", CANDS).mapped
    writes, unmapped = entity_writes(
        Params(mode="sell_power", power_w=400.0, soc_min=20.0, export_limit_w=500.0,
               export_limit_enabled=True), GW, "goodwe", mapped)
    assert [(w.entity_id, w.service, w.data) for w in writes] == [
        ("number.goodwe_depth_of_discharge_on_grid", "set_value", {"value": 80.0}),
        ("number.goodwe_ems_power_limit", "set_value", {"value": 400.0}),
        ("number.goodwe_grid_export_limit", "set_value", {"value": 500.0}),
        ("select.goodwe_ems_mode", "select_option", {"option": "sell_power"}),
    ]
    assert unmapped == ["export_limit_enabled"]


def test_switch_service():
    writes, _ = entity_writes(Params(export_limit_enabled=False), GW, "goodwe",
                              {"export_limit_enabled": "switch.goodwe_export"})
    assert (writes[0].domain, writes[0].service, writes[0].data) == ("switch", "turn_off", {})
    writes, _ = entity_writes(Params(export_limit_enabled=True), GW, "goodwe",
                              {"export_limit_enabled": "switch.goodwe_export"})
    assert writes[0].service == "turn_on"


def test_keys_filter_limits_writes_but_keeps_order():
    mapped = resolve_entities(GW, "goodwe", CANDS).mapped
    writes, unmapped = entity_writes(
        Params(mode="sell_power", power_w=400.0, soc_min=20.0), GW, "goodwe", mapped,
        keys=["mode", "soc_min"])
    assert [w.key for w in writes] == ["soc_min", "mode"] and unmapped == []


def test_read_transforms():
    assert entity_value("grid_power_w", "1500", GW, "goodwe") == -1500.0
    assert entity_value("soc_min", "90", GW, "goodwe") == 10.0
    assert entity_value("mode", "battery_standby", GW, "goodwe") == "battery_standby"
    assert entity_value("mode", "eco_charge", GW, "goodwe") is None
    assert entity_value("soc", "unavailable", GW, "goodwe") is None
    # przełącznik i czas — uzgadnianie musi widzieć także te encje
    assert entity_value("export_limit_enabled", "on", GW, "goodwe") == 1.0
    assert entity_value("export_limit_enabled", "off", GW, "goodwe") == 0.0
    assert entity_value("export_limit_enabled", "unknown", GW, "goodwe") is None


@pytest.mark.parametrize("state", ["nan", "inf", "-inf", "abc"])
def test_read_non_finite_or_garbage_is_none(state):
    assert entity_value("soc", state, GW, "goodwe") is None


# --- jednostki: HA potrafi przeliczyć encję na jednostkę użytkownika (np. °F) ---

def test_temperature_in_fahrenheit_is_converted_to_celsius():
    assert entity_value("battery_temp_c", "82.4", GW, "goodwe", unit="°F") == pytest.approx(28.0)
    assert entity_value("battery_temp_c", "28.0", GW, "goodwe", unit="°C") == pytest.approx(28.0)
    assert entity_value("battery_temp_c", "301.15", GW, "goodwe", unit="K") == pytest.approx(28.0)


def test_power_units_are_converted_before_transform():
    assert entity_value("grid_power_w", "1.5", GW, "goodwe", unit="kW") == pytest.approx(-1500.0)
    assert entity_value("pv_power_w", "2.25", GW, "goodwe", unit="kW") == pytest.approx(2250.0)
    assert entity_value("export_limit_w", "8000.0", GW, "goodwe", unit="W") == 8000.0


def test_missing_unit_means_canonical_unit():
    assert entity_value("battery_temp_c", "28", GW, "goodwe", unit=None) == 28.0
    assert entity_value("battery_temp_c", "28", GW, "goodwe", unit="") == 28.0


@pytest.mark.parametrize("key,unit", [
    ("battery_temp_c", "W"), ("battery_temp_c", "bogus"), ("soc", "W"), ("pv_power_w", "°C"),
    ("soc_min", "kWh"),
])
def test_incompatible_unit_fails_closed(key, unit):
    assert entity_value(key, "50", GW, "goodwe", unit=unit) is None


def test_percent_passes_with_percent_unit():
    assert entity_value("soc", "89", GW, "goodwe", unit="%") == 89.0
    assert entity_value("soc_min", "90.0", GW, "goodwe", unit="%") == 10.0


# --- syntetyczny odpowiednik raportu wykrywania z instalacji GoodWe (integracja z HACS) ---
# Kształt `unique_id` jak w źródłach integracji: `goodwe-<klucz>-<SN>` dla sensorów,
# liczb i wyborów, `<klucz>-<SN>` (bez prefiksu domeny) dla przełączników i części przycisków.
# Nazwy encji syntetyczne. Wabiki: klucze z tym samym początkiem co klucze profilu.

def _raw(eid, uid, platform="goodwe"):
    return EntityCandidate(eid, platform, uid.replace("<SN>", SN))


REPORT_LIKE = [
    _raw("sensor.gw_soc", "goodwe-battery_soc-<SN>"),
    _raw("sensor.gw_pv", "goodwe-ppv-<SN>"),
    _raw("sensor.gw_pv1", "goodwe-ppv1-<SN>"),
    _raw("sensor.gw_pv2", "goodwe-ppv2-<SN>"),
    _raw("sensor.gw_batt_power", "goodwe-pbattery1-<SN>"),
    _raw("sensor.gw_house", "goodwe-house_consumption-<SN>"),
    _raw("sensor.gw_active_power", "goodwe-active_power-<SN>"),
    _raw("sensor.gw_active_power_l1", "goodwe-active_power1-<SN>"),
    _raw("sensor.gw_grid", "goodwe-active_power_total-<SN>"),
    _raw("sensor.gw_meter_total", "goodwe-meter_active_power_total-<SN>"),
    _raw("sensor.gw_batt_temp", "goodwe-battery_temperature-<SN>"),
    _raw("sensor.gw_temp", "goodwe-temperature-<SN>"),
    _raw("select.gw_operation", "goodwe-operation_mode-<SN>"),
    _raw("select.gw_ems", "goodwe-ems_mode-<SN>"),
    _raw("number.gw_ems_power", "goodwe-ems_power_limit-<SN>"),
    _raw("number.gw_dod", "goodwe-battery_discharge_depth-<SN>"),
    _raw("number.gw_dod_backup", "goodwe-battery_discharge_depth_offline-<SN>"),
    _raw("number.gw_soc_upper", "goodwe-soc_upper_limit-<SN>"),
    _raw("number.gw_export_limit", "goodwe-grid_export_limit-<SN>"),
    _raw("number.gw_eco_soc", "goodwe-eco_mode_soc-<SN>"),
    _raw("switch.gw_export_limit", "grid_export_limit_switch-<SN>"),
    _raw("switch.gw_load_control", "load_control-<SN>"),
    _raw("switch.gw_dod_holding", "dod_holding_switch-<SN>"),
    _raw("button.gw_sync_clock", "synchronize_clock-<SN>"),
    _raw("button.gw_sync_clock_2", "goodwe-synchronize_clock-<SN>"),
    _raw("sensor.other_soc", "goodwe-battery_soc-<SN>", platform="other"),
]


def test_goodwe_profile_resolves_every_key_on_report_shaped_candidates():
    r = resolve_entities(GW, "goodwe", REPORT_LIKE)
    assert not r.missing and not r.ambiguous
    assert r.mapped == {
        "soc": "sensor.gw_soc",
        "pv_power_w": "sensor.gw_pv",
        "battery_power_w": "sensor.gw_batt_power",
        "load_power_w": "sensor.gw_house",
        "grid_power_w": "sensor.gw_grid",
        "battery_temp_c": "sensor.gw_batt_temp",
        "mode": "select.gw_ems",
        "power_w": "number.gw_ems_power",
        "soc_min": "number.gw_dod",
        "soc_max": "number.gw_soc_upper",
        "export_limit_w": "number.gw_export_limit",
        "export_limit_enabled": "switch.gw_export_limit",
    }


def test_domain_must_match_entity_id_prefix():
    # ten sam unique_id pod inną domeną encji nie jest kandydatem
    r = resolve_entities(GW, "goodwe", [_raw("number.gw_soc", "goodwe-battery_soc-<SN>")])
    assert "soc" in r.missing


def test_unknown_integration_domain_raises():
    with pytest.raises(KeyError):
        resolve_entities(GW, "solarman", REPORT_LIKE)


# --- encje czasu (programy okien czasowych) ---

def _tw_with_ha():
    raw = tw_profile()
    raw["ha"] = {"integrations": [{"domain": "inv", "ems": False, "status": "draft", "entities": {
        "tou_1_start": {"domain": "time", "unique_id_regex": "_prog1_time$"},
        "tou_1_soc": {"domain": "number", "unique_id_regex": "_prog1_soc$"},
        "tou_1_grid_charge": {"domain": "switch", "unique_id_regex": "_prog1_charge$"},
    }}]}
    return profile_from_dict(raw)


def test_time_entity_read_as_minutes_of_day():
    tw = _tw_with_ha()
    assert entity_value("tou_1_start", "05:30:00", tw, "inv") == 330.0
    assert entity_value("tou_1_start", "23:59", tw, "inv") == 1439.0
    assert entity_value("tou_1_start", "00:00:00", tw, "inv") == 0.0


@pytest.mark.parametrize("state", ["24:00", "12:60", "noon", "5", "unavailable", "1:2:3:4"])
def test_time_entity_garbage_is_none(state):
    assert entity_value("tou_1_start", state, _tw_with_ha(), "inv") is None


def test_tou_writes_use_time_service_and_profile_field_order():
    tw = _tw_with_ha()
    mapped = {"tou_1_start": "time.p1", "tou_1_soc": "number.p1_soc",
              "tou_1_grid_charge": "switch.p1_charge"}
    writes, unmapped = entity_writes(
        Params(tou=(TouProgram(start_min=330, power_w=3000.0, soc=80.0, grid_charge=True),)),
        tw, "inv", mapped)
    assert [(w.key, w.domain, w.service, w.data) for w in writes] == [
        ("tou.1.soc", "number", "set_value", {"value": 80.0}),
        ("tou.1.grid_charge", "switch", "turn_on", {}),
        ("tou.1.start", "time", "set_value", {"time": "05:30:00"}),
    ]
    assert unmapped == ["tou.1.power_w"]
