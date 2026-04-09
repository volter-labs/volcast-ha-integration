"""Sensor platform for Volcast Solar Forecast."""

from __future__ import annotations

from datetime import datetime, timedelta
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
    coordinator: VolcastCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    entities: list[SensorEntity] = [
        VolcastEnergyTodaySensor(coordinator, entry),
        VolcastEnergyTomorrowSensor(coordinator, entry),
        VolcastPowerNowSensor(coordinator, entry),
        VolcastApiStatusSensor(coordinator, entry),
    ]

    # Day 3-7 forecast sensors
    for day_num in range(3, 8):
        entities.append(VolcastEnergyDaySensor(coordinator, entry, day_num))

    async_add_entities(entities)


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

    def _get_tz(self):
        """Return HA configured timezone."""
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(self.hass.config.time_zone)
        except Exception:
            from datetime import timezone
            return timezone.utc

    def _date_str(self, days_ahead: int = 0) -> str:
        """Return date string for today + N days."""
        tz = self._get_tz()
        dt = datetime.now(tz) + timedelta(days=days_ahead)
        return dt.strftime("%Y-%m-%d")

    def _hourly_list(self, date_str: str) -> list[dict[str, Any]]:
        """Return hourly breakdown for a specific date."""
        if self._data is None:
            return []
        hours = self._data.hourly.get(date_str, [])
        return [
            {"hour": h.hour, "power_kw": h.power_kw, "energy_kwh": h.energy_kwh}
            for h in hours
        ]

    def _detailed_hourly(self, date_str: str) -> list[dict[str, Any]]:
        """Return hourly data with ISO timestamps (Solcast-compatible format)."""
        if self._data is None:
            return []
        tz = self._get_tz()
        hours = self._data.hourly.get(date_str, [])
        result = []
        for h in hours:
            try:
                dt = datetime(
                    int(date_str[:4]),
                    int(date_str[5:7]),
                    int(date_str[8:10]),
                    h.hour,
                    tzinfo=tz,
                )
                result.append({
                    "period_start": dt.isoformat(),
                    "power_kw": h.power_kw,
                    "energy_kwh": h.energy_kwh,
                })
            except (ValueError, IndexError):
                continue
        return result

    def _detailed_forecast(self, date_str: str) -> list[dict[str, Any]]:
        """Return 5-minute detailed data with ISO timestamps."""
        if self._data is None:
            return []
        tz = self._get_tz()
        entries = self._data.detailed.get(date_str, [])
        result = []
        for e in entries:
            if e.power_w <= 0:
                continue
            try:
                parts = e.time.split(":")
                dt = datetime(
                    int(date_str[:4]),
                    int(date_str[5:7]),
                    int(date_str[8:10]),
                    int(parts[0]),
                    int(parts[1]),
                    tzinfo=tz,
                )
                result.append({
                    "period_start": dt.isoformat(),
                    "power_w": e.power_w,
                    "energy_wh": e.energy_wh,
                })
            except (ValueError, IndexError):
                continue
        return result

    def _day_attributes(self, date_str: str) -> dict[str, Any]:
        """Return daily summary + hourly breakdown for a date."""
        if self._data is None:
            return {}
        attrs: dict[str, Any] = {"date": date_str}
        day = next((d for d in self._data.forecast if d.date == date_str), None)
        if day:
            attrs["peak_power_kw"] = day.peak_power_kw
            attrs["confidence"] = day.confidence
            attrs["sunshine_hours"] = day.sunshine_hours
            attrs["cloud_cover_pct"] = day.cloud_cover_pct
        attrs["hours"] = self._hourly_list(date_str)
        attrs["detailedHourly"] = self._detailed_hourly(date_str)
        detailed = self._detailed_forecast(date_str)
        if detailed:
            attrs["detailedForecast"] = detailed
        return attrs


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
        """Return today's details + hourly breakdown + 7-day summary."""
        today_str = self._date_str(0)
        attrs = self._day_attributes(today_str)
        # Full 7-day daily summary for overview
        if self._data:
            attrs["forecast"] = [
                {
                    "date": d.date,
                    "energy_kwh": d.energy_kwh,
                    "peak_power_kw": d.peak_power_kw,
                }
                for d in self._data.forecast
            ]
            attrs["nowcast_applied"] = self._data.nowcast_applied
        return attrs


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

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return tomorrow's details + hourly breakdown."""
        return self._day_attributes(self._date_str(1))


