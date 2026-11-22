import json
from pathlib import Path

import pytest

from custom_components.volcast.core.discovery.assign import (
    FIELDS, MAX_CANDIDATES, Candidate, FieldProposal, Proposal, propose,
)
from custom_components.volcast.core.discovery.classify import classify
from custom_components.volcast.core.discovery.models import (
    ConfigEntrySnap, DeviceSnap, EntitySnap, StateSnap,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name):
    """Migawka rejestrów z pliku (ten sam kształt co fixtury ładowarek)."""
    raw = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    devices = [
        DeviceSnap(id=d["id"], manufacturer=d["manufacturer"], model=d["model"], name=d["name"],
                   sw_version=d["sw_version"], hw_version=d["hw_version"],
                   serial_number=d["serial_number"],
                   identifiers=tuple(tuple(i) for i in d["identifiers"]),
                   config_entry_ids=tuple(d["config_entry_ids"]))
        for d in raw["devices"]
    ]
    entities = [
        EntitySnap(entity_id=e["entity_id"], platform=e["platform"], unique_id=e["unique_id"],
                   device_id=e["device_id"], config_entry_id=e["config_entry_id"],
                   device_class=e["device_class"], unit=e["unit"],
                   translation_key=e["translation_key"], original_name=e["original_name"],
                   disabled=e["disabled"], capabilities=e.get("capabilities"))
        for e in raw["entities"]
    ]
    states = {s["entity_id"]: StateSnap(s["entity_id"], s["state"], s["attributes"])
              for s in raw["states"]}
    entries = [ConfigEntrySnap(c["entry_id"], c["domain"], c["title"], c["host"])
               for c in raw["config_entries"]]
    return devices, entities, states, entries


def _propose(name, **kw):
    # tak jak warstwa HA: najpierw klasyfikacja (znaleziska falowników), potem propozycja
    devices, entities, states, entries = _load(name)
    inverters = classify(devices, entities, entries, states).inverters
    return propose(entities, states, devices, inverters, **kw)


def _ids(fp: FieldProposal):
    return [c.entity_id for c in fp.candidates]


def _conf(fp: FieldProposal, entity_id):
    return next(c.confidence for c in fp.candidates if c.entity_id == entity_id)


def _all_ids(p: Proposal):
    return {c.entity_id for fp in p.fields for c in fp.candidates}


def test_fields_match_telemetry_fields():
    # rdzeń nie importuje control/ — stała musi się zgadzać z polami formularza „Installation details”
    from custom_components.volcast.control.telemetry import TELEMETRY_FIELDS
    assert FIELDS == tuple(TELEMETRY_FIELDS)


# ── czujniki szablonowe bez urządzenia ──────────────────────────────────────

def test_template_sensors_without_device_are_all_certain():
    p = _propose("assign_template_sensors")
    assert p.chosen() == {
        "soc": "sensor.battery_soc",
        "pv_power_w": "sensor.solar_power",
        "battery_power_w": "sensor.battery_power",
        "grid_power_w": "sensor.grid_power",
        "load_power_w": "sensor.house_load",
        "pv_energy_total_kwh": "sensor.solar_energy_total",
        "grid_import_total_kwh": "sensor.grid_import_total",
        "grid_export_total_kwh": "sensor.grid_export_total",
    }


def test_disabled_own_and_forecast_entities_are_never_candidates():
    p = _propose("assign_template_sensors")
    ids = _all_ids(p)
    assert "sensor.solar_power_old" not in ids        # wyłączona
    assert "sensor.volcast_pv_power" not in ids       # encja samej wtyczki
    assert "sensor.power_production_now" not in ids   # prognoza PV, nie pomiar


def test_kw_unit_is_a_power_candidate_with_unit_reason():
    p = _propose("assign_template_sensors")
    best = p.field("battery_power_w").candidates[0]
    assert best.entity_id == "sensor.battery_power"
    assert best.unit == "kW"
    assert "unit:kW" in best.reasons


def test_daily_counter_listed_below_lifetime_total_and_not_high():
    fp = _propose("assign_template_sensors").field("pv_energy_total_kwh")
    assert _ids(fp)[0] == "sensor.solar_energy_total"
    assert _conf(fp, "sensor.solar_energy_today") != "high"


