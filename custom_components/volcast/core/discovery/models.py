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


@dataclass
class Classification:
    inverters: list[InverterFinding]
    price_entities: list[EntitySnap]
    energy_candidates: list[EntitySnap]   # posortowane wg priorytetu, max 30