# =============================================================================
# ENERGY DAY 3-7 (generic)
# =============================================================================


class VolcastEnergyDaySensor(VolcastBaseSensor):
    """Forecasted energy for day N (3-7)."""

    def __init__(
        self,
        coordinator: VolcastCoordinator,
        entry: ConfigEntry,
        day_number: int,
    ) -> None:
        """Initialize."""
        self._days_ahead = day_number - 1  # day 3 = 2 days from today
        super().__init__(
            coordinator,
            entry,
            SensorEntityDescription(
                key=f"energy_day_{day_number}",
                translation_key=f"energy_day_{day_number}",
                native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                device_class=SensorDeviceClass.ENERGY,
                suggested_display_precision=1,
            ),
        )

    @property
    def native_value(self) -> float | None:
        """Return forecasted energy for this day."""
        if self._data is None:
            return None
        date_str = self._date_str(self._days_ahead)
        day = next((d for d in self._data.forecast if d.date == date_str), None)
        if day is None:
            return None
        return round(day.energy_kwh, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return day details + hourly breakdown."""
        return self._day_attributes(self._date_str(self._days_ahead))


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
        """Return current power in W (from 5-min data or hourly fallback)."""
        if self._data is None:
            return None

        tz = self._get_tz()
        now = datetime.now(tz)
        today_str = now.strftime("%Y-%m-%d")

        # Prefer 5-min detailed data (api_version >= 2)
        detailed = self._data.detailed.get(today_str, [])
        if detailed:
            slot_minute = (now.minute // 5) * 5
            slot_key = f"{now.hour:02d}:{slot_minute:02d}"
            entry = next((e for e in detailed if e.time == slot_key), None)
            if entry is not None:
                return entry.power_w
            return 0

        # Fallback: hourly linear interpolation
        current_hour = now.hour
        minute_fraction = now.minute / 60.0

        hours = self._data.hourly.get(today_str, [])
        if not hours:
            return 0

        current_entry = next((h for h in hours if h.hour == current_hour), None)
        next_entry = next((h for h in hours if h.hour == current_hour + 1), None)

        if current_entry is None:
            return 0

        power_kw = current_entry.power_kw
        if next_entry is not None:
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
        self._entry_id = entry.entry_id

    @property
    def native_value(self) -> str | None:
        """Return API status."""
        if self._data is None:
            return "Unavailable"
        return self._data.api_status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return connection details + production tracking status."""
        if self._data is None:
            return {}
        attrs: dict[str, Any] = {
            "cache_age_minutes": self._data.cache_age_minutes,
            "generated_at": self._data.generated_at,
            "location": self._data.location,
            "system_capacity_kwp": self._data.system_capacity_kwp,
            "api_version": self._data.api_version,
            "nowcast_applied": self._data.nowcast_applied,
            "nowcast_ratio": self._data.nowcast_ratio,
        }

        # Production tracker info (jeśli aktywny)
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry_id, {})
        tracker = entry_data.get("tracker") if isinstance(entry_data, dict) else None
        if tracker is not None:
            attrs["production_tracking"] = True
            attrs["submissions_today"] = tracker.submissions_today
            attrs["queued_readings"] = tracker.queued_count
            if tracker.calibration:
                attrs["kalman_bias"] = tracker.calibration.get("bias")
            if tracker.last_submission_time:
                attrs["last_submission"] = tracker.last_submission_time.isoformat()
        else:
            attrs["production_tracking"] = False

        return attrs
