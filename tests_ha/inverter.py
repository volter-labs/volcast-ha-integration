"""Prawdziwe encje HA (select/number/switch/sensor) jako integracja falownika `goodwe`.

Usługi idą przez prawdziwe komponenty domen, więc kontekst wywołania trafia do encji
tak jak w HA (`entity.async_set_context`), a stan zapisuje `async_write_ha_state`.
"""
from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockModule, MockPlatform, mock_integration, mock_platform

from homeassistant.components.number import NumberEntity
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from .conftest import GOODWE, SN


class InverterSelect(SelectEntity):
    def __init__(self, uid: str, obj: str, state: str, options: list[str]) -> None:
        self._attr_unique_id = uid
        self._attr_name = obj
        self._attr_options = options
        self._attr_current_option = state
        self.calls = 0

    async def async_select_option(self, option: str) -> None:
        self.calls += 1
        self._attr_current_option = option
        self.async_write_ha_state()


class InverterNumber(NumberEntity):
    def __init__(self, uid: str, obj: str, state: str, attrs: dict) -> None:
        self._attr_unique_id = uid
        self._attr_name = obj
        self._attr_native_value = float(state)
        self._attr_native_min_value = attrs["min"]
        self._attr_native_max_value = attrs["max"]
        self._attr_native_step = attrs["step"]
        self._attr_native_unit_of_measurement = attrs["unit_of_measurement"]

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()


class InverterSwitch(SwitchEntity):
    def __init__(self, uid: str, obj: str, on: bool) -> None:
        self._attr_unique_id = uid
        self._attr_name = obj
        self._attr_is_on = on

    async def async_turn_on(self, **kwargs) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()


class InverterSensor(SensorEntity):
    def __init__(self, uid: str, obj: str, state: str, unit: str) -> None:
        self._attr_unique_id = uid
        self._attr_name = obj
        self._attr_native_value = float(state)
        self._attr_native_unit_of_measurement = unit


async def async_setup_inverter(hass: HomeAssistant) -> dict[str, object]:
    """Encje falownika na prawdziwych platformach; zwraca klucz → encja (obiekt)."""
    mock_integration(hass, MockModule("goodwe"))
    ents: dict[str, list] = {"select": [], "number": [], "switch": [], "sensor": []}
    by_key: dict[str, object] = {}
    for key, (domain, uid, obj, state, attrs) in GOODWE.items():
        unique = f"goodwe-{uid}-{SN}"
        if domain == "select":
            ent = InverterSelect(unique, obj, state, attrs["options"])
        elif domain == "number":
            ent = InverterNumber(unique, obj, state, attrs)
        else:
            ent = InverterSensor(unique, obj, state, attrs["unit_of_measurement"])
        ents[domain].append(ent)
        by_key[key] = ent
    sw = InverterSwitch(f"grid_export_limit_switch-{SN}", "goodwe_grid_export_limit_switch", True)
    ents["switch"].append(sw)
    by_key["export_limit_enabled"] = sw
    for domain, entities in ents.items():
        async def _setup(hass, config, add, discovery_info=None, _e=entities):
            add(_e)
        mock_platform(hass, f"goodwe.{domain}", MockPlatform(async_setup_platform=_setup))
        assert await async_setup_component(hass, domain, {domain: [{"platform": "goodwe"}]})
    await hass.async_block_till_done()
    return by_key