def test_phone_battery_level_is_low_for_soc():
    fp = _propose("assign_template_sensors").field("soc")
    assert _conf(fp, "sensor.pixel_8_battery_level") == "low"


# ── jeden falownik ──────────────────────────────────────────────────────────

def test_one_inverter_all_fields_certain():
    p = _propose("assign_one_inverter")
    assert p.chosen() == {
        "soc": "sensor.battery_state_of_charge",
        "pv_power_w": "sensor.pv_power",
        "battery_power_w": "sensor.battery_power",
        "grid_power_w": "sensor.meter_active_power_total",
        "load_power_w": "sensor.house_consumption",
        "pv_energy_total_kwh": "sensor.total_pv_generation",
        "grid_import_total_kwh": "sensor.total_energy_import",
        "grid_export_total_kwh": "sensor.total_energy_export",
    }


def test_one_inverter_strings_and_phases_are_medium_not_high():
    p = _propose("assign_one_inverter")
    pv = p.field("pv_power_w")
    assert _conf(pv, "sensor.pv1_power") == "medium"
    assert _conf(pv, "sensor.pv2_power") == "medium"
    assert _conf(p.field("grid_power_w"), "sensor.on_grid_l1_power") == "medium"


def test_one_inverter_excludes_disabled_own_and_wrong_quantity():
    p = _propose("assign_one_inverter")
    ids = _all_ids(p)
    assert "sensor.load_power" not in ids             # wyłączona
    assert "sensor.volcast_battery_soc" not in ids    # encja samej wtyczki
    assert "sensor.battery_temperature" not in ids    # zła wielkość


def test_one_inverter_candidates_carry_inverter_device_reason():
    best = _propose("assign_one_inverter").field("pv_power_w").candidates[0]
    assert best.device_id == "device-inv"
    assert "inverter_device" in best.reasons


def test_generic_name_without_subject_is_low():
    fp = _propose("assign_one_inverter").field("pv_power_w")
    assert _conf(fp, "sensor.active_power") == "low"


# ── dwa falowniki ───────────────────────────────────────────────────────────

def test_two_inverters_ambiguous_fields_have_no_choice_but_list_both():
    p = _propose("assign_two_inverters")
    for field in ("pv_power_w", "grid_power_w", "load_power_w"):
        fp = p.field(field)
        assert fp.chosen is None, field
        highs = [c.entity_id for c in fp.candidates if c.confidence == "high"]
        assert len(highs) == 2, field


def test_two_inverters_battery_only_on_one_is_certain():
    p = _propose("assign_two_inverters")
    assert p.field("soc").chosen == "sensor.inverter_a_battery_soc"
    assert p.field("battery_power_w").chosen == "sensor.inverter_a_battery_power"


def test_two_inverters_same_device_as_chosen_fields_ranks_first():
    fp = _propose("assign_two_inverters").field("pv_power_w")
    first = fp.candidates[0]
    assert first.entity_id == "sensor.inverter_a_pv_power"
    assert any(r.startswith("same_device:") for r in first.reasons)


def test_device_name_prefix_of_entity_id_is_not_read_as_name_words():
    # „solar_inverter_” to slug urządzenia; encja mówi „grid”, więc dla PV jest tylko niska
    dev = DeviceSnap(id="d1", manufacturer="Acme", model=None, name="Solar inverter", sw_version=None,
                     hw_version=None, serial_number=None, identifiers=(), config_entry_ids=())
    ent = EntitySnap(entity_id="sensor.solar_inverter_grid_power", platform="acme", unique_id="u1",
                     device_id="d1", config_entry_id=None, device_class="power", unit="W",
                     translation_key=None, original_name="Grid power", disabled=False,
                     capabilities={"state_class": "measurement"})
    p = propose([ent], {}, [dev], ())
    assert _conf(p.field("pv_power_w"), "sensor.solar_inverter_grid_power") == "low"
    assert p.field("grid_power_w").chosen == "sensor.solar_inverter_grid_power"


# ── osobny licznik sieci ────────────────────────────────────────────────────

