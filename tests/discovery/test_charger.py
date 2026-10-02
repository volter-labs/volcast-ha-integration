import importlib
import json
from pathlib import Path

import pytest

from custom_components.volcast.core.discovery.charger import classify_chargers
from custom_components.volcast.core.discovery.classify import classify
from custom_components.volcast.core.discovery.models import (
    Classification, ConfigEntrySnap, DeviceSnap, EntitySnap, StateSnap,
)

FIXTURES = Path(__file__).parent / "fixtures"
# moduł, nie funkcję: pakiet eksportuje funkcję `classify` pod tą samą nazwą
classify_mod = importlib.import_module("custom_components.volcast.core.discovery.classify")


def _load(name):
    """Migawka rejestrów z pliku: urządzenia, encje (z `capabilities`), stany, wpisy."""
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


def _ids(finding):
    return {role: r.entity_id for role, r in finding.roles.items()}


def test_tuya_local_charger_all_roles_found():
    devices, entities, states, _ = _load("tuya_local_evcharger")
    found = classify_chargers(devices, entities, states)
    assert len(found) == 1
    f = found[0]
    assert f.device_id == "device-0001"
    assert _ids(f) == {
        "status": "sensor.ev_charger_status",
        "setpoint": "number.ev_charger_charge_current",
        "start_stop": "select.ev_charger_charging_operation",
        "power": "sensor.ev_charger_moc",
        "energy": "sensor.ev_charger_energia",
    }
    sp = f.roles["setpoint"]
    assert (sp.unit, sp.min, sp.max, sp.step) == ("A", 6.0, 16.0, 1.0)
    assert "plugged_in" in f.roles["status"].options
    assert f.missing == ()
    assert f.confidence == "high"


def test_second_brand_all_roles_found():
    devices, entities, states, _ = _load("foreign_evcharger")
    found = classify_chargers(devices, entities, states)
    assert len(found) == 1
    f = found[0]
    assert _ids(f) == {
        "status": "sensor.go_e_charger_status",
        "setpoint": "number.go_e_charger_max_current",
        "start_stop": "switch.go_e_charger_charging_allowed",
        "power": "sensor.go_e_charger_charging_power",
        "energy": "sensor.go_e_charger_energy_total",
    }
    assert f.roles["start_stop"].kind == "switch"
    assert f.confidence == "high"


def test_dimmer_is_not_a_charger():
    devices, entities, states, _ = _load("dimmer_trap")
    # pułapka ma nastawę w W z zakresem i enum ze stanem "charging" — bez stanu złącza
    ids = {e.entity_id for e in entities}
    assert {"number.dimmer_minimum_power", "sensor.dimmer_battery_state"} <= ids
    assert classify_chargers(devices, entities, states) == []


def test_roles_found_from_registry_capabilities_without_states():
    # discovery_runner woła classify z states={}; min/max/step i opcje przychodzą wtedy
    # wyłącznie z capabilities rejestru encji (EntitySnap.capabilities)
    devices, entities, _, _ = _load("tuya_local_evcharger")
    f = classify_chargers(devices, entities, {})[0]
    assert f.roles["setpoint"].max == 16.0
    assert f.missing == ()


def test_setpoint_range_falls_back_to_state_attributes():
    devices, entities, states, _ = _load("foreign_evcharger")
    bare = [EntitySnap(**{**e.__dict__, "capabilities": None}) for e in entities]
    f = classify_chargers(devices, bare, states)[0]
    assert (f.roles["setpoint"].min, f.roles["setpoint"].max) == (6, 16)


def _dev(id="d1", name="Box"):
    return DeviceSnap(id=id, manufacturer="X", model=None, name=name, sw_version=None,
                      hw_version=None, serial_number=None, identifiers=(),
                      config_entry_ids=("e1",))


def _ent(eid, device_class=None, unit=None, caps=None, disabled=False, name=None, device="d1",
         uid=None):
    return EntitySnap(entity_id=eid, platform="p", unique_id=uid or eid, device_id=device,
                      config_entry_id="e1", device_class=device_class, unit=unit,
                      translation_key=None, original_name=name, disabled=disabled,
                      capabilities=caps)


_STATUS = _ent("sensor.st", "enum", caps={"options": ["available", "plugged_in", "charging"]})
_SETPOINT = _ent("number.cur", "current", "A", caps={"min": 6, "max": 32, "step": 1})


def test_status_alone_is_not_a_charger():
    assert classify_chargers([_dev()], [_STATUS], {}) == []


def test_control_without_status_is_not_a_charger():
    sw = _ent("switch.charge", name="Charging")
    assert classify_chargers([_dev()], [_SETPOINT, sw], {}) == []


