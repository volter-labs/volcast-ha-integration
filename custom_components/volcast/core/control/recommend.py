"""Rekomendacja ścieżki sterowania: system wybiera za użytkownika (czysta logika, bez HA).

Wejście: raport rozpoznania (`inverters`), profile marek, wynik wyszukiwania bezpośredniego
(`ProbeReport`) z powodem oferty (`offer`, kod z warstwy HA albo None), mapa encji wybranej
integracji, flaga `ems` integracji (None = z profilu) i kolizje statyczne adresu (domeny).

Reguły (kolejność):
1. integracja falownika z encją trybu (`mode` w mapie) → `entities`;
2. falownik rozpoznany sondą: bez integracji → `direct`, z integracją bez zapisu →
   `direct_with_integration_data`;
3. brak profilu → `unsupported` (`no_profile`); profil bez żadnej drogi zapisu →
   `unsupported` (`no_write_path`).

`ladder_start` (pierwszy szczebel weryfikacji): profil i jego droga zapisu zweryfikowane
(wpis integracji dla encji; sekcja `modbus` i udana próba dla rejestrów) → 3 (zapis
kontrolny), inaczej 1. Integracja z `ems: true` to od razu konflikt `entry` — sama steruje
baterią. Kolizja statyczna adresu (inny wpis na tym falowniku) też.

Ładunek (`to_payload`) to blok `driver.control.recommendation` kontraktu sterowania: bez
adresów, seriali i tokenów; `entity_map` tylko dla ścieżki encji, ≤24 pozycji, entity_id
tylko we wzorcu kontraktu.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from ..discovery.known import INVERTER_DOMAINS
from ..profile import Profile, direct_verified, integration_ems
from .select import InverterHint, ProfileChoice, control_verified, select_profile

ENTITIES, DIRECT = "entities", "direct"
DIRECT_WITH_INTEGRATION_DATA, UNSUPPORTED = "direct_with_integration_data", "unsupported"
PATHS = (ENTITIES, DIRECT, DIRECT_WITH_INTEGRATION_DATA, UNSUPPORTED)

# Kody powodu (≤64 znaki); dla `direct` przy odmowie oferty — kod oferty z warstwy HA.
INTEGRATION_WRITES, INTEGRATION_READ_ONLY = "integration_writes", "integration_read_only"
IDENTIFIED, NO_PROFILE, NO_WRITE_PATH = "identified", "no_profile", "no_write_path"

RUNG_IDENTIFY, RUNG_CONTROL_WRITE = 1, 3
MAX_TEXT, MAX_EVIDENCE, MAX_ENTITY_MAP, MAX_CONFLICTS = 64, 120, 24, 8
_ENTITY_ID = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
# Kolizja nieznanej integracji i inny wpis Volcast mają w `static_conflicts` stałe nazwy.
_EMS_EVIDENCE = "integration controls the battery itself (ems)"
_CLASH_EVIDENCE = "another entry uses the same inverter address"


@dataclass(frozen=True)
class Recommendation:
    path: str
    reason: str
    ladder_start: int
    integration: Mapping[str, Any] | None = None     # {domain, name, origin}
    device: Mapping[str, Any] | None = None          # {manufacturer, model}
    entity_map: tuple[tuple[str, str], ...] = ()     # (klucz profilu, entity_id)
    conflicts: tuple[Mapping[str, str], ...] = ()    # [{kind, label, evidence}]

    def to_payload(self) -> dict:
        out: dict = {"path": self.path, "reason": _cut(self.reason),
                     "ladder_start": min(max(int(self.ladder_start), RUNG_IDENTIFY), RUNG_CONTROL_WRITE)}
        for name, block in (("integration", self.integration), ("device", self.device)):
            clean = {k: _cut(v) for k, v in (block or {}).items() if isinstance(v, str) and v}
            if clean:
                out[name] = clean
        if self.path == ENTITIES:
            pairs = [{"key": k, "entity_id": e} for k, e in sorted(self.entity_map)
                     if isinstance(e, str) and len(e) <= MAX_TEXT and _ENTITY_ID.fullmatch(e)]
            if pairs:
                out["entity_map"] = pairs[:MAX_ENTITY_MAP]
        return out

    def conflicts_payload(self) -> list[dict]:
        return [{"kind": c["kind"], "label": _cut(c["label"]), "evidence": _cut(c["evidence"], MAX_EVIDENCE)}
                for c in self.conflicts[:MAX_CONFLICTS]]


def _cut(text: str, limit: int = MAX_TEXT) -> str:
    return str(text)[:limit]


def hints_from_report(report: Mapping | None) -> list[InverterHint]:
    """Wskazówki falownika z raportu rozpoznania (domena + producent/model urządzeń HA)."""
    out: list[InverterHint] = []
    for inv in (report or {}).get("inverters") or ():
        domain = inv.get("domain")
        if not isinstance(domain, str) or not domain:
            continue
        devices = [d for d in inv.get("devices") or () if isinstance(d, Mapping)] or [{}]
        out.extend(InverterHint(domain, d.get("manufacturer"), d.get("model")) for d in devices)
    return out


def _report_inverter(report: Mapping | None, domain: str | None) -> Mapping | None:
    invs = [i for i in (report or {}).get("inverters") or () if isinstance(i, Mapping)]
    if domain is not None:
        return next((i for i in invs if i.get("domain") == domain), None)
    return invs[0] if invs else None


def _integration(inv: Mapping | None, domain: str | None) -> dict | None:
    domain = domain or (inv.get("domain") if inv else None)
    if not domain:
        return None
    origin = (inv.get("matched_by") if inv else None) or "profile"
    return {"domain": domain, "name": INVERTER_DOMAINS.get(domain, domain), "origin": origin}


def _report_device(inv: Mapping | None) -> dict | None:
    dev = next((d for d in (inv or {}).get("devices") or () if isinstance(d, Mapping)), None)
    return {"manufacturer": dev.get("manufacturer"), "model": dev.get("model")} if dev else None


def _probe_device(probe, profile: Profile) -> dict:
    words = str(profile.raw.get("label") or "").split()
    return {"manufacturer": words[0] if words else None, "model": probe.identity.model}


def _conflicts(domain: str | None, ems: bool, clash: Iterable[str]) -> tuple[dict, ...]:
    out: list[dict] = []
    if ems and domain:
        out.append({"kind": "entry", "label": domain, "evidence": _EMS_EVIDENCE})
    for d in clash:
        if isinstance(d, str) and d and all(c["label"] != d for c in out):
            out.append({"kind": "entry", "label": d, "evidence": _CLASH_EVIDENCE})
    return tuple(out)


def recommend(report: Mapping | None, profiles: Sequence[Profile], probe=None, offer: str | None = None,
              entity_map: Mapping[str, str] | None = None, ems_flag: bool | None = None,
              conflicts: Sequence[str] = (), *, choice: ProfileChoice | None = None) -> Recommendation:
    """Rekomendacja ścieżki; `choice` = wybór profilu, z którego pochodzi `entity_map`
    (None → wybór z wskazówek raportu, jak `select_profile`)."""
    if choice is None:
        choice = select_profile(hints_from_report(report), profiles)
    mapped = dict(entity_map or {})
    domain = choice.integration_domain if choice is not None else None
    inv = _report_inverter(report, domain)
    integration = _integration(inv, domain)
    integ_domain = integration["domain"] if integration else None
    ems = ems_flag if ems_flag is not None else bool(
        choice is not None and integration_ems(choice.profile, integ_domain))
    found = _conflicts(integ_domain, ems, conflicts)
    device = _report_device(inv)
    if device is None and choice is not None and choice.model:
        device = {"manufacturer": None, "model": choice.model}

    if choice is not None and domain and mapped.get("mode"):
        start = RUNG_CONTROL_WRITE if control_verified(choice.profile, domain) else RUNG_IDENTIFY
        return Recommendation(ENTITIES, INTEGRATION_WRITES, start, integration, device,
                              tuple(sorted(mapped.items())), found)

    ident = getattr(probe, "identity", None)
    probe_profile = next((p for p in profiles if ident is not None and p.id == ident.profile_id), None)
    if probe_profile is not None:
        start = RUNG_CONTROL_WRITE if direct_verified(probe_profile) and probe.direct_available else RUNG_IDENTIFY
        device = _probe_device(probe, probe_profile)
        if integration is not None:
            return Recommendation(DIRECT_WITH_INTEGRATION_DATA, INTEGRATION_READ_ONLY, start, integration,
                                  device, (), found)
        return Recommendation(DIRECT, offer or IDENTIFIED, start, None, device, (), found)

    reason = NO_PROFILE if choice is None else NO_WRITE_PATH
    return Recommendation(UNSUPPORTED, reason, RUNG_IDENTIFY, integration, device, (), found)
