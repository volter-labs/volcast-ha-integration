"""Sensory trybu bezpośredniego: odczyty rejestrów falownika jako encje HA.

Encje tylko wtedy, gdy wpis ma połączenie bezpośrednie (tryb bezpośredni albo próba), i tylko
dla kluczy mapy `read` profilu, które zna tabela `SENSOR_SPECS` (nieznane i `serial` pomijane).
Wartość znika (encja niedostępna), gdy odczyt jest starszy niż 3× okres odpytywania albo go nie
ma (także przy niezgodnej tożsamości urządzenia). Jakość łącza (odsetek ramek bez odpowiedzi)
jest dostępna, dopóki połączenie istnieje. Nastawy i jakość łącza to encje diagnostyczne.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory

from ..const import DOMAIN

STALE_FACTOR = 3.0
_POWER, _ENERGY = "power", "energy"
_MEAS, _TOTAL = "measurement", "total_increasing"


@dataclass(frozen=True)
class SensorSpec:
    unit: str | None
    device_class: str | None
    state_class: str | None
    diagnostic: bool = False
    icon: str | None = None


SENSOR_SPECS: dict[str, SensorSpec] = {
    "soc": SensorSpec("%", "battery", _MEAS),
    "battery_temp_c": SensorSpec("°C", "temperature", _MEAS),
    "battery_voltage_v": SensorSpec("V", "voltage", _MEAS),
    "battery_current_a": SensorSpec("A", "current", _MEAS),
    "battery_power_w": SensorSpec("W", _POWER, _MEAS),
    "pv_power_w": SensorSpec("W", _POWER, _MEAS),
    "active_power_w": SensorSpec("W", _POWER, _MEAS),
    "load_power_w": SensorSpec("W", _POWER, _MEAS),
    "grid_power_w": SensorSpec("W", _POWER, _MEAS),
    "pv_energy_total_kwh": SensorSpec("kWh", _ENERGY, _TOTAL),
    "grid_import_total_kwh": SensorSpec("kWh", _ENERGY, _TOTAL),
    "grid_export_total_kwh": SensorSpec("kWh", _ENERGY, _TOTAL),
    "mode": SensorSpec(None, "enum", None, icon="mdi:battery-sync"),
    "power_w": SensorSpec("W", _POWER, _MEAS, diagnostic=True),
    "export_limit_w": SensorSpec("W", _POWER, _MEAS, diagnostic=True),
    "soc_min": SensorSpec("%", None, _MEAS, diagnostic=True, icon="mdi:battery-arrow-down"),
    "link_quality": SensorSpec("%", None, _MEAS, diagnostic=True, icon="mdi:lan-disconnect"),
}
# klucz mapy `read`, z którego pochodzi sensor (tryb: wartość rejestru → nazwa z profilu)
_SOURCE = {"mode": "mode_value"}


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


class DirectSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry, conn, key: str) -> None:
        self._entry, self._conn, self.key = entry, conn, key
        spec = SENSOR_SPECS[key]
        self._spec = spec
        self._attr_unique_id = f"{entry.entry_id}_control_direct_{key}"
        self._attr_translation_key = f"control_direct_{key}"
        self._attr_native_unit_of_measurement = spec.unit
        if spec.device_class is not None:
            self._attr_device_class = SensorDeviceClass(spec.device_class)
        if spec.state_class is not None:
            self._attr_state_class = SensorStateClass(spec.state_class)
        if spec.diagnostic:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        if spec.icon:
            self._attr_icon = spec.icon
        if key == "mode":
            self._attr_options = list(conn.profile.modes)
        self._attr_device_info = {"identifiers": {(DOMAIN, f"{entry.entry_id}_control")},
                                  "name": "Volcast control", "manufacturer": "Volcast", "model": "EMS"}

    @property
    def unique_id(self) -> str:
        return self._attr_unique_id

    @property
    def options(self) -> list[str] | None:
        return getattr(self, "_attr_options", None)

    def _fresh(self):
        r = self._conn.reading
        if r is None or not self._conn.age_s() < STALE_FACTOR * float(self._conn.poll_s):
            return None
        return r

    @property
    def available(self) -> bool:
        if self.key == "link_quality":
            return self._conn.refused() is None
        return self._fresh() is not None

    @property
    def native_value(self) -> float | str | None:
        if self.key == "link_quality":
            stats = self._conn.stats
            return round(100.0 * stats.timeouts / stats.requests, 1) if stats.requests > 0 else None
        r = self._fresh()
        if r is None:
            return None
        if self.key == "mode":
            mode = r.device.get("mode")
            return mode if isinstance(mode, str) and mode in self._conn.profile.modes else None
        return _num(r.values.get(self.key))

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._conn.add_listener(self.async_write_ha_state))


def direct_sensors(entry, conn) -> list[DirectSensor]:
    """Encje dla kluczy mapy `read` profilu połączenia (+ jakość łącza); nigdy `serial`."""
    read = conn.profile.raw.get("read") or {}
    keys = [k for k in SENSOR_SPECS if k != "link_quality" and _SOURCE.get(k, k) in read and "serial" not in k]
    return [DirectSensor(entry, conn, k) for k in (*keys, "link_quality")]
