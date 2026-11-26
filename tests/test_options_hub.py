"""Opcje: sterowanie przez hub `modbus` HA — krok wyboru huba, adresu urządzenia i walidacji mapy czujników."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tests.test_options_direct import DEYE, GW_V, flow  # noqa: I001 — najpierw atrapy HA
from custom_components.volcast import config_flow as cf_mod
from custom_components.volcast.core.discovery.classify import is_inverter
from custom_components.volcast.core.modbus.identity import device_fingerprint, identity_info
from custom_components.volcast.core.registers import RegisterImage
from custom_components.volcast.core.transports.ha_modbus_map import required_addresses
from tests.control.test_ha_modbus_control import HUB, UNIT, FakeHub, hub_words

SALT = bytes(range(16))
ROOT = Path(__file__).parent.parent


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def advance(self, s: float) -> None:
        self.now += s


def hub_flow(monkeypatch, *, drop=(), hubs=None):
    f = flow(profiles=(GW_V, DEYE), monkeypatch=monkeypatch)
    f.async_show_form = lambda **kw: {"type": "form", "errors": kw.pop("errors", None) or {}, **kw}
    hub = FakeHub(f.hass, _Clock(), hub_words())
    for a in drop:
        hub.remove_sensor(a)
    if hubs is not None:
        f.hass.data["modbus"] = {h: object() for h in hubs}

    async def salt(_hass):
        return SALT
    monkeypatch.setattr(cf_mod, "async_installation_salt", salt)
    return f, hub


def run(coro):
    return asyncio.run(coro)


def test_menu_offers_the_hub_path_but_discovery_never_calls_modbus_an_inverter():
    f = flow()
    assert "control_hub" not in run(f.async_step_control())["menu_options"]      # bez integracji `modbus`
    f.hass.config.components = {"modbus"}
    assert run(f.async_step_control())["menu_options"] == ["control_entities", "control_direct", "control_hub",
                                                           "control_off"]
    hint = SimpleNamespace(domain="modbus", manufacturer=None, model=None)
    assert is_inverter(hint, []) is False


def test_hub_list_comes_from_the_modbus_integration(monkeypatch):
    f, _ = hub_flow(monkeypatch, hubs=["deye_hub", "other"])
    r = run(f.async_step_control_hub())
    assert r["step_id"] == "control_hub"
    schema_keys = {str(k): v for k, v in r["data_schema"].schema.items()}
    assert schema_keys["hub"].container == ["deye_hub", "other"]
    assert r["description_placeholders"]["yaml_path"] == (
        "https://github.com/volter-labs/volcast-ha-integration/blob/main/docs/profiles/deye-sg-ha-modbus.yaml")


def test_hub_falls_back_to_text_without_the_modbus_integration(monkeypatch):
    f, _ = hub_flow(monkeypatch)
    r = run(f.async_step_control_hub())
    schema_keys = {str(k): v for k, v in r["data_schema"].schema.items()}
    assert schema_keys["hub"] is str


@pytest.mark.parametrize("user,field", [
    ({"hub": "", "unit_id": 1}, "hub"), ({"hub": " deye_hub", "unit_id": 1}, "hub"),
    ({"hub": HUB, "unit_id": 0}, "unit_id"), ({"hub": HUB, "unit_id": 248}, "unit_id"),
    ({"hub": HUB, "unit_id": "1"}, "unit_id"), ({"hub": HUB, "unit_id": True}, "unit_id")])
def test_invalid_hub_or_device_address_is_a_form_error(monkeypatch, user, field):
    f, _ = hub_flow(monkeypatch)
    r = run(f.async_step_control_hub(user))
    assert r["type"] == "form" and field in r["errors"]


def test_incomplete_map_shows_problems_and_does_not_enable_control(monkeypatch):
    f, hub = hub_flow(monkeypatch, drop=(146, 147, 148, 20))
    hub.hass.data["entity_registry"].entities["sensor.plain"] = SimpleNamespace(
        entity_id="sensor.plain", platform="modbus", unique_id="other")
    r = run(f.async_step_control_hub({"hub": HUB, "unit_id": UNIT}))
    assert r["type"] == "form" and r["errors"] == {"base": "hub_map_incomplete"}
    problems = r["description_placeholders"]["problems"]
    assert "20" in problems and "146-148" in problems
    assert f.config_entry.options.get("control_mode") is None


def test_unsupported_sensor_is_reported_with_its_cause(monkeypatch):
    f, hub = hub_flow(monkeypatch)
    hub.sensor(146)._scale = 10                      # nie surowe słowo
    r = run(f.async_step_control_hub({"hub": HUB, "unit_id": UNIT}))
    assert r["errors"] == {"base": "hub_map_incomplete"}
    assert "sensor_not_raw" in r["description_placeholders"]["problems"]


def test_sensor_bound_to_another_device_is_reported(monkeypatch):
    f, _ = hub_flow(monkeypatch)
    r = run(f.async_step_control_hub({"hub": HUB, "unit_id": UNIT + 1}))
    assert r["errors"] == {"base": "hub_map_incomplete"}
    assert "sensor_other_device" in r["description_placeholders"]["problems"]


def test_check_again_after_fixing_the_yaml_proceeds(monkeypatch):
    f, hub = hub_flow(monkeypatch, drop=(150,))
    user = {"hub": HUB, "unit_id": UNIT}
    assert run(f.async_step_control_hub(user))["errors"] == {"base": "hub_map_incomplete"}
    hub.add_sensor(150, "sensor.volcast_reg_150")    # użytkownik poprawił YAML i przeładował Modbus
    r = run(f.async_step_control_hub(user))
    assert r["type"] == "create_entry"


def test_valid_map_enables_direct_control_through_the_hub(monkeypatch):
    f, hub = hub_flow(monkeypatch)
    r = run(f.async_step_control_hub({"hub": HUB, "unit_id": UNIT}))
    assert r["type"] == "create_entry"
    data = r["data"]
    target = data["direct_target"]
    assert data["control_mode"] == "direct"
    assert (target["transport"], target["hub"], target["unit_id"], target["profile_id"]) == (
        "ha_modbus", HUB, UNIT, "deye-sg")
    image = RegisterImage.from_blocks({b[0]: hub.words(range(b[0], b[0] + b[1])) for b in DEYE.modbus.identify_reads})
    assert target["device_fp"] == device_fingerprint(SALT, DEYE, image, reject_placeholder=True)
    assert target["rated_power_w"] == identity_info(DEYE, image)["rated_power_w"] > 0
    assert hub.writes == []                          # kreator niczego nie zapisuje do falownika


def test_placeholder_serial_is_not_a_new_identity(monkeypatch):
    f, hub = hub_flow(monkeypatch)
    for a in range(3, 8):
        hub.bank[a] = 0x3030
        hub.refresh(a)
    r = run(f.async_step_control_hub({"hub": HUB, "unit_id": UNIT}))
    assert r["type"] == "form" and r["errors"] == {"base": "hub_identity_unreadable"}


def test_mapped_but_unavailable_sensor_blocks_enabling(monkeypatch):
    f, hub = hub_flow(monkeypatch)
    hub.hass.states.set(hub.entities[150], "unavailable", hub.utcnow())
    r = run(f.async_step_control_hub({"hub": HUB, "unit_id": UNIT}))
    assert r["errors"] == {"base": "hub_sensors_unavailable"}
    assert "150" in r["description_placeholders"]["problems"]


def test_form_prefills_the_saved_hub_target(monkeypatch):
    f, _ = hub_flow(monkeypatch)
    f.config_entry.options["direct_target"] = {"transport": "ha_modbus", "hub": "saved_hub", "unit_id": 7}
    r = run(f.async_step_control_hub())
    sug = {str(k): k.description["suggested_value"] for k in r["data_schema"].schema}
    assert sug == {"hub": "saved_hub", "unit_id": 7}


def test_yaml_snippet_is_valid_and_covers_the_profile_read_plan():
    doc = yaml.safe_load((ROOT / cf_mod.hub_template_path('deye-sg')).read_text("utf-8"))
    sensors = doc["sensors"]
    assert {s["address"] for s in sensors} == set(required_addresses(DEYE)) and len(sensors) == 51
    for s in sensors:
        assert s["unique_id"] == f"volcast_reg_{s['address']}"
        assert s["data_type"] == "uint16" and s["input_type"] == "holding"
        assert not {"scale", "offset", "precision", "swap"} & set(s)
        assert s["slave"] and s["scan_interval"] > 0
    assert len({s["name"] for s in sensors}) == 51


def test_compact_ranges():
    assert cf_mod.compact_ranges([5, 3, 4, 0, 9, 9]) == "0, 3-5, 9"
    assert cf_mod.compact_ranges([]) == ""
