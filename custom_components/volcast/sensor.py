"""Sensor platform for Volcast Solar Forecast."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import VolcastCoordinator, VolcastData


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Volcast sensors from a config entry."""
    coordinator: VolcastCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        [
            VolcastEnergyTodaySensor(coordinator, entry),
            VolcastEnergyTomorrowSensor(coordinator, entry),
            VolcastPowerNowSensor(coordinator, entry),
            VolcastApiStatusSensor(coordinator, entry),
        ]
    )


class VolcastBaseSensor(CoordinatorEntity[VolcastCoordinator], SensorEntity):
    """Base class for Volcast sensors."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: VolcastCoordinator,
        entry: ConfigEntry,
        description: SensorEntityDescription,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Volcast Solar Forecast",
            "manufacturer": "Volter Labs",
            "model": "PV Forecast",
            "entry_type": "service",
        }

    @property
    def _data(self) -> VolcastData | None:
        """Shortcut to coordinator data."""
        return self.coordinator.data


# =============================================================================
# ENERGY TODAY
# =============================================================================


class VolcastEnergyTodaySensor(VolcastBaseSensor):
    """Today's forecasted energy production."""

    def __init__(self, coordinator: VolcastCoordinator, entry: ConfigEntry) -> None:
        """Initialize."""
        super().__init__(
            coordinator,
            entry,
            SensorEntityDescription(
                key="energy_today",
                translation_key="energy_today",
                native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                device_class=SensorDeviceClass.ENERGY,
                state_class=SensorStateClass.TOTAL,
                suggested_display_precision=1,
            ),
        )

    @property
    def native_value(self) -> float | None:
        """Return today's forecasted energy."""
        if self._data is None:
            return None
        return round(self._data.energy_today, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return forecast details as attributes."""
        if self._data is None:
            return {}
        today = next((d for d in self._data.forecast if d.date == self._today_str), None)
        attrs: dict[str, Any] = {}
        if today:
            attrs["peak_power_kw"] = today.peak_power_kw
            attrs["confidence"] = today.confidence
            attrs["sunshine_hours"] = today.sunshine_hours
            attrs["cloud_cover_pct"] = today.cloud_cover_pct
        attrs["forecast"] = [
            {
                "date": d.date,
                "energy_kwh": d.energy_kwh,
                "peak_power_kw": d.peak_power_kw,
            }
            for d in self._data.forecast
        ]
        return attrs

    @property
    def _today_str(self) -> str:
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(self.hass.config.time_zone)
        except Exception:
            from datetime import timezone
            tz = timezone.utc
        return datetime.now(tz).strftime("%Y-%m-%d")


# =============================================================================
# ENERGY TOMORROW
# =============================================================================


class VolcastEnergyTomorrowSensor(VolcastBaseSensor):
    """Tomorrow's forecasted energy production."""

    def __init__(self, coordinator: VolcastCoordinator, entry: ConfigEntry) -> None:
        """Initialize."""
        super().__init__(
            coordinator,
            entry,
            SensorEntityDescription(
                key="energy_tomorrow",
                translation_key="energy_tomorrow",
                native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                device_class=SensorDeviceClass.ENERGY,
                suggested_display_precision=1,
            ),
        )

    @property
    def native_value(self) -> float | None:
        """Return tomorrow's forecasted energy."""
        if self._data is None:
            return None
        return round(self._data.energy_tomorrow, 2)


# =============================================================================
# POWER NOW
# =============================================================================


class VolcastPowerNowSensor(VolcastBaseSensor):
    """Current estimated power output (interpolated from hourly forecast)."""

    def __init__(self, coordinator: VolcastCoordinator, entry: ConfigEntry) -> None:
        """Initialize."""
        super().__init__(
            coordinator,
            entry,
            SensorEntityDescription(
                key="power_now",
                translation_key="power_now",
                native_unit_of_measurement=UnitOfPower.WATT,
                device_class=SensorDeviceClass.POWER,
                state_class=SensorStateClass.MEASUREMENT,
                suggested_display_precision=0,
            ),
        )

    @property
    def native_value(self) -> float | None:
        """Return interpolated current power in W."""
        if self._data is None:
            return None

        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(self.hass.config.time_zone)
        except Exception:
            from datetime import timezone
            tz = timezone.utc

        now = datetime.now(tz)
        today_str = now.strftime("%Y-%m-%d")
        current_hour = now.hour
        minute_fraction = now.minute / 60.0

        hours = self._data.hourly.get(today_str, [])
        if not hours:
            return 0

        # Find current and next hour entries
        current_entry = next((h for h in hours if h.hour == current_hour), None)
        next_entry = next((h for h in hours if h.hour == current_hour + 1), None)

        if current_entry is None:
            return 0

        power_kw = current_entry.power_kw
        if next_entry is not None:
            # Linear interpolation between current and next hour
            power_kw = (
                current_entry.power_kw * (1 - minute_fraction)
                + next_entry.power_kw * minute_fraction
            )

        return round(power_kw * 1000)  # kW → W


# =============================================================================
# API STATUS (diagnostic)
# =============================================================================


class VolcastApiStatusSensor(VolcastBaseSensor):
    """Diagnostic sensor showing API connection status."""

    def __init__(self, coordinator: VolcastCoordinator, entry: ConfigEntry) -> None:
        """Initialize."""
        super().__init__(
            coordinator,
            entry,
            SensorEntityDescription(
                key="api_status",
                translation_key="api_status",
                entity_category=EntityCategory.DIAGNOSTIC,
            ),
        )

    @property
    def native_value(self) -> str | None:
        """Return API status."""
        if self._data is None:
            return "Unavailable"
        return self._data.api_status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return connection details."""
        if self._data is None:
            return {}
        return {
            "from_cache": self._data.from_cache,
            "cache_age_minutes": self._data.cache_age_minutes,
            "generated_at": self._data.generated_at,
            "location": self._data.location,
            "system_capacity_kwp": self._data.system_capacity_kwp,
        }
