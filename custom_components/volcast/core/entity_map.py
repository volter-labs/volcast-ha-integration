"""Profil → encje HA: rozpoznanie po `platform` + `unique_id` i przekład parametrów
na wywołania usług. Czyste dane — samo wywołanie usług robi warstwa HA.

Encje rozpoznajemy po `unique_id` (stabilne przy zmianie nazw przez użytkownika),
nigdy po `entity_id`. Encja pasująca do regexu więcej niż raz NIE jest mapowana —
zgadywanie adresata zapisu do falownika jest gorsze niż jego brak.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from .params import Params
from .registers import ordered_keys

_UNAVAILABLE = ("unavailable", "unknown", "")
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")

# Jednostki, w których HA może podać stan encji → przelicznik na jednostkę kanoniczną
# klucza (W, kWh, V, A, %). HA potrafi przeliczyć encję na system jednostek
# użytkownika (np. temperatura w °F), więc odczyt musi to odwrócić — inaczej strażnik
# temperatury baterii widzi 82,4 „°C" i trwale blokuje sterowanie.
_LINEAR_UNITS: dict[str, dict[str, float]] = {
    "power": {"W": 1.0, "kW": 1000.0, "MW": 1_000_000.0},
    "energy": {"kWh": 1.0, "Wh": 0.001, "MWh": 1000.0},
    "voltage": {"V": 1.0, "mV": 0.001},
    "current": {"A": 1.0, "mA": 0.001},
    "percent": {"%": 1.0},
}
_TEMPERATURE = {
    "°C": lambda v: v,
    "°F": lambda v: (v - 32.0) * 5.0 / 9.0,
    "K": lambda v: v - 273.15,
}


@dataclass(frozen=True)
class EntityCandidate:
    entity_id: str
    platform: str
    unique_id: str


@dataclass(frozen=True)
class ResolveResult:
    mapped: dict[str, str]
    missing: list[str]
    ambiguous: dict[str, list[str]]


@dataclass(frozen=True)
class EntityWrite:
    key: str
    entity_id: str
    domain: str
    service: str
    data: dict


def _integration(profile, domain: str) -> dict:
    for integ in profile.raw["ha"]["integrations"]:
        if integ["domain"] == domain:
            return integ
    raise KeyError(f"profil {profile.id} nie opisuje integracji {domain!r}")


def resolve_entities(profile, integration_domain: str,
                     candidates: Iterable[EntityCandidate]) -> ResolveResult:
    ents = _integration(profile, integration_domain)["entities"]
    pool = [c for c in candidates if c.platform == integration_domain]
    mapped: dict[str, str] = {}
    missing: list[str] = []
    ambiguous: dict[str, list[str]] = {}
    for key, spec in ents.items():
        rx = re.compile(spec["unique_id_regex"])
        hits = sorted(c.entity_id for c in pool
                      if c.entity_id.split(".", 1)[0] == spec["domain"] and rx.search(c.unique_id))
        if len(hits) == 1:
            mapped[key] = hits[0]
        elif not hits:
            missing.append(key)
        else:
            ambiguous[key] = hits
    return ResolveResult(mapped, missing, ambiguous)


def entity_writes(params: Params, profile, integration_domain: str, mapped: Mapping[str, str],
                  keys: Iterable[str] | None = None) -> tuple[list[EntityWrite], list[str]]:
    """Zapisy (w kolejności profilu) i klucze, dla których nie ma zmapowanej encji."""
    ents = _integration(profile, integration_domain)["entities"]
    wanted = None if keys is None else set(keys)
    writes: list[EntityWrite] = []
    unmapped: list[str] = []
    for key in ordered_keys(params, profile):
        if wanted is not None and key not in wanted:
            continue
        ha_key = key.replace(".", "_") if key.startswith("tou.") else key
        if ha_key not in mapped or ha_key not in ents:
            unmapped.append(key)
            continue
        spec, eid = ents[ha_key], mapped[ha_key]
        domain = spec["domain"]
        if key.startswith("tou."):
            _, idx, field = key.split(".")
            prog = params.tou[int(idx) - 1]
            raw = {"start": prog.start_min, "power_w": prog.power_w, "soc": prog.soc,
                   "grid_charge": prog.grid_charge}[field]
        else:
            raw = getattr(params, key)
        if domain == "select":
            writes.append(EntityWrite(key, eid, domain, "select_option",
                                      {"option": profile.modes[raw].ha_option}))
        elif domain == "switch":
            writes.append(EntityWrite(key, eid, domain, "turn_on" if raw else "turn_off", {}))
        elif domain == "time":
            minutes = int(raw)
            writes.append(EntityWrite(key, eid, domain, "set_value",
                                      {"time": f"{minutes // 60:02d}:{minutes % 60:02d}:00"}))
        else:
            value = float(raw)
            if spec.get("transform") == "invert_percent":
                # Encja GoodWe to GŁĘBOKOŚĆ rozładowania: próg 20 % = DoD 80 %.
                value = round(100.0 - value, 1)
            writes.append(EntityWrite(key, eid, domain, "set_value", {"value": value}))
    return writes, unmapped


def _quantity(key: str) -> str | None:
    """Wielkość fizyczna klucza z jego nazwy (sufiks = jednostka kanoniczna)."""
    if key.endswith("_temp_c"):
        return "temperature"
    if key.endswith("_kwh"):
        return "energy"
    if key.endswith("_w"):
        return "power"
    if key.endswith("_v"):
        return "voltage"
    if key.endswith("_a"):
        return "current"
    if key in ("soc", "soc_min", "soc_max") or key.endswith("_soc"):
        return "percent"
    return None


def _to_canonical(key: str, value: float, unit: str | None) -> float | None:
    """Przelicza na jednostkę kanoniczną klucza; nieznana/niezgodna jednostka → None.

    Brak jednostki (None/"") = zakładamy jednostkę kanoniczną — tak raportuje
    większość integracji. Jednostka podana, ale obca (np. „W" dla SoC) to błąd
    mapowania: lepiej nie mieć odczytu niż podać strażnikowi złą liczbę.
    """
    quantity = _quantity(key)
    if quantity is None or not unit:
        return value
    if quantity == "temperature":
        conv = _TEMPERATURE.get(unit)
        return None if conv is None else conv(value)
    factor = _LINEAR_UNITS[quantity].get(unit)
    return None if factor is None else value * factor


def _time_to_minutes(state: str) -> float | None:
    m = _TIME_RE.match(state)
    if m is None:
        return None
    h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    if h > 23 or mi > 59 or s > 59:
        return None
    return float(h * 60 + mi)


def entity_value(key: str, state: str, profile, integration_domain: str,
                 *, unit: str | None = None) -> float | str | None:
    """Stan encji HA → wartość w konwencji profilu (jak `Params.flatten`/odczyt rejestrów).

    `unit` to `unit_of_measurement` encji; przeliczamy na jednostkę kanoniczną klucza
    PRZED transformacją profilu. Przełącznik: 1.0/0.0, czas: minuta doby, wybór: nazwa
    trybu z profilu. Cokolwiek nieczytelnego → None (brak odczytu, nie zero).
    """
    spec = _integration(profile, integration_domain)["entities"][key]
    if state in _UNAVAILABLE:
        return None
    domain = spec["domain"]
    if domain == "select":
        for m in profile.modes.values():
            if m.ha_option == state:
                return m.name
        return None   # opcja spoza profilu = obce przejęcie albo nieznany tryb
    if domain == "switch":
        return {"on": 1.0, "off": 0.0}.get(state)
    if domain == "time":
        return _time_to_minutes(state)
    try:
        value = float(state)
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    value = _to_canonical(key, value, unit)
    if value is None:
        return None
    if spec.get("transform") == "negate":
        return -value
    if spec.get("transform") == "invert_percent":
        return round(100.0 - value, 1)
    return value
