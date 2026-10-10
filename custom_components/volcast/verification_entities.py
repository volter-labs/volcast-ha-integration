"""Sensor diagnostyczny drabiny weryfikacji urządzenia (`rung n/5` / `verified` / `stopped` / `idle`).

Tylko czyta `ControlRuntime.verification` — niczym nie steruje. Odświeża się na sygnał zmiany stanu
sterowania, który drabina wysyła przy zmianie szczebla, stanu, stopu albo urządzenia.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import SIGNAL_CONTROL_STATE_UPDATED
from .core.control.ladder import IDLE, RUNG_WINDOW, STOPPED, VERIFIED
from .discovery_entities import _device_info

_ATTRS = ("rung", "state", "since", "next_at", "stop_reason", "migrated")


class VolcastVerificationSensor(SensorEntity):
    """Szczebel drabiny weryfikacji i jej atrybuty (stan, od kiedy, następny krok, powód stopu, próba)."""

    _attr_has_entity_name = True
    _attr_translation_key = "verification"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:shield-check-outline"
    _attr_should_poll = False

    def __init__(self, entry, control) -> None:
        self._control = control
        self._entry_id = entry.entry_id
        self._attr_unique_id = f"{entry.entry_id}_control_verification"
        self._attr_device_info = _device_info(entry.entry_id)

    def _payload(self) -> dict[str, Any] | None:
        runner = getattr(self._control, "verification", None)
        return runner.payload() if runner is not None else None

    @property
    def native_value(self) -> str:
        data = self._payload()
        if not data or data.get("state") == IDLE:
            return "idle"
        if data["state"] in (VERIFIED, STOPPED):
            return data["state"]
        return f"rung {data.get('rung')}/{RUNG_WINDOW}"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self._payload() or {}
        out = {k: data[k] for k in _ATTRS if k in data}
        out.update(data.get("trial") or {})
        return out

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(
            self.hass, SIGNAL_CONTROL_STATE_UPDATED.format(entry_id=self._entry_id), self._handle_update))

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()
