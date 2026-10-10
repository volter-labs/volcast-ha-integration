"""Wybór profilu marki dla wykrytego falownika.

1) domena integracji HA opisana w `profile.ha.integrations` i model zgodny z
   `identify.model_regex` → profil z integracją (mapowanie encji; sterowanie tylko
   dla zweryfikowanego wpisu, patrz `control_verified`);
2) producent zawiera markę z id profilu albo domena pasuje → profil tylko do odczytu/podglądu.

Kilka kandydatów dla jednej wskazówki (np. kilka profili tej samej domeny `solarman`)
zawężamy po tekście urządzenia HA: marka z id profilu w producencie, potem
`ha.integrations[].model_regex` na „producent model”, potem `identify.model_regex` na modelu.
Gdy nadal zostaje więcej niż jeden — ta wskazówka nie wybiera nic (`ambiguous_profiles`),
bo zły id profilu w chmurze jest gorszy niż brak profilu.

Profil bez `identify.model_regex` (identyfikacja po rejestrze) trafia wyłącznie do gałęzi 2.
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


def _maker_ok(profile: Profile, hint: InverterHint) -> bool:
    return profile.id.split("-", 1)[0] in (hint.manufacturer or "").lower()


def _ha_text_ok(profile: Profile, hint: InverterHint) -> bool:
    text = " ".join(t for t in (hint.manufacturer, hint.model) if t)
    if not text:
        return False
    for integ in (profile.raw.get("ha") or {}).get("integrations") or ():
        if integ["domain"] == hint.domain and any(re.search(rx, text) for rx in integ.get("model_regex") or ()):
            return True
    return False


def _narrow(cands: list[Profile], hint: InverterHint) -> list[Profile]:
    """Zawęża kandydatów po tekście urządzenia; filtr bez trafień pomijamy."""
    for keep in (lambda p: _maker_ok(p, hint), lambda p: _ha_text_ok(p, hint),
                 lambda p: _model_ok(p, hint.model)):
        if len(cands) <= 1:
            break
        sub = [p for p in cands if keep(p)]
        if sub:
            cands = sub
    return cands


def _entity_cands(hint: InverterHint, ordered: Sequence[Profile]) -> list[Profile]:
    return _narrow([p for p in ordered if hint.domain in _domains(p) and _model_ok(p, hint.model)], hint)


def _read_only_cands(hint: InverterHint, ordered: Sequence[Profile]) -> list[Profile]:
    return _narrow([p for p in ordered if _maker_ok(p, hint) or hint.domain in _domains(p)], hint)


def select_profile(hints: Sequence[InverterHint], profiles: Sequence[Profile]) -> ProfileChoice | None:
    """Najlepsze dopasowanie spośród WSZYSTKICH wskazówek (kilka falowników w domu).

    Kolejność: profil i wpis integracji zweryfikowane (sterowanie encjami) → profil zweryfikowany
    (także tylko do odczytu) → drafty. W obrębie jednego poziomu dopasowanie z integracją przed
    dopasowaniem tylko do odczytu, a potem kolejność wskazówek — dom z samymi draftami dostaje
    to samo co dotąd. Draft innej marki nie może przesłonić zweryfikowanego falownika.
    """
    ordered = sorted(profiles, key=lambda p: p.id)
    found: list[tuple[tuple[int, int, int], ProfileChoice]] = []
    for idx, h in enumerate(hints):
        cands = _entity_cands(h, ordered)
        if len(cands) == 1:
            found.append(((_rank(cands[0], h.domain), 0, idx), ProfileChoice(cands[0], h.domain, h.model)))
        cands = _read_only_cands(h, ordered)
        if len(cands) == 1:
            found.append(((_rank(cands[0], None), 1, idx), ProfileChoice(cands[0], None, h.model)))
    return min(found, key=lambda f: f[0])[1] if found else None


def _rank(profile: Profile, integration_domain: str | None) -> int:
    if control_verified(profile, integration_domain):
        return 0
    return 1 if profile.status == "verified" else 2


def ambiguous_profiles(hints: Sequence[InverterHint], profiles: Sequence[Profile]) -> dict[str, tuple[str, ...]]:
    """Domeny, których wskazówki pasują do kilku profili, a tekst urządzenia ich nie rozstrzyga.

    Dla diagnostyki (atrybuty sensora wykrywania): {domena: (id profili, …)}.
    """
    ordered = sorted(profiles, key=lambda p: p.id)
    out: dict[str, tuple[str, ...]] = {}
    for h in hints:
        if len(_entity_cands(h, ordered)) == 1:
            continue
        cands = _read_only_cands(h, ordered)
        if len(cands) > 1:
            out[h.domain] = tuple(sorted({*out.get(h.domain, ()), *(p.id for p in cands)}))
    return out


def control_verified(profile: Profile, integration_domain: str | None) -> bool:
    if integration_domain is None or profile.status != "verified":
        return False
    return any(i["domain"] == integration_domain and i.get("status") == "verified"
               for i in (profile.raw.get("ha") or {}).get("integrations") or ())
