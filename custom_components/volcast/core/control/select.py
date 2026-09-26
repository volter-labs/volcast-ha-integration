"""Wybór profilu marki dla wykrytego falownika.

1) domena integracji HA opisana w `profile.ha.integrations` i model zgodny z
   `identify.model_regex` → profil z trybem encji;
2) producent zawiera markę z id profilu (albo domena pasuje, a model nie) →
   profil tylko do odczytu/podglądu.

Profil bez `model_regex` (identyfikacja po rejestrze) trafia wyłącznie do gałęzi 2.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from ..profile import Profile


@dataclass(frozen=True)
class InverterHint:
    domain: str
    manufacturer: str | None
    model: str | None


@dataclass(frozen=True)
class ProfileChoice:
    profile: Profile
    integration_domain: str | None
    model: str | None


def _domains(profile: Profile) -> set[str]:
    return {i["domain"] for i in (profile.raw.get("ha") or {}).get("integrations") or ()}


def _model_ok(profile: Profile, model: str | None) -> bool:
    if not model:
        return False
    regexes = (profile.raw.get("identify") or {}).get("model_regex") or ()
    return any(re.search(rx, model) for rx in regexes)


def select_profile(hints: Sequence[InverterHint], profiles: Sequence[Profile]) -> ProfileChoice | None:
    ordered = sorted(profiles, key=lambda p: p.id)
    for h in hints:
        for p in ordered:
            if h.domain in _domains(p) and _model_ok(p, h.model):
                return ProfileChoice(p, h.domain, h.model)
    for h in hints:
        maker = (h.manufacturer or "").lower()
        for p in ordered:
            if p.id.split("-", 1)[0] in maker or h.domain in _domains(p):
                return ProfileChoice(p, None, h.model)
    return None


def control_verified(profile: Profile, integration_domain: str | None) -> bool:
    if integration_domain is None or profile.status != "verified":
        return False
    return any(i["domain"] == integration_domain and i.get("status") == "verified"
               for i in (profile.raw.get("ha") or {}).get("integrations") or ())
