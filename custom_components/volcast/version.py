"""Wersja integracji z loadera HA (manifest już wczytany — bez I/O w pętli zdarzeń)."""
from __future__ import annotations

import logging

from .const import DOMAIN

try:
    from homeassistant.loader import async_get_integration
except ImportError:  # atrapy w testach nie mają loadera
    async_get_integration = None

_LOGGER = logging.getLogger(__name__)


async def async_integration_version(hass) -> str:
    """Wersja z manifest.json; nigdy nie rzuca ("unknown", gdy niedostępna)."""
    try:
        if async_get_integration is None:
            return "unknown"
        version = getattr(await async_get_integration(hass, DOMAIN), "version", None)
        return str(version) if version else "unknown"
    except Exception:  # noqa: BLE001 — wersja jest informacyjna
        _LOGGER.debug("Volcast: integration version unavailable", exc_info=True)
        return "unknown"
