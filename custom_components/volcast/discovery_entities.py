"""Encje wykrywania instalacji: sensor diagnostyczny i przycisk „Run discovery".

Obie tylko czytają `DiscoveryRunner` — niczym nie sterują. Sensor odświeża stan
na sygnał dispatchera wysyłany przez runner po każdym przebiegu.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import DOMAIN, SIGNAL_DISCOVERY_UPDATED
from .core.discovery.report import compact_attributes, summarize


def _device_info(entry_id: str) -> dict[str, Any]:
    # Ta sama karta urządzenia co encje prognozy (identyfikator = entry_id).
    return {
        "identifiers": {(DOMAIN, entry_id)},
        "name": "Volcast Solar Forecast",
        "manufacturer": "Volter Labs",
        "model": "PV Forecast",
        "entry_type": "service",
    }


class VolcastDiscoverySensor(SensorEntity):
    """Podsumowanie ostatniego raportu wykrywania (albo „pending")."""

    _attr_has_entity_name = True
    _attr_translation_key = "discovery"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:magnify-scan"
    _attr_should_poll = False
    # Atrybuty raportu nie trafiają do rekordera — to migawka, nie historia.
    _unrecorded_attributes = frozenset({
        "inverters", "price_platforms", "max_history_days", "loggers", "errors",
        "schema", "generated_at", "truncated",
    })

    def __init__(self, runner, entry_id: str) -> None:
        self._runner = runner
        self._entry_id = entry_id
        self._attr_unique_id = f"{entry_id}_discovery"
        self._attr_device_info = _device_info(entry_id)

    @property
    def native_value(self) -> str:
        report = self._runner.report
        return summarize(report) if report else "pending"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        report = self._runner.report
        return compact_attributes(report) if report else {}

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(
            self.hass,
            SIGNAL_DISCOVERY_UPDATED.format(entry_id=self._entry_id),
            self._handle_report_updated,
        ))

    @callback
    def _handle_report_updated(self) -> None:
        self.async_write_ha_state()


class VolcastDiscoveryButton(ButtonEntity):
    """Ręczne uruchomienie wykrywania (tylko odczyt)."""

    _attr_has_entity_name = True
    _attr_translation_key = "run_discovery"
    _attr_icon = "mdi:magnify"

    def __init__(self, runner, entry_id: str) -> None:
        self._runner = runner
        self._attr_unique_id = f"{entry_id}_run_discovery"
        self._attr_device_info = _device_info(entry_id)

    async def async_press(self) -> None:
        await self._runner.async_run()