def test_separate_grid_meter_is_certain_for_grid_power_by_device_name():
    p = _propose("assign_grid_meter")
    fp = p.field("grid_power_w")
    assert fp.chosen == "sensor.grid_meter_power"
    best = fp.candidates[0]
    assert best.unit == "kW"
    assert "device_name:grid" in best.reasons


def test_separate_grid_meter_export_certain_import_left_to_user():
    p = _propose("assign_grid_meter")
    assert p.field("grid_export_total_kwh").chosen == "sensor.grid_meter_total_active_returned_energy"
    imp = p.field("grid_import_total_kwh")
    assert imp.chosen is None
    assert imp.candidates[0].entity_id == "sensor.grid_meter_total_active_energy"
    assert imp.candidates[0].confidence == "medium"


def test_separate_grid_meter_other_fields_from_inverter_and_template():
    assert _propose("assign_grid_meter").chosen() == {
        "soc": "sensor.battery_state_of_capacity",
        "pv_power_w": "sensor.inverter_input_power",
        "battery_power_w": "sensor.battery_charge_discharge_power",
        "grid_power_w": "sensor.grid_meter_power",
        "load_power_w": "sensor.house_load",
        "pv_energy_total_kwh": "sensor.inverter_total_yield",
        "grid_export_total_kwh": "sensor.grid_meter_total_active_returned_energy",
    }


# ── jednostki i wielkości ───────────────────────────────────────────────────

def _ent(entity_id, device_class, unit, name, *, state_class="measurement", platform="template",
         disabled=False):
    return EntitySnap(entity_id=entity_id, platform=platform, unique_id=f"uid-{entity_id}",
                      device_id=None, config_entry_id=None, device_class=device_class, unit=unit,
                      translation_key=None, original_name=name, disabled=disabled,
                      capabilities={"state_class": state_class} if state_class else None)


def test_power_units_w_kw_mw_accepted_and_wrong_units_rejected():
    ents = [_ent("sensor.pv_power_w", "power", "W", "PV power W"),
            _ent("sensor.pv_power_kw", "power", "kW", "PV power kW"),
            _ent("sensor.pv_power_mw", "power", "MW", "PV power MW"),
            _ent("sensor.pv_current", "power", "A", "PV current"),          # niespójna jednostka
            _ent("sensor.pv_apparent", "apparent_power", "VA", "PV apparent"),
            _ent("sensor.pv_energy", "energy", "kWh", "PV energy", state_class="total_increasing")]
    fp = propose(ents, {}, (), (), fields=("pv_power_w",)).field("pv_power_w")
    assert sorted(_ids(fp)) == ["sensor.pv_power_kw", "sensor.pv_power_mw", "sensor.pv_power_w"]
    assert {c.unit for c in fp.candidates} == {"W", "kW", "MW"}


def test_energy_units_kwh_wh_mwh_accepted():
    ents = [_ent("sensor.grid_import_kwh", "energy", "kWh", "Grid import", state_class="total_increasing"),
            _ent("sensor.grid_import_wh", "energy", "Wh", "Grid import", state_class="total_increasing"),
            _ent("sensor.grid_import_mwh", "energy", "MWh", "Grid import", state_class="total"),
            _ent("sensor.grid_import_power", "power", "W", "Grid import power")]
    fp = propose(ents, {}, fields=("grid_import_total_kwh",)).field("grid_import_total_kwh")
    assert sorted(_ids(fp)) == ["sensor.grid_import_kwh", "sensor.grid_import_mwh", "sensor.grid_import_wh"]


def test_soc_requires_percent_and_battery_class():
    ents = [_ent("sensor.battery_soc", "battery", "%", "Battery SoC"),
            _ent("sensor.room_humidity_soc", "humidity", "%", "Humidity"),
            _ent("sensor.battery_soc_kwh", None, "kWh", "Battery SoC kWh")]
    fp = propose(ents, {}, fields=("soc",)).field("soc")
    assert _ids(fp) == ["sensor.battery_soc"]
    assert fp.chosen == "sensor.battery_soc"


