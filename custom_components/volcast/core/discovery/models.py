"""Migawki rejestrów HA i wynik klasyfikacji. Bez importów Home Assistant."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class DeviceSnap:
    id: str
    manufacturer: str | None
    model: str | None
    name: str | None
    sw_version: str | None
    hw_version: str | None
    serial_number: str | None
    identifiers: tuple[tuple[str, str], ...]
    config_entry_ids: tuple[str, ...]


@dataclass(frozen=True)
class EntitySnap:
    entity_id: str
    platform: str
    unique_id: str
    device_id: str | None
    config_entry_id: str | None
    device_class: str | None
    unit: str | None
    translation_key: str | None
    original_name: str | None
    disabled: bool
    # capabilities z rejestru encji (np. options, min/max/step); poza hashem, bo to słownik
    capabilities: dict[str, Any] | None = field(default=None, hash=False)


@dataclass(frozen=True)
class StateSnap:
    entity_id: str
    state: str
    attributes: dict[str, Any]


@dataclass(frozen=True)
class ConfigEntrySnap:
    entry_id: str
    domain: str
    title: str
    host: str | None          # tylko klucze z HOST_KEYS, nigdy całe data


@dataclass
class InverterFinding:
    domain: str
    config_entry_id: str | None
    config_entry_title: str | None
    host: str | None
    devices: list[DeviceSnap] = field(default_factory=list)
    entities: list[EntitySnap] = field(default_factory=list)
    matched_by: str = "domain"   # "domain" | "manufacturer"


@dataclass(frozen=True)
class ChargerRole:
    """Encja pełniąca jedną rolę w ładowarce. Pola zakresu tylko dla nastawy."""
    entity_id: str
    kind: str                       # domena encji: sensor, binary_sensor, number, switch, select, button
    unit: str | None = None
    min: float | None = None
    max: float | None = None
    step: float | None = None
    options: tuple[str, ...] = ()   # surowe opcje encji (status, select), bez interpretacji


@dataclass
class ChargerFinding:
    """Kandydat na ładowarkę EV w obrębie jednego urządzenia HA."""
    device_id: str
    name: str | None
    manufacturer: str | None
    model: str | None
    config_entry_id: str | None
    platform: str | None
    roles: dict[str, ChargerRole] = field(default_factory=dict)
    missing: tuple[str, ...] = ()
    confidence: str = "low"         # "high" | "medium" | "low"


@dataclass
class Classification:
    inverters: list[InverterFinding]
    price_entities: list[EntitySnap]
    energy_candidates: list[EntitySnap]   # posortowane wg priorytetu, max 30
    chargers: list[ChargerFinding] = field(default_factory=list)
