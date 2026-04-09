"""Shared test fixtures for Volcast integration tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

# ---------------------------------------------------------------------------
# Stub out homeassistant imports so we can import the integration modules
# without a full HA installation.
# ---------------------------------------------------------------------------

import sys
import types


def _make_module(name: str, attrs: dict[str, Any] | None = None) -> types.ModuleType:
    mod = types.ModuleType(name)
    if attrs:
        for k, v in attrs.items():
            setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


# --- homeassistant.core ---
class _FakeHomeAssistant:
    class _Config:
        time_zone = "Europe/Warsaw"
    config = _Config()


def _fake_callback(func):
    """Stub for @callback decorator."""
    return func


class _FakeEvent:
    """Stub for homeassistant.core.Event."""
    def __init__(self, data=None):
        self.data = data or {}


_make_module("homeassistant")
_make_module("homeassistant.core", {
    "HomeAssistant": _FakeHomeAssistant,
    "Event": _FakeEvent,
    "callback": _fake_callback,
})
_make_module("homeassistant.const", {
    "CONF_API_KEY": "api_key",
    "Platform": MagicMock(),
    "EntityCategory": MagicMock(),
    "UnitOfEnergy": MagicMock(),
    "UnitOfPower": MagicMock(),
    "STATE_UNAVAILABLE": "unavailable",
    "STATE_UNKNOWN": "unknown",
})

# --- homeassistant.config_entries ---
class _FakeConfigEntry:
    entry_id = "test_entry_id"
    data = {"api_key": "fake"}
    options = {}
    def add_update_listener(self, _): ...
    def async_on_unload(self, _): ...

_make_module("homeassistant.config_entries", {"ConfigEntry": _FakeConfigEntry})

# --- homeassistant.helpers ---
_make_module("homeassistant.helpers")
_make_module("homeassistant.helpers.entity_platform", {"AddEntitiesCallback": Any})


class _FakeStore:
    """Minimal stub for homeassistant.helpers.storage.Store."""

    def __init__(self, hass=None, version: int = 1, key: str = ""):
        self._version = version
        self._key = key
        self._data: Any = None

    async def async_load(self) -> Any:
        return self._data

    async def async_save(self, data: Any) -> None:
        self._data = data

    async def async_remove(self) -> None:
        self._data = None


_make_module("homeassistant.helpers.storage", {"Store": _FakeStore})
_make_module("homeassistant.helpers.event", {
    "async_track_state_change_event": MagicMock(return_value=MagicMock()),
    "async_track_time_interval": MagicMock(return_value=MagicMock()),
})
_make_module("homeassistant.helpers.aiohttp_client", {
    "async_get_clientsession": MagicMock(),
})
_make_module("homeassistant.helpers.issue_registry", {
    "async_create_issue": MagicMock(),
    "async_delete_issue": MagicMock(),
})


class _FakeCoordinatorEntity:
    """Minimal stub for CoordinatorEntity."""
    def __init__(self, coordinator):
        self.coordinator = coordinator

    def __class_getitem__(cls, item):
        return cls


class _FakeDataUpdateCoordinator:
    """Minimal stub for DataUpdateCoordinator."""
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    def __class_getitem__(cls, item):
        return cls

_make_module("homeassistant.helpers.update_coordinator", {
    "DataUpdateCoordinator": _FakeDataUpdateCoordinator,
    "UpdateFailed": Exception,
    "CoordinatorEntity": _FakeCoordinatorEntity,
})

# --- homeassistant.components.sensor ---
class _FakeSensorEntity:
    _attr_has_entity_name = False
    _attr_unique_id = None
    _attr_device_info = None
    entity_description = None

class _FakeSensorEntityDescription:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

_make_module("homeassistant.components")
_make_module("homeassistant.components.sensor", {
    "SensorDeviceClass": MagicMock(),
    "SensorEntity": _FakeSensorEntity,
    "SensorEntityDescription": _FakeSensorEntityDescription,
    "SensorStateClass": MagicMock(),
})

# --- homeassistant.components.binary_sensor ---
class _FakeBinarySensorEntity:
    _attr_has_entity_name = False
    _attr_unique_id = None
    _attr_device_info = None
    entity_description = None

class _FakeBinarySensorEntityDescription:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

_make_module("homeassistant.components.binary_sensor", {
    "BinarySensorEntity": _FakeBinarySensorEntity,
    "BinarySensorEntityDescription": _FakeBinarySensorEntityDescription,
})


# ---------------------------------------------------------------------------
# Now we can safely import integration code
# ---------------------------------------------------------------------------
from custom_components.volcast.coordinator import (
    DailyForecast,
    DetailedEntry,
    HourlyEntry,
    VolcastData,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_sample_data(
    *,
    energy_today: float = 25.5,
    energy_tomorrow: float = 30.1,
    date_today: str = "2026-03-20",
    date_tomorrow: str = "2026-03-21",
    include_detailed: bool = False,
) -> VolcastData:
    """Create a realistic VolcastData for testing."""
    forecast = [
        DailyForecast(
            date=date_today,
            energy_kwh=energy_today,
            peak_power_kw=4.2,
            confidence=0.85,
            sunshine_hours=8.5,
            cloud_cover_pct=25.0,
        ),
        DailyForecast(
            date=date_tomorrow,
            energy_kwh=energy_tomorrow,
            peak_power_kw=3.8,
            confidence=0.78,
            sunshine_hours=7.0,
            cloud_cover_pct=40.0,
        ),
    ]

    hourly = {
        date_today: [
            HourlyEntry(hour=h, power_kw=0.0, energy_kwh=0.0) for h in range(0, 6)
        ] + [
            HourlyEntry(hour=6, power_kw=0.1, energy_kwh=0.05),
            HourlyEntry(hour=7, power_kw=0.5, energy_kwh=0.4),
            HourlyEntry(hour=8, power_kw=1.2, energy_kwh=1.0),
            HourlyEntry(hour=9, power_kw=2.5, energy_kwh=2.2),
            HourlyEntry(hour=10, power_kw=3.8, energy_kwh=3.5),
            HourlyEntry(hour=11, power_kw=4.2, energy_kwh=4.0),
            HourlyEntry(hour=12, power_kw=4.0, energy_kwh=3.8),
            HourlyEntry(hour=13, power_kw=3.5, energy_kwh=3.2),
            HourlyEntry(hour=14, power_kw=2.8, energy_kwh=2.5),
            HourlyEntry(hour=15, power_kw=1.8, energy_kwh=1.5),
            HourlyEntry(hour=16, power_kw=0.8, energy_kwh=0.6),
            HourlyEntry(hour=17, power_kw=0.2, energy_kwh=0.1),
        ] + [
            HourlyEntry(hour=h, power_kw=0.0, energy_kwh=0.0) for h in range(18, 24)
        ],
        date_tomorrow: [
            HourlyEntry(hour=h, power_kw=0.0, energy_kwh=0.0) for h in range(0, 7)
        ] + [
            HourlyEntry(hour=7, power_kw=0.3, energy_kwh=0.2),
            HourlyEntry(hour=8, power_kw=1.0, energy_kwh=0.8),
            HourlyEntry(hour=9, power_kw=2.0, energy_kwh=1.8),
            HourlyEntry(hour=10, power_kw=3.0, energy_kwh=2.7),
        ] + [
            HourlyEntry(hour=h, power_kw=0.0, energy_kwh=0.0) for h in range(11, 24)
        ],
    }

    detailed: dict[str, list[DetailedEntry]] = {}
    if include_detailed:
        detailed[date_today] = [
            DetailedEntry(time="10:00", power_w=3800, energy_wh=317),
            DetailedEntry(time="10:05", power_w=3850, energy_wh=321),
            DetailedEntry(time="10:10", power_w=3900, energy_wh=325),
            DetailedEntry(time="10:15", power_w=3750, energy_wh=313),
            DetailedEntry(time="10:20", power_w=3700, energy_wh=308),
            DetailedEntry(time="10:25", power_w=3600, energy_wh=300),
            DetailedEntry(time="10:30", power_w=3500, energy_wh=292),
            DetailedEntry(time="10:35", power_w=3650, energy_wh=304),
            DetailedEntry(time="10:40", power_w=3700, energy_wh=308),
            DetailedEntry(time="10:45", power_w=3800, energy_wh=317),
            DetailedEntry(time="10:50", power_w=3850, energy_wh=321),
            DetailedEntry(time="10:55", power_w=3900, energy_wh=325),
        ]

    return VolcastData(
        energy_today=energy_today,
        energy_tomorrow=energy_tomorrow,
        forecast=forecast,
        hourly=hourly,
        detailed=detailed,
        wh_hours={},
        system_capacity_kwp=6.5,
        location="Warsaw, PL",
        generated_at="2026-03-20T06:00:00Z",
        cache_age_minutes=5,
        api_version=2 if include_detailed else 1,
        api_status="Active",
    )


@pytest.fixture
def sample_data() -> VolcastData:
    """VolcastData without detailed 5-min data."""
    return make_sample_data(include_detailed=False)


@pytest.fixture
def sample_data_detailed() -> VolcastData:
    """VolcastData with detailed 5-min data (Premium/API v2)."""
    return make_sample_data(include_detailed=True)


@pytest.fixture
def empty_data() -> VolcastData:
    """VolcastData with empty hourly/detailed (e.g. error or new install)."""
    return VolcastData(
        energy_today=0,
        energy_tomorrow=0,
        forecast=[],
        hourly={},
        detailed={},
        wh_hours={},
        system_capacity_kwp=None,
        location="",
        generated_at="",
        cache_age_minutes=0,
        api_version=0,
        api_status="Premium required",
    )


class FakeCoordinator:
    """Minimal coordinator stub for sensor tests."""
    def __init__(self, data: VolcastData | None):
        self.data = data


class FakeHass:
    """Minimal hass stub."""
    class _Config:
        time_zone = "Europe/Warsaw"
    config = _Config()


@pytest.fixture
def fake_hass() -> FakeHass:
    return FakeHass()
