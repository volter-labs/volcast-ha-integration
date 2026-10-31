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


def device_config_entry_ids(device: Any) -> tuple[str, ...]:
    """Wpisy urządzenia w stałej kolejności; wpis główny (jeśli HA go zna) pierwszy —
    klasyfikacja bierze pierwszy wpis jako klucz znaleziska.

    HA 2026.10+ (`is_composite_device`): urządzenie należy do jednego wpisu, `config_entry_id`;
    `config_entries` i `primary_config_entry` są tam wycofane i zgłaszają użycie. Starsze HA —
    dotychczasowa ścieżka (bez ostrzeżeń)."""
    if getattr(device, "is_composite_device", None) is False:
        entry_id = getattr(device, "config_entry_id", None)
        return (str(entry_id),) if entry_id else ()
    ids = sorted(str(x) for x in (getattr(device, "config_entries", None) or ()))
    primary = getattr(device, "primary_config_entry", None)
    if primary in ids:
        ids.remove(primary)
        ids.insert(0, primary)
    return tuple(ids)
