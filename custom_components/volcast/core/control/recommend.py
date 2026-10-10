"""Rekomendacja ścieżki sterowania: system wybiera za użytkownika (czysta logika, bez HA).

Wejście: raport rozpoznania (`inverters`), profile marek, wynik wyszukiwania bezpośredniego
(`ProbeReport`) z powodem oferty (`offer`, kod z warstwy HA; None = „Bezpośrednio” dostępne),
mapa encji wybranej integracji, kolizje statyczne adresu (domeny) i pochodzenie integracji
(`origins`: domena → `core`|`custom`, ustala warstwa HA).

Reguły (kolejność):
1. integracja falownika z encją trybu (`mode` w mapie) → `entities`;
2. falownik rozpoznany sondą: integracja TEGO falownika bez zapisu → `direct_with_integration_data`,
   inaczej (brak integracji albo integracja innego urządzenia) → `direct`;
3. brak profilu → `unsupported` (`no_profile`); profil jest, ale nie ma ani encji trybu, ani
   rozpoznanego falownika → `unsupported` (`no_write_path`).

`ladder_start` (pierwszy szczebel weryfikacji): profil i jego droga zapisu zweryfikowane
(wpis integracji dla encji; sekcja `modbus`, udana próba i oferta bez odmowy dla rejestrów) → 4
(zapis kontrolny — bez 24-godzinnej próby), inaczej 1. Odmowa oferty to nie powód rekomendacji: kolizja adresu trafia do
`conflicts`. Flaga `ems` integracji nie jest konfliktem — konflikt wynika tylko z dowodu.

Ładunek (`to_payload`) to blok `driver.control.recommendation` kontraktu sterowania: zamknięte
listy kodów i pochodzenia, bez adresów, seriali i tokenów; `entity_map` tylko dla ścieżki encji,
≤24 pozycji, klucze i entity_id tylko we wzorcach kontraktu.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..discovery.known import INVERTER_DOMAINS
from ..profile import Profile, direct_verified, ha_integration
from .conflict import entry_conflicts
from .select import InverterHint, ProfileChoice, control_verified, select_profile

ENTITIES, DIRECT = "entities", "direct"
DIRECT_WITH_INTEGRATION_DATA, UNSUPPORTED = "direct_with_integration_data", "unsupported"
PATHS = (ENTITIES, DIRECT, DIRECT_WITH_INTEGRATION_DATA, UNSUPPORTED)

# Kody powodu — lista zamknięta kontraktu (chmura odrzuca całą rekomendację z kodem spoza niej).
INTEGRATION_WRITE_ENTITIES = "integration_write_entities"
NO_INTEGRATION_IDENTIFY_OK = "no_integration_identify_ok"
INTEGRATION_READ_ONLY = "integration_read_only"
NO_PROFILE, NO_WRITE_PATH = "no_profile", "no_write_path"
REASONS = (INTEGRATION_WRITE_ENTITIES, NO_INTEGRATION_IDENTIFY_OK, INTEGRATION_READ_ONLY, NO_PROFILE,
           NO_WRITE_PATH)
ORIGINS = ("core", "custom")

RUNG_IDENTIFY, RUNG_CONTROL_WRITE = 1, 4
MAX_TEXT, MAX_EVIDENCE, MAX_ENTITY_MAP, MAX_CONFLICTS = 64, 120, 24, 8
_ENTITY_ID = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
_MAP_KEY = re.compile(r"^[a-z0-9_]{1,24}$")
_DOMAIN = re.compile(r"^[a-z0-9_]{1,32}$")


@dataclass(frozen=True)
class Recommendation:
    path: str
    reason: str
    ladder_start: int
    integration: Mapping[str, Any] | None = None     # {domain, name, origin?}
    device: Mapping[str, Any] | None = None          # {manufacturer, model}
    entity_map: tuple[tuple[str, str], ...] = ()     # (klucz profilu, entity_id)
    conflicts: tuple[Mapping[str, str], ...] = ()    # [{kind, label, evidence}]

    def to_payload(self) -> dict:
        out: dict = {"path": self.path, "reason": _cut(self.reason),
                     "ladder_start": min(max(int(self.ladder_start), RUNG_IDENTIFY), RUNG_CONTROL_WRITE)}
        integ = self.integration or {}
        if isinstance(integ.get("domain"), str) and _DOMAIN.fullmatch(integ["domain"]):
            out["integration"] = {k: _cut(v) for k, v in integ.items() if isinstance(v, str) and v
                                  and (k != "origin" or v in ORIGINS)}
        device = {k: _cut(v) for k, v in (self.device or {}).items() if isinstance(v, str) and v}
        if device:
            out["device"] = device
        if self.path == ENTITIES:
            pairs = [{"key": k, "entity_id": e} for k, e in sorted(self.entity_map)
                     if isinstance(k, str) and _MAP_KEY.fullmatch(k)
                     and isinstance(e, str) and len(e) <= MAX_TEXT and _ENTITY_ID.fullmatch(e)]
            if pairs:
                out["entity_map"] = pairs[:MAX_ENTITY_MAP]
        return out

    def conflicts_payload(self) -> list[dict]:
        return [{"kind": c["kind"], "label": _cut(c["label"]), "evidence": _cut(c["evidence"], MAX_EVIDENCE)}
                for c in self.conflicts[:MAX_CONFLICTS]]


def _cut(text: str, limit: int = MAX_TEXT) -> str:
    return str(text)[:limit]


def _inverters(report: Mapping | None) -> list[Mapping]:
    return [i for i in (report or {}).get("inverters") or () if isinstance(i, Mapping)]


def _devices(inv: Mapping) -> list[Mapping]:
    return [d for d in inv.get("devices") or () if isinstance(d, Mapping)]


def hints_from_report(report: Mapping | None) -> list[InverterHint]:
    """Wskazówki falownika z raportu rozpoznania (domena + producent/model urządzeń HA)."""
    out: list[InverterHint] = []
    for inv in _inverters(report):
        domain = inv.get("domain")
        if isinstance(domain, str) and domain:
            out.extend(InverterHint(domain, d.get("manufacturer"), d.get("model")) for d in _devices(inv) or [{}])
    return out


def _report_inverter(report: Mapping | None, domain: str) -> Mapping | None:
    return next((i for i in _inverters(report) if i.get("domain") == domain), None)


def _model_ok(profile: Profile, model: Any) -> bool:
    regexes = (profile.raw.get("identify") or {}).get("model_regex") or ()
    return isinstance(model, str) and bool(model) and any(re.search(rx, model) for rx in regexes)


def _belongs(inv: Mapping, profile: Profile) -> bool:
    """Integracja z raportu opisuje TEN falownik: profil wśród kandydatów, marka w producencie
    albo domena z profilu i model zgodny z identyfikacją profilu."""
    if profile.id in (inv.get("profile_candidates") or ()):
        return True
    brand = profile.id.split("-", 1)[0]
    in_profile = ha_integration(profile, inv.get("domain")) is not None
    return any(brand in str(d.get("manufacturer") or "").lower()
               or (in_profile and _model_ok(profile, d.get("model"))) for d in _devices(inv))


def _integration(domain: str, origins: Mapping[str, str]) -> dict:
    out = {"domain": domain, "name": INVERTER_DOMAINS.get(domain, domain)}
    if origins.get(domain) in ORIGINS:
        out["origin"] = origins[domain]
    return out


def _report_device(inv: Mapping | None) -> dict | None:
    dev = next(iter(_devices(inv or {})), None)
    return {"manufacturer": dev.get("manufacturer"), "model": dev.get("model")} if dev else None


def _probe_device(probe, profile: Profile) -> dict:
    words = str(profile.raw.get("label") or "").split()
    return {"manufacturer": words[0] if words else None, "model": probe.identity.model}


def recommend(report: Mapping | None, profiles: Sequence[Profile], probe=None, offer: str | None = None,
              entity_map: Mapping[str, str] | None = None, conflicts: Sequence[str] = (), *,
              choice: ProfileChoice | None = None, origins: Mapping[str, str] | None = None) -> Recommendation:
    """Rekomendacja ścieżki; `choice` = wybór profilu, z którego pochodzi `entity_map`
    (None → wybór ze wskazówek raportu, jak `select_profile`)."""
    if choice is None:
        choice = select_profile(hints_from_report(report), profiles)
    mapped = dict(entity_map or {})
    origins = origins or {}
    found = entry_conflicts(conflicts)
    domain = choice.integration_domain if choice is not None else None

    if choice is not None and domain and mapped.get("mode"):
        inv = _report_inverter(report, domain)
        device = _report_device(inv) or ({"manufacturer": None, "model": choice.model} if choice.model else None)
        start = RUNG_CONTROL_WRITE if control_verified(choice.profile, domain) else RUNG_IDENTIFY
        return Recommendation(ENTITIES, INTEGRATION_WRITE_ENTITIES, start, _integration(domain, origins),
                              device, tuple(sorted(mapped.items())), found)

    ident = getattr(probe, "identity", None)
    probe_profile = next((p for p in profiles if ident is not None and p.id == ident.profile_id), None)
    if probe_profile is not None:
        ready = offer is None and direct_verified(probe_profile) and bool(probe.direct_available)
        start = RUNG_CONTROL_WRITE if ready else RUNG_IDENTIFY
        device = _probe_device(probe, probe_profile)
        inv = next((i for i in _inverters(report) if isinstance(i.get("domain"), str) and _belongs(i, probe_profile)),
                   None)
        if inv is None and choice is not None and domain and choice.profile.id == probe_profile.id:
            inv = _report_inverter(report, domain) or {"domain": domain}
        if inv is not None:
            return Recommendation(DIRECT_WITH_INTEGRATION_DATA, INTEGRATION_READ_ONLY, start,
                                  _integration(inv["domain"], origins), device, (), found)
        return Recommendation(DIRECT, NO_INTEGRATION_IDENTIFY_OK, start, None, device, (), found)

    # Integrację dołączamy tylko, gdy należy do wybranego profilu — nie „pierwszą z brzegu”.
    integration = device = None
    if choice is not None and domain:
        inv = _report_inverter(report, domain)
        integration, device = _integration(domain, origins), _report_device(inv)
    reason = NO_PROFILE if choice is None else NO_WRITE_PATH
    return Recommendation(UNSUPPORTED, reason, RUNG_IDENTIFY, integration, device, (), found)