def test_minimum_finding_lists_missing_roles_and_low_confidence():
    f = classify_chargers([_dev()], [_STATUS, _SETPOINT], {})[0]
    assert set(f.roles) == {"status", "setpoint"}
    assert f.missing == ("start_stop", "power", "energy")
    assert f.confidence == "low"


def test_medium_confidence_when_only_control_role_missing():
    power = _ent("sensor.p", "power", "W")
    energy = _ent("sensor.e", "energy", "kWh")
    f = classify_chargers([_dev()], [_STATUS, _SETPOINT, power, energy], {})[0]
    assert f.missing == ("start_stop",)
    assert f.confidence == "medium"


def test_battery_status_of_inverter_is_not_a_charger():
    # falownik: stan baterii charging/discharging + nastawa prądu ładowania baterii w A
    batt = _ent("sensor.battery_state", "enum",
                caps={"options": ["charging", "discharging", "idle"]})
    cur = _ent("number.battery_max_charge_current", "current", "A",
               caps={"min": 0, "max": 185, "step": 1})
    mode = _ent("select.work_mode", caps={"options": ["Selling first", "Zero export"]})
    assert classify_chargers([_dev()], [batt, cur, mode], {}) == []


def test_setpoint_must_be_amps_or_watts_with_range():
    no_range = _ent("number.cur", "current", "A")
    wrong_unit = _ent("number.delay", "duration", "h", caps={"min": 0, "max": 15, "step": 1})
    assert classify_chargers([_dev()], [_STATUS, no_range, wrong_unit], {}) == []


def test_setpoint_in_watts_accepted():
    watts = _ent("number.limit", "power", "W", caps={"min": 1400, "max": 11000, "step": 100})
    f = classify_chargers([_dev()], [_STATUS, watts], {})[0]
    assert (f.roles["setpoint"].unit, f.roles["setpoint"].max) == ("W", 11000)


def test_start_stop_buttons_need_both():
    start = _ent("button.start", name="Start charging")
    stop = _ent("button.stop", name="Stop charging")
    f = classify_chargers([_dev()], [_STATUS, start, stop], {})[0]
    assert (f.roles["start"].entity_id, f.roles["stop"].entity_id) == ("button.start",
                                                                     "button.stop")
    assert "start_stop" not in f.missing
    assert classify_chargers([_dev()], [_STATUS, start], {}) == []


def test_restart_button_is_not_start():
    restart = _ent("button.restart", name="Restart")
    stop = _ent("button.stop", name="Stop charging")
    assert classify_chargers([_dev()], [_STATUS, restart, stop], {}) == []


def test_plug_binary_sensor_is_not_a_status():
    # status musi mieć listę stanów (options); on/off wtyczki jej nie ma
    plug = _ent("binary_sensor.plug", "plug")
    sw = _ent("switch.charge", name="Charging")
    assert classify_chargers([_dev()], [plug, _SETPOINT, sw], {}) == []


def test_disabled_entities_and_other_devices_ignored():
    off_status = _ent("sensor.st", "enum", disabled=True,
                      caps={"options": ["plugged_in", "charging"]})
    other = _ent("number.cur", "current", "A", caps={"min": 6, "max": 16, "step": 1},
                 device="d2")
    assert classify_chargers([_dev(), _dev("d2")], [off_status, _SETPOINT], {}) == []
    assert classify_chargers([_dev(), _dev("d2")], [_STATUS, other], {}) == []


def test_classify_adds_chargers_without_touching_other_fields():
    devices, entities, states, entries = _load("tuya_local_evcharger")
    c = classify(devices, entities, entries, states)
    assert [f.device_id for f in c.chargers] == ["device-0001"]
    assert c.inverters == [] and c.price_entities == []
    assert Classification([], [], []).chargers == []


def test_device_name_tokens_do_not_make_any_switch_a_start_stop():
    # entity_id zawiera slug nazwy urządzenia ("ev_charger") — nie może podpowiadać roli
    devices, entities, states, _ = _load("tuya_local_evcharger")
    lock = EntitySnap(entity_id="switch.ev_charger_child_lock", platform="tuya_local",
                      unique_id="UID0001-switch_child_lock", device_id="device-0001",
                      config_entry_id="entry-0001", device_class=None, unit=None,
                      translation_key=None, original_name="Child lock", disabled=False)
    f = classify_chargers(devices, entities + [lock], states)[0]
    assert f.roles["start_stop"].entity_id == "select.ev_charger_charging_operation"


def test_robot_vacuum_is_not_a_charger():
    status = _ent("sensor.robot_status", "enum", caps={"options": [
        "cleaning", "returning", "charging", "charger_disconnected", "idle"]})
    resume = _ent("switch.robot_resume", name="Resume cleaning")
    assert classify_chargers([_dev(name="Robot")], [status, resume], {}) == []


