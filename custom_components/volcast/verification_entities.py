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

from .const import OPT_HUB_CHECK, SIGNAL_CONTROL_STATE_UPDATED
from .core.control import hub_check
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
        self._entry = entry
        self._entry_id = entry.entry_id
        self._attr_unique_id = f"{entry.entry_id}_control_verification"
        self._attr_device_info = _device_info(entry.entry_id)

    def _payload(self) -> dict[str, Any] | None:
        runner = getattr(self._control, "verification", None)
        return runner.payload() if runner is not None else None

    def _hub_check(self) -> dict[str, Any] | None:
        """Raport weryfikacji huba `modbus` (tryb tylko-odczyt: bez sterowania jest jedyną treścią encji)."""
        report = (getattr(self._entry, "options", None) or {}).get(OPT_HUB_CHECK)
        return report if isinstance(report, dict) and report else None

    @property
    def native_value(self) -> str:
        data = self._payload()
        if not data or data.get("state") == IDLE:
            return "hub_check_failed" if hub_check.blocks_control(self._hub_check()) and not data else "idle"
        if data["state"] in (VERIFIED, STOPPED):
            return data["state"]
        return f"rung {data.get('rung')}/{RUNG_WINDOW}"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self._payload() or {}
        out = {k: data[k] for k in _ATTRS if k in data}
        out.update(data.get("trial") or {})
        report = self._hub_check()
        if report is not None:
            out["hub_check"] = report
        return out

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(
            self.hass, SIGNAL_CONTROL_STATE_UPDATED.format(entry_id=self._entry_id), self._handle_update))

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()
