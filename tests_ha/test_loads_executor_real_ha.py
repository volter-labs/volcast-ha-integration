"""Wykonawca ładowarki EV na prawdziwych encjach number / select / sensor w HA testowym.

Zapis idzie prawdziwymi usługami domen (`number.set_value`, `select.select_option`), a encje
ładowarki należą w rejestrze do jednego urządzenia (`device_id == load_key`)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry, MockModule, MockPlatform, mock_integration, mock_platform)

from homeassistant.components.number import NumberEntity
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.volcast.control.ha_writer import EntityServiceWriter
from custom_components.volcast.control.loads_executor import LoadsExecutor, charger_roles
from custom_components.volcast.control.loads_store import LoadsStore
from custom_components.volcast.core.ev.model import LoadSlot, LoadsMeta

OPTIONS = ["Start charging", "Stop charging", "No operation"]


class ChargerSelect(SelectEntity):
    _attr_unique_id = "wb-start-stop"
    _attr_name = "wb_start_stop"
    _attr_options = OPTIONS
    _attr_current_option = "No operation"

    async def async_select_option(self, option: str) -> None:
        self._attr_current_option = option
        self.async_write_ha_state()


class ChargerNumber(NumberEntity):
    _attr_unique_id = "wb-current"
    _attr_name = "wb_current"
    _attr_native_value = 6.0
    _attr_native_min_value = 6.0
    _attr_native_max_value = 16.0
    _attr_native_step = 1.0
    _attr_native_unit_of_measurement = "A"

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()


class ChargerStatus(SensorEntity):
    _attr_unique_id = "wb-status"
    _attr_name = "wb_status"
    _attr_native_value = "Available"


async def _setup_charger(hass: HomeAssistant) -> tuple[str, dict]:
    """Encje ładowarki na prawdziwych platformach i jedno urządzenie w rejestrze."""
    mock_integration(hass, MockModule("wallbox_test"))
    for domain, ent in (("select", ChargerSelect()), ("number", ChargerNumber()), ("sensor", ChargerStatus())):
        async def _setup(hass, config, add, discovery_info=None, _e=ent):
            add([_e])
        mock_platform(hass, f"wallbox_test.{domain}", MockPlatform(async_setup_platform=_setup))
        assert await async_setup_component(hass, domain, {domain: [{"platform": "wallbox_test"}]})
    await hass.async_block_till_done()
    cfg = MockConfigEntry(domain="wallbox_test")
    cfg.add_to_hass(hass)
    dev = dr.async_get(hass).async_get_or_create(config_entry_id=cfg.entry_id, identifiers={("wallbox_test", "wb")})
    reg = er.async_get(hass)
    roles = {"status": "sensor.wb_status", "setpoint": "number.wb_current", "start_stop": "select.wb_start_stop"}
    for eid in roles.values():
        assert reg.async_get(eid) is not None, eid
        reg.async_update_entity(eid, device_id=dev.id)
    return dev.id, roles


def _executor(hass: HomeAssistant, key: str, roles: dict) -> LoadsExecutor:
    entry = SimpleNamespace(entry_id="e1", options={"ev_chargers": [
        {"device_id": key, "label": "Wallbox", "roles": roles}]})
    return LoadsExecutor(hass, entry, writer=EntityServiceWriter(hass),
                         roles_provider=lambda k: charger_roles(entry.options, k),
                         store=LoadsStore(hass, "e1"), consent_getter=lambda: True,
                         local_switch_getter=lambda: True, plan_only_getter=lambda: False, readback_s=0)


def _schedule(key: str, mode: str):
    now = datetime.now(timezone.utc)
    slot = LoadSlot(device_id=key, start=now - timedelta(minutes=5), end=now + timedelta(minutes=25),
                    mode=mode, power_w=11100.0, min_power_w=None, session_id="s1", battery_guard="bypass")
    meta = LoadsMeta(session_id="s1", load_key=key, control_enabled=True, phases=3, control_unit="A",
                     min=6.0, max=16.0, step=1.0, max_power_w=11040.0,
                     plan_generated_at=now - timedelta(minutes=1))
    return SimpleNamespace(loads=(slot,), loads_meta=meta)


async def test_forced_slot_writes_setpoint_then_start_on_real_entities(hass: HomeAssistant):
    key, roles = await _setup_charger(hass)
    ex = _executor(hass, key, roles)
    await ex.async_start()
    try:
        await ex.async_on_plan(_schedule(key, "forced"))
        await hass.async_block_till_done()
        assert float(hass.states.get("number.wb_current").state) == 16.0
        assert hass.states.get("select.wb_start_stop").state == "Start charging"
    finally:
        await ex.async_stop()


async def test_off_slot_stops_running_charge_on_real_entities(hass: HomeAssistant):
    key, roles = await _setup_charger(hass)
    hass.states.async_set("sensor.wb_status", "Charging")
    ex = _executor(hass, key, roles)
    await ex.async_start()
    try:
        await ex.async_on_plan(_schedule(key, "off"))
        await hass.async_block_till_done()
        assert hass.states.get("select.wb_start_stop").state == "Stop charging"
        assert float(hass.states.get("number.wb_current").state) == 6.0
    finally:
        await ex.async_stop()


async def test_setpoint_outside_charger_device_means_no_writes(hass: HomeAssistant):
    """Encja nastawy spoza urządzenia ładowarki unieważnia ładowarkę: żaden zapis."""
    key, roles = await _setup_charger(hass)
    other = dr.async_get(hass).async_get_or_create(
        config_entry_id=hass.config_entries.async_entries("wallbox_test")[0].entry_id,
        identifiers={("wallbox_test", "other")})
    er.async_get(hass).async_update_entity("number.wb_current", device_id=other.id)
    ex = _executor(hass, key, roles)
    await ex.async_start()
    try:
        await ex.async_on_plan(_schedule(key, "forced"))
        await hass.async_block_till_done()
        assert float(hass.states.get("number.wb_current").state) == 6.0
        assert hass.states.get("select.wb_start_stop").state == "No operation"
    finally:
        await ex.async_stop()
