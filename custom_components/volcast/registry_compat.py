"""Odczyt rejestru urządzeń zgodny ze starszymi i nowszymi wersjami HA.

Starsze HA: `registry.devices` to mapowanie id → urządzenie (iteracja daje id).
Nowsze HA: `registry.devices` to widok, którego iteracja daje urządzenia, a użycie
jako mapowania (`.values()`, `[id]`) jest wycofywane (ostrzeżenie w logu, potem błąd).
"""
from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)
_warned_missing = False


def all_devices(registry: Any) -> list:
    """Wszystkie wpisy rejestru urządzeń (także wyłączone) — bez wycofywanych wywołań.

    Brak `devices` (przyszłe HA) nie wywraca wołających, ale jest głośny raz: bez urządzeń
    maskowanie numerów seryjnych w diagnostyce i podpowiedzi profilu działają słabiej.
    """
    global _warned_missing
    devices = getattr(registry, "devices", None)
    if devices is None:
        if not _warned_missing:
            _warned_missing = True
            _LOGGER.warning("Volcast: the device registry has no device list in this Home Assistant "
                            "version — inverter hints and serial masking are limited")
        return []
    if isinstance(devices, Mapping):
        return list(devices.values())
    return list(devices)
