"""Binary sensor platform for Volcast Solar Forecast."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
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
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator: VolcastCoordinator = entry_data["coordinator"]
    tracker = entry_data.get("tracker")
    reconciler = entry_data.get("reconciler")
    peak_threshold = entry.options.get(CONF_PEAK_THRESHOLD, DEFAULT_PEAK_THRESHOLD)

    async_add_entities([
        VolcastPeakProductionSensor(coordinator, entry, peak_threshold),
        IntegrationHealthyBinarySensor(
            coordinator, tracker, reconciler, entry_id=entry.entry_id,
        ),
    ])


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

        # Prefer 5-min detailed data
        detailed = data.detailed.get(today_str, [])
        if detailed:
            slot_minute = (now.minute // 5) * 5
            slot_key = f"{now.hour:02d}:{slot_minute:02d}"
            entry = next((e for e in detailed if e.time == slot_key), None)
            if entry is None:
                return False
            threshold_w = today_forecast.peak_power_kw * 1000 * (self._threshold_pct / 100.0)
            return entry.power_w >= threshold_w

        # Fallback: hourly data
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


# =============================================================================
# DIAGNOSTIC: Integration health (Task 20b)
# =============================================================================


class IntegrationHealthyBinarySensor(BinarySensorEntity):
    """Single-signal health sensor — wireable into automations.

    Healthy when ALL three subsystems OK:
     - forecast_ok: coordinator.last_update_success (or no coordinator)
     - submit_ok: tracker queue ≤ 24 (or no tracker — power-only user)
     - reconcile_ok: last reconcile run was success or skipped (or never run,
       or no reconciler — power-only user)

    Skipped reconciles (e.g. out_of_window, no_stats) are NOT a failure —
    the reconciler intentionally bowed out for valid reasons.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:check-circle"
    _attr_has_entity_name = True
    _attr_translation_key = "integration_healthy"
    _attr_name = "Integration Healthy"

    def __init__(self, coordinator, tracker, reconciler, entry_id: str) -> None:
        self._coord = coordinator
        self._tracker = tracker
        self._reconciler = reconciler
        self._attr_unique_id = f"{entry_id}_integration_healthy"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry_id)},
            "name": "Volcast Solar Forecast",
            "manufacturer": "Volter Labs",
            "model": "PV Forecast",
            "entry_type": "service",
        }

    def _forecast_ok(self) -> bool:
        return self._coord is None or self._coord.last_update_success

    def _submit_ok(self) -> bool:
        if self._tracker is None:
            return True
        return len(self._tracker._queue) <= 24

    def _reconcile_ok(self) -> bool:
        if self._reconciler is None:
            return True
        last = getattr(self._reconciler, "_last_result", None)
        if last is None:
            return True  # not yet run
        return last.success or last.skipped

    @property
    def is_on(self) -> bool:
        return self._forecast_ok() and self._submit_ok() and self._reconcile_ok()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "forecast_ok": self._forecast_ok(),
            "submit_ok": self._submit_ok(),
            "reconcile_ok": self._reconcile_ok(),
            "last_check": datetime.now(timezone.utc).isoformat(),
        }
