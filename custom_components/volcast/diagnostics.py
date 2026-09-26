"""Diagnostyka wpisu Volcast (pobierana przez użytkownika z karty integracji).

Zawiera wyłącznie tryb wpisu, wersję integracji i raport wykrywania. `entry.data`
(klucz API) ani `entry.options` NIGDY tu nie trafiają — nie ma czego redagować,
bo nic z nich nie kopiujemy.
"""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

# Klucz i wartość domyślna trybu wpisu; stałe CONF_MODE/MODE_* dochodzą z trybem
# „tylko rozpoznanie" (wpis bez `mode` = klasyczny wpis prognozy z kluczem API).
_MODE_KEY = "mode"
_MODE_FORECAST = "forecast"


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    entry_data = (getattr(hass, "data", None) or {}).get(DOMAIN, {}).get(entry.entry_id) or {}
    runner = entry_data.get("discovery")
    mode = entry.data.get(_MODE_KEY) if hasattr(entry.data, "get") else None
    return {
        "entry": {
            "mode": mode if isinstance(mode, str) else _MODE_FORECAST,
            "version": getattr(runner, "integration_version", None) or "unknown",
        },
        "discovery": (runner.report if runner is not None and runner.report
                      else {"status": "pending"}),
    }
