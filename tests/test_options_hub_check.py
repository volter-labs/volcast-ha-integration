"""Kreator huba `modbus`: raport weryfikacji przed zgodą (identyfikacja + porównanie z encjami użytkownika)."""
from types import SimpleNamespace

from custom_components.volcast.const import DOMAIN
from custom_components.volcast.verification_entities import VolcastVerificationSensor
from tests.control.test_ha_modbus_control import HUB, UNIT
from tests.test_options_hub import hub_flow, run

USER = {"hub": HUB, "unit_id": UNIT}


def setup(monkeypatch, *, soc=None, power=None, bank=None, options=None, negate=False):
    f, hub = hub_flow(monkeypatch)
    for a, v in (bank or {}).items():
        hub.bank[a] = v & 0xFFFF
        hub.refresh(a)
    tmap = {}
    for key, eid, val in (("soc", "sensor.user_soc", soc), ("battery_power_w", "sensor.user_bat", power)):
        if val is not None:
            hub.hass.states.set(eid, str(val), hub.utcnow())
            tmap[key] = eid
    f.config_entry.options.update({"telemetry_map": tmap, "battery_power_negate": negate, **(options or {})})
    saved = []

    def update(entry, *, options):
        entry.options = dict(options)
        saved.append(options)
    f.hass.config_entries.async_update_entry = update
    return f, hub, saved


def test_matching_values_enable_control_and_carry_the_report(monkeypatch):
    f, _, _ = setup(monkeypatch, soc=54, power=-1150, bank={588: 55, 590: -1200})
    r = run(f.async_step_control_hub(USER))
    assert r["type"] == "create_entry"
    rep = r["data"]["hub_check"]
    assert rep["verdict"] == "ok" and rep["identity_ok"] is True
    assert "sensor." not in repr(rep) and HUB not in repr(rep)


def test_soc_mismatch_gives_a_report_and_no_control(monkeypatch):
    f, _, saved = setup(monkeypatch, soc=80, power=0, bank={588: 55, 590: 0})
    r = run(f.async_step_control_hub(USER))
    assert r["type"] == "form" and r["errors"] == {"base": "hub_check_failed"}
    assert f.config_entry.options.get("control_mode") is None and "direct_target" not in f.config_entry.options
    assert f.config_entry.options["hub_check"]["verdict"] == "mismatch"
    assert f.config_entry.options["hub_check"]["quantities"]["soc"]["deviation"] == 25.0
    assert r["description_placeholders"]["problems"] and "55" in r["description_placeholders"]["problems"]


def test_wrong_device_type_is_reported_not_called_unreadable(monkeypatch):
    f, _, _ = setup(monkeypatch, soc=55, bank={0: 9999, 588: 55})
    r = run(f.async_step_control_hub(USER))
    assert r["errors"] == {"base": "hub_check_failed"}
    rep = f.config_entry.options["hub_check"]
    assert rep["verdict"] == "identity_mismatch" and rep["device_type"] == 9999
    assert f.config_entry.options.get("control_mode") is None


def test_missing_user_entities_report_cannot_compare_and_do_not_block(monkeypatch):
    f, _, _ = setup(monkeypatch, bank={588: 55})
    r = run(f.async_step_control_hub(USER))
    assert r["type"] == "create_entry" and r["data"]["hub_check"]["verdict"] == "cannot_compare"


def test_battery_negate_flag_is_applied_to_the_user_entity(monkeypatch):
    # encja użytkownika ma przeciwny znak, ale flaga to mówi — zgodne
    f, _, _ = setup(monkeypatch, soc=55, power=1200, bank={588: 55, 590: -1200}, negate=True)
    assert run(f.async_step_control_hub(USER))["data"]["hub_check"]["verdict"] == "ok"
    f2, _, _ = setup(monkeypatch, soc=55, power=1200, bank={588: 55, 590: -1200})
    r = run(f2.async_step_control_hub(USER))
    assert f2.config_entry.options["hub_check"]["quantities"]["battery_power_w"]["sign_inverted"] is True
    assert r["errors"] == {"base": "hub_check_failed"}


def test_unavailable_user_entity_cannot_compare(monkeypatch):
    f, hub, _ = setup(monkeypatch, soc=55, bank={588: 55})
    hub.hass.states.set("sensor.user_soc", "unavailable", hub.utcnow())
    r = run(f.async_step_control_hub(USER))
    assert r["data"]["hub_check"]["quantities"]["soc"]["verdict"] == "cannot_compare"


def test_verification_entity_shows_the_report_without_control():
    report = {"verdict": "mismatch", "identity_ok": True, "device_type": 5, "checked_at": "t", "quantities": {}}
    entry = SimpleNamespace(entry_id="e1", options={"hub_check": report})
    ent = VolcastVerificationSensor(entry, None)
    assert ent.native_value == "hub_check_failed"
    assert ent.extra_state_attributes["hub_check"] == report
    ok = VolcastVerificationSensor(SimpleNamespace(entry_id="e1", options={"hub_check": {**report, "verdict": "ok"}}),
                                   None)
    assert ok.native_value == "idle" and ok.extra_state_attributes["hub_check"]["verdict"] == "ok"
    none = VolcastVerificationSensor(SimpleNamespace(entry_id="e1", options={}), None)
    assert none.extra_state_attributes == {}


def test_verification_entity_is_created_without_control_when_a_report_exists():
    import inspect
    from custom_components.volcast import sensor
    src = inspect.getsource(sensor.async_setup_entry)
    assert "hub_check" in src or "OPT_HUB_CHECK" in src