def test_inverter_battery_with_available_state_is_not_a_charger():
    batt = _ent("sensor.battery_status", "enum",
                caps={"options": ["available", "charging", "discharging"]})
    cur = _ent("number.battery_charge_current", "current", "A",
               caps={"min": 0, "max": 25, "step": 1})
    assert classify_chargers([_dev(name="Inverter")], [batt, cur], {}) == []


def test_car_integration_is_still_found():
    # integracja auta ma te same role co ładowarka; użytkownik potwierdza ręcznie
    status = _ent("sensor.car_charging", "enum", caps={"options": [
        "starting", "charging", "stopped", "complete", "disconnected", "no_power"]})
    cable = _ent("binary_sensor.car_charge_cable", "plug")
    sw = _ent("switch.car_charge", name="Charge")
    cur = _ent("number.car_charge_current", "current", "A",
               caps={"min": 0, "max": 32, "step": 1})
    found = classify_chargers([_dev(name="Model 3")], [status, cable, sw, cur], {})
    assert len(found) == 1
    assert {"status", "setpoint", "start_stop"} <= set(found[0].roles)


def test_charger_classifier_failure_does_not_break_classify(monkeypatch, caplog):
    devices, entities, states, entries = _load("tuya_local_evcharger")

    def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(classify_mod, "classify_chargers", boom)
    c = classify(devices, entities, entries, states)
    assert c.chargers == []
    assert c.energy_candidates  # reszta klasyfikacji nietknięta
    assert any("charger" in r.getMessage() and r.levelname == "WARNING" for r in caplog.records)


def test_package_exports_charger_api():
    from custom_components.volcast.core import discovery
    assert {"classify_chargers", "ChargerFinding", "ChargerRole"} <= set(discovery.__all__)
    assert discovery.classify_chargers is classify_chargers


@pytest.mark.parametrize("dev_name, eid, name, with_setpoint", [
    ("Charging station", "switch.charging_station_charging", "Charging", False),
    ("Charger", "switch.charger_charger", "Charger", False),
    ("Charge Point", "switch.charge_point_charge_control", "Charge control", True),
    ("Wallbox Charge", "switch.wallbox_charge_charge", "Charge", True),
])
def test_hint_word_in_device_name_still_matches_entity_name(dev_name, eid, name, with_setpoint):
    # nazwa urządzenia zdejmowana tylko jako prefiks entity_id; nazwa encji liczy się w całości
    sw = _ent(eid, name=name, uid="u-switch")
    ents = [_STATUS, sw] + ([_SETPOINT] if with_setpoint else [])
    found = classify_chargers([_dev(name=dev_name)], ents, {})
    assert len(found) == 1
    assert found[0].roles["start_stop"].entity_id == eid


def test_unique_id_with_device_name_does_not_hint_a_role():
    # kształt OCPP: unique_id niesie nazwę urządzenia ("charger"), więc nie może podpowiadać
    avail = _ent("switch.charger_availability", name="Availability",
                 uid="switch.ocpp.charger.availability")
    control = _ent("switch.charger_charge_control", name="Charge control",
                   uid="switch.ocpp.charger.charge_control")
    f = classify_chargers([_dev(name="charger")], [_STATUS, avail, control], {})[0]
    assert f.roles["start_stop"].entity_id == "switch.charger_charge_control"


def test_non_ascii_device_name_prefix_is_stripped():
    # HA transliteruje nazwę urządzenia w entity_id: "Ładowarka EV Charger" -> ladowarka_ev_charger
    lock = _ent("switch.ladowarka_ev_charger_child_lock", name="Child lock", uid="u-lock")
    op = _ent("select.ladowarka_ev_charger_operation", name="Operation", uid="u-op",
              caps={"options": ["Start charging", "Stop charging"]})
    f = classify_chargers([_dev(name="Ładowarka EV Charger")], [_STATUS, lock, op], {})[0]
    assert f.roles["start_stop"].entity_id == "select.ladowarka_ev_charger_operation"


def test_adapter_slug_strips_prefix_for_non_latin_device_name():
    # nazwa cyrylicą: przybliżenie ASCII daje pusty slug, prefiks "start_" zostałby w tekście encji
    dev = DeviceSnap(id="d1", manufacturer="X", model=None, name="Старт", sw_version=None,
                     hw_version=None, serial_number=None, identifiers=(),
                     config_entry_ids=("e1",), slug="start")
    sw = _ent("switch.start_socket")
    assert "start_stop" not in classify_chargers([dev], [_STATUS, _SETPOINT, sw], {})[0].roles
    bare = DeviceSnap(**{**dev.__dict__, "slug": None})
    assert "start_stop" in classify_chargers([bare], [_STATUS, _SETPOINT, sw], {})[0].roles
