"""Klasyfikacja migawek rejestrów HA. Czyste funkcje, bez importów HA."""
from __future__ import annotations

import re

from .known import (INVERTER_DOMAINS, INVERTER_MANUFACTURERS, LOAD_HINTS,
                    MAX_ENERGY_CANDIDATES, PRICE_PLATFORMS)
from .models import (Classification, ConfigEntrySnap, DeviceSnap, EntitySnap,
                     InverterFinding, StateSnap)

_TOKEN_RE = re.compile(r"[^a-z0-9]+")


def _is_inverter_manufacturer(m: str | None) -> bool:
    if not m:
        return False
    tokens = {t for t in _TOKEN_RE.split(m.lower()) if t}
    return not tokens.isdisjoint(INVERTER_MANUFACTURERS)


def _energy_like(e: EntitySnap, states: dict[str, StateSnap]) -> bool:
    if e.disabled or not e.entity_id.startswith("sensor."):
        return False
    st = states.get(e.entity_id)
    dc = e.device_class or (st.attributes.get("device_class") if st else None)
    unit = e.unit or (st.attributes.get("unit_of_measurement") if st else None)
    return dc == "energy" or unit in ("kWh", "Wh", "MWh")


def _load_score(e: EntitySnap) -> int:
    text = f"{e.entity_id} {e.unique_id} {e.original_name or ''}".lower()
    return 0 if any(h in text for h in LOAD_HINTS) else 1


def classify(
    devices: list[DeviceSnap],
    entities: list[EntitySnap],
    config_entries: list[ConfigEntrySnap],
    states: dict[str, StateSnap],
) -> Classification:
    entries = {c.entry_id: c for c in config_entries}
    findings: dict[str, InverterFinding] = {}

    def finding_for(key: str, domain: str, entry: ConfigEntrySnap | None, how: str) -> InverterFinding:
        if key not in findings:
            findings[key] = InverterFinding(
                domain=domain,
                config_entry_id=entry.entry_id if entry else None,
                config_entry_title=entry.title if entry else None,
                host=entry.host if entry else None,
                matched_by=how,
            )
        return findings[key]

    # 1) po domenie wpisu konfiguracji
    for c in config_entries:
        if c.domain in INVERTER_DOMAINS:
            finding_for(c.entry_id, c.domain, c, "domain")
    # 2) urządzenia: przypisz do znalezisk; producent łapie nieznane domeny
    device_to_key: dict[str, str] = {}
    for d in devices:
        key = next((eid for eid in d.config_entry_ids if eid in findings), None)
        if key is None and _is_inverter_manufacturer(d.manufacturer):
            eid = d.config_entry_ids[0] if d.config_entry_ids else f"device:{d.id}"
            entry = entries.get(eid)
            key = f"mfr:{eid}:{d.id}"
            finding_for(key, entry.domain if entry else "unknown", entry, "manufacturer")
        if key is not None:
            findings[key].devices.append(d)
            device_to_key[d.id] = key
    # 3) encje: po urządzeniu, a dla znalezisk po domenie także po wpisie
    for e in entities:
        key = device_to_key.get(e.device_id or "")
        if key is None and e.config_entry_id in findings and findings[e.config_entry_id].matched_by == "domain":
            key = e.config_entry_id
        if key is not None:
            findings[key].entities.append(e)

    prices = [e for e in entities if e.platform in PRICE_PLATFORMS]
    energy = sorted((e for e in entities if _energy_like(e, states)),
                    key=lambda e: (_load_score(e), e.entity_id))[:MAX_ENERGY_CANDIDATES]
    return Classification(inverters=list(findings.values()), price_entities=prices,
                          energy_candidates=energy)