def test_unit_and_class_from_state_attributes_when_registry_has_none():
    ent = EntitySnap(entity_id="sensor.solar_power", platform="template", unique_id="u1", device_id=None,
                     config_entry_id=None, device_class=None, unit=None, translation_key=None,
                     original_name=None, disabled=False)
    st = {"sensor.solar_power": StateSnap("sensor.solar_power", "1200", {
        "device_class": "power", "unit_of_measurement": "kW", "state_class": "measurement",
        "friendly_name": "Solar power"})}
    fp = propose([ent], st, fields=("pv_power_w",)).field("pv_power_w")
    assert fp.chosen == "sensor.solar_power"
    assert fp.candidates[0].unit == "kW"


def test_non_sensor_entities_are_ignored():
    ents = [_ent("number.pv_power_limit", "power", "W", "PV power limit"),
            _ent("sensor.pv_power", "power", "W", "PV power")]
    fp = propose(ents, {}, fields=("pv_power_w",)).field("pv_power_w")
    assert _ids(fp) == ["sensor.pv_power"]


# ── limit, wynik i API ─────────────────────────────────────────────────────

def test_candidate_list_is_capped_and_many_highs_leave_no_choice():
    ents = [_ent(f"sensor.pv_power_{i}", "power", "W", f"PV power {i}") for i in range(MAX_CANDIDATES + 4)]
    fp = propose(ents, {}, fields=("pv_power_w",)).field("pv_power_w")
    assert len(fp.candidates) == MAX_CANDIDATES
    assert fp.chosen is None
    assert len(propose(ents, {}, fields=("pv_power_w",), limit=2).field("pv_power_w").candidates) == 2


def test_single_high_among_medium_is_chosen_and_listed_first():
    ents = [_ent("sensor.pv1_power", "power", "W", "PV1 power"),
            _ent("sensor.pv_power", "power", "W", "PV power"),
            _ent("sensor.pv2_power", "power", "W", "PV2 power")]
    fp = propose(ents, {}, fields=("pv_power_w",)).field("pv_power_w")
    assert fp.chosen == "sensor.pv_power"
    assert fp.candidates[0].entity_id == "sensor.pv_power"


def test_field_without_candidates_is_empty_and_not_chosen():
    p = propose([], {})
    assert [fp.field for fp in p.fields] == list(FIELDS)
    assert all(fp.candidates == () and fp.chosen is None for fp in p.fields)
    assert p.chosen() == {}


def test_unknown_field_is_rejected():
    with pytest.raises(ValueError):
        propose([], {}, fields=("pv_power_w", "nope"))


def test_as_dict_is_json_serializable_and_complete():
    p = _propose("assign_one_inverter")
    d = json.loads(json.dumps(p.as_dict()))
    assert set(d["fields"]) == set(FIELDS)
    pv = d["fields"]["pv_power_w"]
    assert pv["chosen"] == "sensor.pv_power"
    c = pv["candidates"][0]
    assert set(c) == {"entity_id", "confidence", "score", "reasons", "device_id", "name", "unit"}
    assert isinstance(c["reasons"], list)
    assert d["chosen"] == p.chosen()


def test_candidate_and_field_are_plain_dataclasses():
    c = Candidate(entity_id="sensor.x", confidence="high", score=8, reasons=("unit:W",))
    assert c.as_dict()["reasons"] == ["unit:W"]
    fp = FieldProposal(field="pv_power_w", candidates=(c,), chosen="sensor.x")
    assert fp.as_dict() == {"field": "pv_power_w", "chosen": "sensor.x", "candidates": [c.as_dict()]}


# ── bez zgadywania: przypadki, które najwyżej „medium” ──────────────────────

def _one(field, entity_id, device_class, unit, *, state_class="measurement", attributes=None, name=None):
    """Ocena pojedynczej encji dla pola (encja jedyna na liście)."""
    ent = _ent(entity_id, device_class, unit, name, state_class=state_class)
    st = {entity_id: StateSnap(entity_id, "1", attributes)} if attributes is not None else {}
    fp = propose([ent], st, fields=(field,)).field(field)
    assert _ids(fp) == [entity_id]
    return fp.candidates[0].confidence, fp.chosen


