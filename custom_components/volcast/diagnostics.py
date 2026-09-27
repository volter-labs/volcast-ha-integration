"""Diagnostyka wpisu Volcast (pobierana przez użytkownika z karty integracji).

`entry.data` (klucz API, tokeny parowania, dane konta) NIGDY nie trafia tu w
całości — kopiujemy z niego wyłącznie pojedyncze, nieszkodliwe pola (czy wpis
jest sparowany, hostname backendu). `entry.options` trafia tu jedynie jako
lista NAZW kluczy (`options_keys`), nigdy wartości. Sekcja `control`, gdy
sterowanie jest złożone, przechodzi przez to samo maskowanie seriali/MAC-ów/
e-maili co raport wykrywania (`core/discovery/report.py`).
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import CONF_BACKEND, CONF_MODE, DOMAIN
from .core.discovery.report import _mask_value, _serial_pattern
from .registry_compat import all_devices

# Wpis bez `mode` (sprzed trybu „tylko rozpoznanie") to klasyczny wpis prognozy
# z kluczem API.
_MODE_FORECAST = "forecast"


def _serials(hass: HomeAssistant) -> set[str]:
    """Numery seryjne znane rejestrowi urządzeń — do maskowania sekcji `control`."""
    out: set[str] = set()
    for dev in all_devices(dr.async_get(hass)):
        if getattr(dev, "serial_number", None):
            out.add(str(dev.serial_number))
        for ident in getattr(dev, "identifiers", ()) or ():
            out.update(str(p) for p in list(ident)[1:] if len(str(p)) >= 8)
    return out


def _control(hass: HomeAssistant, rt: Any) -> dict | None:
    """Sekcja sterowania: profil, mapowanie encji (zamaskowane), stan wykonawcy.

    `foreign_changes` trzyma `entity_id` — to jest pobrana lokalnie diagnostyka,
    nie telemetria (patrz `control/executor.py` — logi i telemetria widzą tylko
    nazwę parametru), ale numer seryjny w środku `entity_id` i tak przechodzi
    przez maskowanie niżej.
    """
    if rt is None:
        return None
    ex = rt.executor
    raw = {"profile": getattr(getattr(rt.choice, "profile", None), "id", None),
           "integration_domain": getattr(rt.choice, "integration_domain", None),
           "mapped": dict(rt.mapped or {}), "exec": ex.exec_summary(),
           "tou_preview": ex.tou_preview, "foreign_changes": list(ex.foreign_changes)}
    return _mask_value(raw, _serial_pattern(_serials(hass)))


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    entry_data = (getattr(hass, "data", None) or {}).get(DOMAIN, {}).get(entry.entry_id) or {}
    runner = entry_data.get("discovery")
    mode = entry.data.get(CONF_MODE) if hasattr(entry.data, "get") else None
    backend = entry.data.get(CONF_BACKEND) if hasattr(entry.data, "get") else None
    return {
        "entry": {
            "mode": mode if isinstance(mode, str) else _MODE_FORECAST,
            "version": getattr(runner, "integration_version", None) or "unknown",
            "paired": isinstance(backend, dict),
            "backend_host": urlparse(backend.get("base_url", "")).hostname
            if isinstance(backend, dict) else None,
            "options_keys": sorted(entry.options),
        },
        "discovery": (runner.report if runner is not None and runner.report
                      else {"status": "pending"}),
        "control": _control(hass, entry_data.get("control")),
    }
