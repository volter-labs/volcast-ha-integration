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


def test_milli_units_are_unknown_not_mega():
    e = _one(_states(power=("3", "mW"), energy=("500", "mWh")))
    assert "power_w" not in e and e["energy_kwh"] is None


def test_unit_case_is_exact_for_supported_units():
    e = _one(_states(power=("2", "kW"), energy=("3", "MWh")))
    assert e["power_w"] == 2000.0 and e["energy_kwh"] == 3000.0
    e = _one(_states(power=("5", "W"), energy=("500", "Wh")))
    assert e["power_w"] == 5.0 and e["energy_kwh"] == 0.5
    assert _one(_states(power=("1", "GW")))["power_w"] == 1e9
    assert _one(_states(energy=("2", "kWh")))["energy_kwh"] == 2.0


def test_negative_power_is_null():
    assert _one(_states(power=("-3.5", "kW")))["power_w"] is None


def test_setpoint_with_unavailable_value_keeps_null_value():
    e = _one(_states(setpoint=("unavailable", {"unit_of_measurement": "A", "min": 6, "max": 32, "step": 1})))
    assert e["setpoint"] == {"unit": "A", "value": None, "min": 6.0, "max": 32.0, "step": 1.0}


def test_unavailable_status_keeps_options():
    e = _one(_states(status="unavailable", options=["available", "charging"]))
    assert e["status_raw"] == "unavailable" and e["status_options"] == ["available", "charging"]


def test_one_broken_charger_does_not_drop_the_rest(monkeypatch, caplog):
    import logging
    caplog.set_level(logging.WARNING, logger="custom_components.volcast.control")
    r = _reader([{"device_id": "bad", "roles": {}}, {"device_id": "ok", "label": "B", "roles": {}}], FakeStates())
    real = LoadsReader._entry_for

    def flaky(self, key, charger):
        if key == "bad":
            raise RuntimeError("sensor.secret")
        return real(self, key, charger)
    monkeypatch.setattr(LoadsReader, "_entry_for", flaky)
    assert [e["key"] for e in r.read()] == ["ok"]
    assert [e["key"] for e in r.read()] == ["ok"]
    warns = [x for x in caplog.records if x.levelno == logging.WARNING]
    assert len(warns) == 1 and "secret" not in caplog.text and "RuntimeError" in caplog.text