@pytest.mark.parametrize("entity_id", [
    "sensor.house_ev_charger_power", "sensor.home_heat_pump_power", "sensor.house_socket_power",
    "sensor.plug_consumption", "sensor.wallbox_load_power", "sensor.boiler_load_power",
])
def test_single_loads_are_not_certain_house_load(entity_id):
    conf, chosen = _one("load_power_w", entity_id, "power", "W")
    assert conf != "high" and chosen is None


def test_place_word_alone_is_not_certain_house_load():
    assert _one("load_power_w", "sensor.home_power", "power", "W") == ("medium", None)
    assert _one("load_power_w", "sensor.house_load_power", "power", "W")[0] == "high"


@pytest.mark.parametrize("entity_id", ["sensor.water_meter_power", "sensor.gas_meter_power"])
def test_other_media_meters_are_not_certain_grid(entity_id):
    conf, chosen = _one("grid_power_w", entity_id, "power", "W")
    assert conf != "high" and chosen is None


@pytest.mark.parametrize("field,entity_id,dc,unit", [
    ("battery_power_w", "sensor.ev_battery_power", "power", "W"),
    ("battery_power_w", "sensor.car_battery_power", "power", "W"),
    ("soc", "sensor.car_soc", "battery", "%"),
    ("soc", "sensor.vehicle_battery_soc", "battery", "%"),
])
def test_vehicle_batteries_are_not_the_home_battery(field, entity_id, dc, unit):
    conf, chosen = _one(field, entity_id, dc, unit)
    assert conf != "high" and chosen is None


@pytest.mark.parametrize("field,entity_id", [
    ("pv_power_w", "sensor.pv_a_power"), ("pv_power_w", "sensor.pv_power_a"),
    ("pv_power_w", "sensor.pv_input_1_power"), ("pv_power_w", "sensor.pv_string_b_power"),
    ("grid_power_w", "sensor.grid_l1_power"), ("grid_power_w", "sensor.grid_phase_a_power"),
    ("grid_power_w", "sensor.grid_power_c"),
])
def test_partial_sensors_are_not_certain_totals(field, entity_id):
    conf, chosen = _one(field, entity_id, "power", "W")
    assert conf == "medium" and chosen is None


def test_power_without_measurement_state_class_is_medium():
    assert _one("pv_power_w", "sensor.pv_power", "power", "W", state_class=None) == ("medium", None)
    assert _one("pv_power_w", "sensor.pv_power", "power", "W", state_class="total")[0] == "medium"


def test_energy_total_without_counter_state_class_is_medium():
    for sc in ("measurement", None):
        assert _one("pv_energy_total_kwh", "sensor.pv_energy_total", "energy", "kWh",
                    state_class=sc) == ("medium", None), sc


def test_counter_without_lifetime_signal_is_medium():
    # total_increasing bez słowa „total” — nie odróżnia licznika dobowego od całkowitego
    assert _one("pv_energy_total_kwh", "sensor.solar_energy", "energy", "kWh",
                state_class="total_increasing") == ("medium", None)
    assert _one("grid_import_total_kwh", "sensor.grid_import_energy", "energy", "kWh",
                state_class="total_increasing") == ("medium", None)


def test_state_class_total_without_reset_is_a_lifetime_signal():
    conf, chosen = _one("pv_energy_total_kwh", "sensor.solar_energy", "energy", "kWh", state_class="total",
                        attributes={"state_class": "total"})
    assert conf == "high" and chosen == "sensor.solar_energy"
    conf, _ = _one("pv_energy_total_kwh", "sensor.solar_energy", "energy", "kWh", state_class="total",
                   attributes={"state_class": "total", "last_reset": "2026-11-21T00:00:00+00:00"})
    assert conf == "medium"


@pytest.mark.parametrize("entity_id", ["sensor.solar_power_next_hour", "sensor.pv_power_tomorrow",
                                       "sensor.solar_power_remaining_today"])
def test_forecast_like_names_are_not_pv_power(entity_id):
    conf, chosen = _one("pv_power_w", entity_id, "power", "W")
    assert conf != "high" and chosen is None


@pytest.mark.parametrize("entity_id", ["sensor.grid_import_power", "sensor.grid_export_power"])
def test_one_way_grid_power_is_not_the_signed_grid_power(entity_id):
    conf, chosen = _one("grid_power_w", entity_id, "power", "W")
    assert conf != "high" and chosen is None
