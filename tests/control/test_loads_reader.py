from types import SimpleNamespace

from custom_components.volcast.control.loads_reader import LoadsReader

from .ha_fakes import FakeStates

ROLES = {"status": "sensor.wb_status", "setpoint": "number.wb_current", "power": "sensor.wb_power",
         "energy": "sensor.wb_energy"}


def _reader(chargers, states):
    hass = SimpleNamespace(states=states)
    return LoadsReader(hass, SimpleNamespace(options={"ev_chargers": chargers} if chargers is not None else {}))


def _states(status="charging", power=("7350", "W"), energy=("1.42", "kWh"), setpoint=None, options=None):
    s = FakeStates()
    s.set("sensor.wb_status", status, {"options": options} if options else {})
    if power:
        s.set("sensor.wb_power", power[0], {"unit_of_measurement": power[1]})
    if energy:
        s.set("sensor.wb_energy", energy[0], {"unit_of_measurement": energy[1]})
    if setpoint:
        s.set("number.wb_current", setpoint[0], setpoint[1])
    return s


def _one(states, roles=ROLES):
    (entry,) = _reader([{"device_id": "dev1", "label": "Wallbox", "roles": roles}], states).read()
    return entry


def test_entry_shape_and_setpoint_from_number_attributes():
    opts = ["available", "charging", "fault"]
    e = _one(_states(options=opts, setpoint=("16", {"unit_of_measurement": "A", "min": 6, "max": 32, "step": 1})))
    assert e == {"key": "dev1", "kind": "ev_charger", "label": "Wallbox", "control_class": "regulated",
                 "status_raw": "charging", "status_options": opts, "power_w": 7350.0, "energy_kwh": 1.42,
                 "setpoint": {"unit": "A", "value": 16.0, "min": 6.0, "max": 32.0, "step": 1.0},
                 "source": {"executor": "ha", "device_ref": "number.wb_current"}}


def test_status_raw_is_verbatim_for_any_state():
    for raw in ("charging", "suspended", "unavailable", "unknown", "SuspendedEV"):
        assert _one(_states(status=raw))["status_raw"] == raw


def test_missing_status_entity_gives_null_and_no_options():
    e = _one(_states(), roles={"energy": "sensor.wb_energy"})
    assert e["status_raw"] is None and e["status_options"] is None


def test_missing_energy_entity_gives_null():
    assert _one(_states(energy=None))["energy_kwh"] is None
    assert _one(_states(), roles={"status": "sensor.wb_status"})["energy_kwh"] is None


def test_unavailable_energy_gives_null():
    assert _one(_states(energy=("unavailable", "kWh")))["energy_kwh"] is None


def test_power_kw_converted_to_w_and_mw():
    assert _one(_states(power=("7.35", "kW")))["power_w"] == 7350.0
    assert _one(_states(power=("0.002", "MW")))["power_w"] == 2000.0


def test_energy_wh_and_mwh_converted_to_kwh():
    assert _one(_states(energy=("1420", "Wh")))["energy_kwh"] == 1.42
    assert _one(_states(energy=("0.0015", "MWh")))["energy_kwh"] == 1.5


def test_unknown_units():
    e = _one(_states(power=("5", "hp"), energy=("5", "J")))
    assert "power_w" not in e and e["energy_kwh"] is None
    e = _one(_states(power=("5", None)))
    assert "power_w" not in e


def test_power_unavailable_is_omitted():
    assert "power_w" not in _one(_states(power=("unavailable", "W")))


def test_setpoint_kw_converted_and_unknown_unit_null():
    e = _one(_states(setpoint=("7.4", {"unit_of_measurement": "kW", "min": 1.4, "max": 11, "step": 0.1})))
    assert e["setpoint"] == {"unit": "W", "value": 7400.0, "min": 1400.0, "max": 11000.0, "step": 100.0}
    assert _one(_states(setpoint=("7", {"unit_of_measurement": "%"})))["setpoint"] is None


def test_no_setpoint_role_gives_null_and_device_ref_is_device_id():
    e = _one(_states(), roles={"status": "sensor.wb_status"})
    assert e["setpoint"] is None and e["source"] == {"executor": "ha", "device_ref": "dev1"}


def test_no_confirmed_chargers_gives_none():
    assert _reader(None, FakeStates()).read() is None
    assert _reader([], FakeStates()).read() is None
    assert _reader([{"label": "x", "roles": {}}], FakeStates()).read() is None


def test_label_falls_back_to_none_and_cap_at_four():
    r = _reader([{"device_id": f"d{i}", "roles": {}} for i in range(6)], FakeStates())
    out = r.read()
    assert len(out) == 4 and out[0]["label"] is None
