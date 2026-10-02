"""Krok opcji „Ładowarka EV”: potwierdzenie znalezisk z wykrywania na prawdziwym menedżerze HA."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData

from custom_components.volcast.const import DOMAIN
from custom_components.volcast.core.discovery import ChargerFinding, ChargerRole, Classification

from .conftest import make_entry

COMPONENT = Path(__file__).parents[1] / "custom_components" / "volcast"


def _finding(device_id="dev1", name="Wallbox", **roles) -> ChargerFinding:
    base = {
        "status": ChargerRole("sensor.wb_status", "sensor", options=("charging", "connected")),
        "setpoint": ChargerRole("number.wb_current", "number", unit="A", min=6, max=16, step=1),
        "power": ChargerRole("sensor.wb_power", "sensor", unit="W"),
        "energy": ChargerRole("sensor.wb_energy", "sensor", unit="kWh"),
    }
    base.update(roles)
    return ChargerFinding(device_id=device_id, name=name, manufacturer="Acme", model="W1",
                          config_entry_id="ce1", platform="acme", roles=base, confidence="high")


def _status(hass: HomeAssistant, *entity_ids: str) -> None:
    """Encje statusu z listą stanów (options) — jedyny dopuszczalny kształt statusu."""
    for eid in entity_ids:
        hass.states.async_set(eid, "charging", {"options": ["available", "charging"]})


def _provide(hass: HomeAssistant, entry, chargers) -> None:
    """Ostatni wynik klasyfikacji, jak zostawia go runner wykrywania."""
    cls = None if chargers is None else Classification([], [], [], chargers=chargers)
    hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})["discovery"] = SimpleNamespace(
        classification=cls)


async def _open(hass, entry):
    menu = await hass.config_entries.options.async_init(entry.entry_id)
    assert "ev_charger" in menu["menu_options"]
    return await hass.config_entries.options.async_configure(menu["flow_id"], {"next_step_id": "ev_charger"})


def _defaults(form) -> dict:
    """Wartości wstępne pól: `default` (wybór ładowarek) albo `suggested_value` (pola ról, dają się wyczyścić)."""
    out = {}
    for k in form["data_schema"].schema:
        value = vol.UNDEFINED if k.default is vol.UNDEFINED else k.default()
        if value is vol.UNDEFINED:
            value = (k.description or {}).get("suggested_value")
        if value is not None:
            out[str(k)] = value
    return out


async def test_no_discovery_result_aborts(hass: HomeAssistant, network_down):
    entry = make_entry(hass)
    _provide(hass, entry, None)
    res = await _open(hass, entry)
    assert res["type"] is FlowResultType.ABORT and res["reason"] == "no_ev_chargers"


async def test_no_chargers_found_aborts(hass: HomeAssistant, network_down):
    entry = make_entry(hass)
    _provide(hass, entry, [])
    res = await _open(hass, entry)
    assert res["type"] is FlowResultType.ABORT and res["reason"] == "no_ev_chargers"


async def test_confirm_charger_saves_only_id_roles_label(hass: HomeAssistant, network_down):
    entry = make_entry(hass, options={"update_interval": 30})
    _status(hass, "sensor.wb_status")
    _provide(hass, entry, [_finding()])
    form = await _open(hass, entry)
    assert form["type"] is FlowResultType.FORM and form["step_id"] == "ev_charger"
    roles = await hass.config_entries.options.async_configure(form["flow_id"], {"chargers": ["dev1"]})
    assert roles["type"] is FlowResultType.FORM and roles["step_id"] == "ev_charger_roles"
    defaults = _defaults(roles)
    assert defaults["status"] == "sensor.wb_status" and defaults["setpoint"] == "number.wb_current"
    done = await hass.config_entries.options.async_configure(roles["flow_id"], defaults)
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["ev_chargers"] == [{
        "device_id": "dev1", "label": "Wallbox",
        "roles": {"status": "sensor.wb_status", "setpoint": "number.wb_current",
                  "power": "sensor.wb_power", "energy": "sensor.wb_energy"}}]
    assert entry.options["update_interval"] == 30


async def test_role_can_be_corrected_and_optional_role_dropped(hass: HomeAssistant, network_down):
    entry = make_entry(hass)
    _status(hass, "sensor.other_status")
    _provide(hass, entry, [_finding()])
    form = await _open(hass, entry)
    roles = await hass.config_entries.options.async_configure(form["flow_id"], {"chargers": ["dev1"]})
    done = await hass.config_entries.options.async_configure(roles["flow_id"], {
        "status": "sensor.other_status", "setpoint": "number.wb_current", "energy": "sensor.wb_energy"})
    (saved,) = entry.options["ev_chargers"]
    assert saved["roles"] == {"status": "sensor.other_status", "setpoint": "number.wb_current",
                              "energy": "sensor.wb_energy"}
    assert done["type"] is FlowResultType.CREATE_ENTRY


async def test_only_selected_charger_is_saved(hass: HomeAssistant, network_down):
    entry = make_entry(hass)
    _status(hass, "sensor.wb_status")
    _provide(hass, entry, [_finding("dev1", "Wallbox"), _finding("dev2", "Garage")])
    form = await _open(hass, entry)
    roles = await hass.config_entries.options.async_configure(form["flow_id"], {"chargers": ["dev2"]})
    await hass.config_entries.options.async_configure(roles["flow_id"], {"status": "sensor.wb_status"})
    assert [c["device_id"] for c in entry.options["ev_chargers"]] == ["dev2"]


async def test_two_selected_chargers_go_through_roles_one_by_one(hass: HomeAssistant, network_down):
    entry = make_entry(hass)
    _status(hass, "sensor.a", "sensor.b")
    _provide(hass, entry, [_finding("dev1", "Wallbox"), _finding("dev2", "Garage")])
    form = await _open(hass, entry)
    r1 = await hass.config_entries.options.async_configure(form["flow_id"], {"chargers": ["dev1", "dev2"]})
    assert r1["step_id"] == "ev_charger_roles"
    r2 = await hass.config_entries.options.async_configure(r1["flow_id"], {"status": "sensor.a"})
    assert r2["type"] is FlowResultType.FORM and r2["step_id"] == "ev_charger_roles"
    done = await hass.config_entries.options.async_configure(r2["flow_id"], {"status": "sensor.b"})
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert [(c["device_id"], c["roles"]["status"]) for c in entry.options["ev_chargers"]] == [
        ("dev1", "sensor.a"), ("dev2", "sensor.b")]


async def test_deselecting_all_removes_key_and_keeps_other_options(hass: HomeAssistant, network_down):
    saved = [{"device_id": "dev1", "label": "Wallbox", "roles": {"status": "sensor.wb_status"}}]
    entry = make_entry(hass, options={"ev_chargers": saved, "update_interval": 30})
    _provide(hass, entry, [_finding()])
    form = await _open(hass, entry)
    done = await hass.config_entries.options.async_configure(form["flow_id"], {"chargers": []})
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert "ev_chargers" not in entry.options and entry.options["update_interval"] == 30


async def test_saved_charger_is_preselected_with_saved_roles(hass: HomeAssistant, network_down):
    saved = [{"device_id": "dev1", "label": "Wallbox", "roles": {"status": "sensor.mine"}}]
    entry = make_entry(hass, options={"ev_chargers": saved})
    _provide(hass, entry, [_finding()])
    form = await _open(hass, entry)
    assert _defaults(form) == {"chargers": ["dev1"]}
    roles = await hass.config_entries.options.async_configure(form["flow_id"], {"chargers": ["dev1"]})
    assert _defaults(roles) == {"status": "sensor.mine"}


async def test_saved_charger_missing_from_discovery_can_be_kept(hass: HomeAssistant, network_down):
    saved = [{"device_id": "gone", "label": "Old", "roles": {"status": "sensor.old"}}]
    entry = make_entry(hass, options={"ev_chargers": saved})
    _provide(hass, entry, [_finding()])
    form = await _open(hass, entry)
    done = await hass.config_entries.options.async_configure(form["flow_id"], {"chargers": ["gone"]})
    assert done["type"] is FlowResultType.CREATE_ENTRY and entry.options["ev_chargers"] == saved


async def _roles_form(hass):
    entry = make_entry(hass)
    _provide(hass, entry, [_finding()])
    form = await _open(hass, entry)
    return entry, await hass.config_entries.options.async_configure(form["flow_id"], {"chargers": ["dev1"]})


async def test_binary_sensor_rejected_as_status(hass: HomeAssistant, network_down):
    # on/off nie niesie stanów ładowarki — pole statusu przyjmuje tylko sensor/select
    hass.states.async_set("binary_sensor.wb_plug", "on", {"device_class": "plug"})
    entry, roles = await _roles_form(hass)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            roles["flow_id"], {"status": "binary_sensor.wb_plug"})
    assert "ev_chargers" not in entry.options


@pytest.mark.parametrize("attrs", [{}, {"options": []}])
async def test_status_without_options_shows_error(hass: HomeAssistant, network_down, attrs):
    hass.states.async_set("sensor.wb_text", "Charging", attrs)
    entry, roles = await _roles_form(hass)
    res = await hass.config_entries.options.async_configure(roles["flow_id"], {"status": "sensor.wb_text"})
    assert res["type"] is FlowResultType.FORM and res["step_id"] == "ev_charger_roles"
    assert res["errors"] == {"status": "status_without_options"}
    assert "ev_chargers" not in entry.options


async def test_missing_status_entity_shows_error(hass: HomeAssistant, network_down):
    entry, roles = await _roles_form(hass)
    res = await hass.config_entries.options.async_configure(roles["flow_id"], {"status": "sensor.nope"})
    assert res["type"] is FlowResultType.FORM and res["errors"] == {"status": "entity_not_found"}
    assert "ev_chargers" not in entry.options


async def test_select_with_options_accepted_as_status(hass: HomeAssistant, network_down):
    hass.states.async_set("select.wb_state", "Charging", {"options": ["Idle", "Charging"]})
    entry, roles = await _roles_form(hass)
    done = await hass.config_entries.options.async_configure(roles["flow_id"], {"status": "select.wb_state"})
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["ev_chargers"][0]["roles"] == {"status": "select.wb_state"}


async def test_no_findings_shows_saved_chargers_and_allows_removal(hass: HomeAssistant, network_down):
    saved = [{"device_id": "gone", "label": "Old", "roles": {"status": "sensor.old"}},
             {"device_id": "gone2", "label": "Old2", "roles": {"status": "sensor.old2"}}]
    entry = make_entry(hass, options={"ev_chargers": saved, "update_interval": 30})
    _provide(hass, entry, [])
    form = await _open(hass, entry)
    assert form["type"] is FlowResultType.FORM and form["step_id"] == "ev_charger"
    assert _defaults(form) == {"chargers": ["gone", "gone2"]}
    done = await hass.config_entries.options.async_configure(form["flow_id"], {"chargers": ["gone2"]})
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["ev_chargers"] == [saved[1]] and entry.options["update_interval"] == 30


async def test_no_findings_and_nothing_saved_aborts(hass: HomeAssistant, network_down):
    entry = make_entry(hass)
    _provide(hass, entry, [])
    form = await _open(hass, entry)
    assert form["type"] is FlowResultType.ABORT and form["reason"] == "no_ev_chargers"


async def test_resave_keeps_saved_roles_outside_form_fields(hass: HomeAssistant, network_down):
    _status(hass, "sensor.mine")
    saved = [{"device_id": "dev1", "label": "Wallbox",
              "roles": {"status": "sensor.old", "start": "button.go", "stop": "button.halt"}}]
    entry = make_entry(hass, options={"ev_chargers": saved})
    _provide(hass, entry, [_finding()])
    form = await _open(hass, entry)
    roles = await hass.config_entries.options.async_configure(form["flow_id"], {"chargers": ["dev1"]})
    done = await hass.config_entries.options.async_configure(roles["flow_id"], {"status": "sensor.mine"})
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["ev_chargers"][0]["roles"] == {
        "status": "sensor.mine", "start": "button.go", "stop": "button.halt"}


async def test_kept_chargers_keep_chosen_order_among_discovered(hass: HomeAssistant, network_down):
    _status(hass, "sensor.wb_status", "sensor.wb2_status")
    saved = [{"device_id": "gone", "label": "Old", "roles": {"status": "sensor.old"}}]
    entry = make_entry(hass, options={"ev_chargers": saved})
    _provide(hass, entry, [_finding("dev1"), _finding("dev2", status=ChargerRole(
        "sensor.wb2_status", "sensor", options=("charging", "connected")))])
    form = await _open(hass, entry)
    step = await hass.config_entries.options.async_configure(
        form["flow_id"], {"chargers": ["dev2", "gone", "dev1"]})
    step = await hass.config_entries.options.async_configure(step["flow_id"], {"status": "sensor.wb2_status"})
    done = await hass.config_entries.options.async_configure(step["flow_id"], {"status": "sensor.wb_status"})
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert [c["device_id"] for c in entry.options["ev_chargers"]] == ["dev2", "gone", "dev1"]


def test_translations_cover_every_locale():
    strings = json.loads((COMPONENT / "strings.json").read_text("utf-8"))["options"]
    files = sorted((COMPONENT / "translations").glob("*.json"))
    assert len(files) >= 13
    for path in [COMPONENT / "strings.json", *files]:
        opts = json.loads(path.read_text("utf-8"))["options"]
        assert opts["step"]["init"]["menu_options"]["ev_charger"], path.name
        for step in ("ev_charger", "ev_charger_roles"):
            assert opts["step"][step]["title"], (path.name, step)
        assert opts["error"]["entity_not_found"], path.name
        assert set(opts["step"]["ev_charger_roles"]["data"]) == set(
            strings["step"]["ev_charger_roles"]["data"]), path.name
        assert opts["step"]["ev_charger"]["data"]["chargers"], path.name
        assert opts["abort"]["no_ev_chargers"], path.name
        assert opts["error"]["status_without_options"], path.name
