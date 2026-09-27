"""Odczyt rejestru urządzeń zgodny ze starszymi i nowszymi wersjami HA.

Starsze HA: `registry.devices` to mapowanie id → urządzenie (iteracja daje id).
Nowsze HA: `registry.devices` to widok, którego iteracja daje urządzenia, a użycie
jako mapowania (`.values()`, `[id]`) jest wycofywane (ostrzeżenie w logu, potem błąd).
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def all_devices(registry: Any) -> list:
    """Wszystkie wpisy rejestru urządzeń (także wyłączone) — bez wycofywanych wywołań."""
    devices = getattr(registry, "devices", None)
    if devices is None:
        return []
    if isinstance(devices, Mapping):
        return list(devices.values())
    return list(devices)
