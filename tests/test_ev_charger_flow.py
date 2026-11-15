"""Krok opcji „EV charger”: ładowarka tylko do odczytu (monitor) bez encji sterujących."""
import asyncio
from types import SimpleNamespace

from custom_components.volcast.core.discovery.models import ChargerFinding, ChargerRole

from tests.test_options_flow import _States, flow  # noqa: F401 — atrapy flow

STATUS_OPTS = {"options": ["not_connected", "connected", "charging"]}


def _finding(control_class="monitor", roles=None):
    roles = roles or {"status": ChargerRole("sensor.wb_status", "sensor"),
                      "power": ChargerRole("sensor.wb_power", "sensor", unit="W")}
    return ChargerFinding(device_id="dev1", name="Wallbox", manufacturer=None, model=None,
                          config_entry_id="e9", platform="p", roles=roles, control_class=control_class)


def _flow(finding, options=None):
    f = flow(options=options or {})
    f.hass.states = _States({"sensor.wb_status": STATUS_OPTS, "sensor.wb_power": {}, "sensor.wb_energy": {}})
    f.hass.data["volcast"]["e1"]["discovery"] = SimpleNamespace(
        classification=SimpleNamespace(chargers=[finding]))
    forms = []
    f.async_show_form = lambda *, step_id, data_schema=None, errors=None, **_: (
        forms.append(data_schema) or {"type": "form", "step_id": step_id, "errors": errors or {}})
    saved = {}

    async def finish(options, **_):
        saved.update(options)
        return {"type": "create_entry"}
    f._finish = finish
    return f, forms, saved


def _field_names(schema):
    return {str(k) for k in schema.schema}


def test_monitor_form_has_no_control_fields():
    f, forms, _ = _flow(_finding())
    asyncio.run(f.async_step_ev_charger({"chargers": ["dev1"]}))
    assert _field_names(forms[-1]) == {"status", "power", "energy"}


def test_regulated_form_keeps_control_fields():
    roles = {"status": ChargerRole("sensor.wb_status", "sensor"),
             "setpoint": ChargerRole("number.wb_cur", "number", unit="A", min=6, max=16)}
    f, forms, _ = _flow(_finding("regulated", roles))
    asyncio.run(f.async_step_ev_charger({"chargers": ["dev1"]}))
    assert {"status", "setpoint", "start_stop", "power", "energy"} <= _field_names(forms[-1])


def test_monitor_charger_is_stored_with_class_and_without_control_roles():
    f, _, saved = _flow(_finding())
    asyncio.run(f.async_step_ev_charger({"chargers": ["dev1"]}))
    r = asyncio.run(f.async_step_ev_charger_roles({
        "status": "sensor.wb_status", "power": "sensor.wb_power", "energy": "sensor.wb_energy",
        # nawet gdyby klient przysłał encję sterującą (np. z falownika), nie trafia do zapisu
        "setpoint": "number.inverters_1_max_charge_current", "start_stop": "switch.inverters_1_start_stop"}))
    assert r["type"] == "create_entry"
    assert saved["ev_chargers"] == [{
        "device_id": "dev1", "label": "Wallbox", "control_class": "monitor",
        "roles": {"status": "sensor.wb_status", "power": "sensor.wb_power", "energy": "sensor.wb_energy"}}]


def test_monitor_without_power_or_energy_is_rejected():
    f, _, saved = _flow(_finding())
    asyncio.run(f.async_step_ev_charger({"chargers": ["dev1"]}))
    r = asyncio.run(f.async_step_ev_charger_roles({"status": "sensor.wb_status"}))
    assert r["errors"] == {"base": "monitor_without_measurement"} and saved == {}


def test_regulated_charger_is_stored_without_class_key():
    roles = {"status": ChargerRole("sensor.wb_status", "sensor"),
             "setpoint": ChargerRole("number.wb_cur", "number", unit="A", min=6, max=16)}
    f, _, saved = _flow(_finding("regulated", roles))
    asyncio.run(f.async_step_ev_charger({"chargers": ["dev1"]}))
    asyncio.run(f.async_step_ev_charger_roles({"status": "sensor.wb_status", "setpoint": "number.wb_cur"}))
    assert saved["ev_chargers"] == [{"device_id": "dev1", "label": "Wallbox",
                                     "roles": {"status": "sensor.wb_status", "setpoint": "number.wb_cur"}}]


def test_saved_regulated_charger_redetected_as_monitor_loses_control_roles():
    saved = [{"device_id": "dev1", "label": "Wallbox",
              "roles": {"status": "sensor.wb_status", "setpoint": "number.wb_cur",
                        "start": "button.wb_start", "stop": "button.wb_stop"}}]
    f, _, out = _flow(_finding(), options={"ev_chargers": saved})
    asyncio.run(f.async_step_ev_charger({"chargers": ["dev1"]}))
    asyncio.run(f.async_step_ev_charger_roles({"status": "sensor.wb_status", "power": "sensor.wb_power"}))
    assert out["ev_chargers"] == [{"device_id": "dev1", "label": "Wallbox", "control_class": "monitor",
                                   "roles": {"status": "sensor.wb_status", "power": "sensor.wb_power"}}]


def test_regulated_charger_keeps_saved_roles_outside_the_form():
    roles = {"status": ChargerRole("sensor.wb_status", "sensor"),
             "setpoint": ChargerRole("number.wb_cur", "number", unit="A", min=6, max=16)}
    saved = [{"device_id": "dev1", "label": "Wallbox",
              "roles": {"status": "sensor.wb_status", "setpoint": "number.wb_cur",
                        "start": "button.wb_start", "stop": "button.wb_stop"}}]
    f, _, out = _flow(_finding("regulated", roles), options={"ev_chargers": saved})
    asyncio.run(f.async_step_ev_charger({"chargers": ["dev1"]}))
    asyncio.run(f.async_step_ev_charger_roles({"status": "sensor.wb_status", "setpoint": "number.wb_cur"}))
    assert out["ev_chargers"][0]["roles"] == saved[0]["roles"]
    assert "control_class" not in out["ev_chargers"][0]


def test_saved_monitor_charger_not_in_findings_is_kept_unchanged():
    saved = [{"device_id": "gone", "label": "Old", "control_class": "monitor",
              "roles": {"status": "sensor.old_status", "energy": "sensor.old_energy"}}]
    f, _, out = _flow(_finding(), options={"ev_chargers": saved})
    asyncio.run(f.async_step_ev_charger({"chargers": ["gone"]}))
    assert out["ev_chargers"] == saved
