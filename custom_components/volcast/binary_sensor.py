"""Binary sensor platform for Volcast Solar Forecast."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_PEAK_THRESHOLD, DEFAULT_PEAK_THRESHOLD, DOMAIN
from .coordinator import VolcastCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Volcast binary sensors from a config entry."""
    coordinator: VolcastCoordinator = hass.data[DOMAIN][entry.entry_id]
    peak_threshold = entry.options.get(CONF_PEAK_THRESHOLD, DEFAULT_PEAK_THRESHOLD)

    async_add_entities([VolcastPeakProductionSensor(coordinator, entry, peak_threshold)])


class VolcastPeakProductionSensor(
    CoordinatorEntity[VolcastCoordinator], BinarySensorEntity
):
    """Binary sensor: ON when current power > threshold % of today's peak."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: VolcastCoordinator,
        entry: ConfigEntry,
        threshold_pct: int,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._threshold_pct = threshold_pct
        self.entity_description = BinarySensorEntityDescription(
            key="peak_production",
            translation_key="peak_production",
        )
        self._attr_unique_id = f"{entry.entry_id}_peak_production"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Volcast Solar Forecast",
            "manufacturer": "Volter Labs",
            "model": "PV Forecast",
            "entry_type": "service",
        }

    @property
    def is_on(self) -> bool | None:
        """Return True if current power > threshold of today's peak."""
        data = self.coordinator.data
        if data is None:
            return None

        try:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(self.hass.config.time_zone)
        except Exception:
            from datetime import timezone

            tz = timezone.utc

        now = datetime.now(tz)
        today_str = now.strftime("%Y-%m-%d")
        today_forecast = next(
            (d for d in data.forecast if d.date == today_str), None
        )
        if not today_forecast or today_forecast.peak_power_kw <= 0:
            return False

        hours = data.hourly.get(today_str, [])
        current_entry = next((h for h in hours if h.hour == now.hour), None)
        if current_entry is None:
            return False

        threshold_kw = today_forecast.peak_power_kw * (self._threshold_pct / 100.0)
        return current_entry.power_kw >= threshold_kw

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return threshold info."""
        return {"threshold_pct": self._threshold_pct}
