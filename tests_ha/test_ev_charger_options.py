"""Krok opcji „Ładowarka EV”: potwierdzenie znalezisk z wykrywania na prawdziwym menedżerze HA."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

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
    _provide(hass, entry, [_finding("dev1", "Wallbox"), _finding("dev2", "Garage")])
    form = await _open(hass, entry)
    roles = await hass.config_entries.options.async_configure(form["flow_id"], {"chargers": ["dev2"]})
    await hass.config_entries.options.async_configure(roles["flow_id"], {"status": "sensor.wb_status"})
    assert [c["device_id"] for c in entry.options["ev_chargers"]] == ["dev2"]


async def test_two_selected_chargers_go_through_roles_one_by_one(hass: HomeAssistant, network_down):
    entry = make_entry(hass)
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


def test_translations_cover_every_locale():
    strings = json.loads((COMPONENT / "strings.json").read_text("utf-8"))["options"]
    files = sorted((COMPONENT / "translations").glob("*.json"))
    assert len(files) >= 13
    for path in [COMPONENT / "strings.json", *files]:
        opts = json.loads(path.read_text("utf-8"))["options"]
        assert opts["step"]["init"]["menu_options"]["ev_charger"], path.name
        for step in ("ev_charger", "ev_charger_roles"):
            assert opts["step"][step]["title"], (path.name, step)
        assert set(opts["step"]["ev_charger_roles"]["data"]) == set(
            strings["step"]["ev_charger_roles"]["data"]), path.name
        assert opts["step"]["ev_charger"]["data"]["chargers"], path.name
        assert opts["abort"]["no_ev_chargers"], path.name
