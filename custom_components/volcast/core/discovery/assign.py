"""Propozycja przypisania czujników do pól telemetrii („Installation details”). Czyste funkcje, bez HA.

Dla każdego pola zwraca ranking kandydatów z oceną `high | medium | low` i powodami. Propozycja niczego
nie zapisuje — wybór zatwierdza użytkownik. Pole jest „pewne” (`chosen`) tylko przy dokładnie jednym
kandydacie `high`; przy kilku albo żadnym zostaje sama lista.

Punktacja (stałe `_PTS_*`): zgodna klasa urządzenia, jednostka i klasa stanu; słowo pola w nazwie encji
(`entity_id` bez slugu urządzenia, nazwa, `unique_id`, `translation_key`) albo w nazwie urządzenia; kary
za słowa innego pola, okres licznika (dzień, miesiąc), część instalacji (string, faza) i nastawy/prognozy.
Ocenę liczymy z punktów encji; bonusy za urządzenie falownika i wspólne urządzenie z polami już pewnymi
tylko porządkują listę (nie podnoszą oceny), więc nie rozstrzygają za użytkownika między dwoma falownikami.
Znak mocy nie jest tu oceniany (osobny moduł z historii odczytów).
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from ..control.conflict import SELF_DOMAIN
from ..entity_map import _LINEAR_UNITS
from .charger import _attr, _device_class, _slug, _unit
from .models import DeviceSnap, EntitySnap, InverterFinding, StateSnap

# pola formularza „Installation details” — ta sama kolejność co `TELEMETRY_FIELDS` w control/telemetry.py
# (rdzeń nie importuje control/; zgodność pilnuje test)
FIELDS: tuple[str, ...] = (
    "soc", "pv_power_w", "battery_power_w", "grid_power_w", "load_power_w",
    "pv_energy_total_kwh", "grid_import_total_kwh", "grid_export_total_kwh",
)
# limit długości listy kandydatów na pole (formularz i odpowiedź usługi)
MAX_CANDIDATES = 6
HIGH, MEDIUM, LOW = "high", "medium", "low"
_RANK = {HIGH: 0, MEDIUM: 1, LOW: 2}

# punkty dowodów
_PTS_DEVICE_CLASS = 2
_PTS_UNIT = 2
_PTS_STATE_CLASS = 1
_PTS_SUBJECT = 3       # słowo pola (np. „pv”, „grid”, „import”)
_PTS_HINT = 1          # słowo wspierające (np. „total”), raz na encję
_PTS_PENALTY = 3       # słowo innego pola, okres licznika, nastawa/prognoza
_PTS_PART = 2          # część instalacji (pv1, string, l1) — suma całości jest lepsza
_PTS_BONUS = 1         # porządek listy: urządzenie falownika, wspólne urządzenie z polem pewnym
# progi oceny (z punktów encji, bez bonusów porządku):
# high = słowo pola + co najmniej jedna zgodna cecha wielkości + klasa stanu, bez żadnej kary
_HIGH_MIN = 6
# medium bez słowa pola = pełna zgodność wielkości (klasa, jednostka, klasa stanu) + słowo wspierające
_MEDIUM_MIN = 6

# platformy, których encje nie są pomiarem instalacji: sama wtyczka i prognozy PV
_SKIP_PLATFORMS: frozenset[str] = frozenset({SELF_DOMAIN, "forecast_solar", "solcast_solar",
                                             "open_meteo_solar_forecast"})


@dataclass(frozen=True)
class _Quantity:
    device_class: str
    units: frozenset[str]
    state_classes: frozenset[str]


# jednostki z tej samej tabeli co przeliczanie odczytu ręcznego (entity_map), więc każda propozycja da się odczytać
_POWER = _Quantity("power", frozenset(_LINEAR_UNITS["power"]), frozenset({"measurement"}))
_SOC = _Quantity("battery", frozenset(_LINEAR_UNITS["percent"]), frozenset({"measurement"}))
_ENERGY = _Quantity("energy", frozenset(_LINEAR_UNITS["energy"]), frozenset({"total_increasing", "total"}))

# słowa (całe słowa slugu, dozwolona końcówka cyfrowa: „pv1”, „bat2”)
_PV = ("pv", "solar", "photovoltaic", "photovoltaics", "production", "generation", "yield",
       "input_power", "dc_power")
_BATTERY = ("battery", "batt", "bat")
_GRID = ("grid", "meter", "utility")
_LOAD = ("load", "consumption", "house", "home", "household")
_SOC_WORDS = ("soc", "state_of_charge", "state_of_capacity")
_IMPORT = ("import", "imported", "purchase", "purchased", "bought", "buy", "from_grid")
_EXPORT = ("export", "exported", "returned", "feed", "sold", "sell", "injection", "injected", "to_grid")
_CHARGE = ("charge", "charging", "discharge", "discharging")
_TOTAL = ("total", "lifetime", "accumulated")
_PERIOD = ("today", "daily", "day", "yesterday", "week", "weekly", "month", "monthly", "year", "yearly",
           "hour", "hourly")
_AUX = ("min", "max", "limit", "target", "reserve", "cutoff", "setpoint", "forecast", "estimated",
        "predicted", "expected", "average", "avg", "peak", "soh", "health")
_PART_RE = re.compile(r"(?:^|_)((?:pv|string|mppt|l|phase)\d+|string|mppt|phase)(?=_|$)")


@dataclass(frozen=True)
class _Spec:
    quantity: _Quantity
    subject: tuple[str, ...]
    hints: tuple[str, ...] = ()
    foreign: tuple[str, ...] = ()
    lifetime: bool = False      # licznik narastający: kara za okres (dzień, miesiąc)


_SPECS: dict[str, _Spec] = {
    "soc": _Spec(_SOC, _SOC_WORDS),
    "pv_power_w": _Spec(_POWER, _PV, foreign=_BATTERY + _GRID + _LOAD + _CHARGE + _IMPORT + _EXPORT),
    "battery_power_w": _Spec(_POWER, _BATTERY, foreign=_PV + _GRID + _LOAD),
    "grid_power_w": _Spec(_POWER, _GRID, foreign=_PV + _BATTERY + _LOAD + _CHARGE),
    "load_power_w": _Spec(_POWER, _LOAD, foreign=_PV + _BATTERY + _GRID + _CHARGE + _IMPORT + _EXPORT),
    "pv_energy_total_kwh": _Spec(_ENERGY, _PV, hints=_TOTAL,
                                 foreign=_BATTERY + _GRID + _LOAD + _CHARGE + _IMPORT + _EXPORT, lifetime=True),
    "grid_import_total_kwh": _Spec(_ENERGY, _IMPORT, hints=_TOTAL + _GRID,
                                   foreign=_EXPORT + _PV + _BATTERY + _CHARGE, lifetime=True),
    "grid_export_total_kwh": _Spec(_ENERGY, _EXPORT, hints=_TOTAL + _GRID,
                                   foreign=_IMPORT + _PV + _BATTERY + _CHARGE, lifetime=True),
}


_DIGITS = "0123456789"


def _find(words: tuple[str, ...], text: str) -> str | None:
    """Pierwsze (najbardziej z lewej) słowo z grupy w slugu: całe słowa, fraza wielowyrazowa jako kolejne
    słowa, ostatnie słowo z dozwoloną końcówką cyfrową („pv1”). Bez wyrażeń regularnych budowanych z danych."""
    if not words or not text:
        return None
    tokens = text.split("_")
    bare = [t.rstrip(_DIGITS) or t for t in tokens]
    for i in range(len(tokens)):
        for w in words:
            parts = w.split("_")
            last = i + len(parts) - 1
            if last < len(tokens) and tokens[i:last] == parts[:-1] and bare[last] == parts[-1]:
                return w
    return None


# ── wynik ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Candidate:
    """Kandydat na pole. `score` = punkty encji + bonusy porządku; `reasons` = kody `rodzaj[:szczegół]`."""
    entity_id: str
    confidence: str             # "high" | "medium" | "low"
    score: int
    reasons: tuple[str, ...]
    device_id: str | None = None
    name: str | None = None
    unit: str | None = None

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["reasons"] = list(self.reasons)
        return d


@dataclass(frozen=True)
class FieldProposal:
    """Ranking dla jednego pola; `chosen` tylko przy dokładnie jednym kandydacie `high`."""
    field: str
    candidates: tuple[Candidate, ...]
    chosen: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"field": self.field, "chosen": self.chosen,
                "candidates": [c.as_dict() for c in self.candidates]}


@dataclass(frozen=True)
class Proposal:
    fields: tuple[FieldProposal, ...]

    def field(self, name: str) -> FieldProposal:
        return next(fp for fp in self.fields if fp.field == name)

    def chosen(self) -> dict[str, str]:
        """Pola pewne jako gotowa mapa telemetrii {pole: entity_id}."""
        return {fp.field: fp.chosen for fp in self.fields if fp.chosen}

    def as_dict(self) -> dict[str, Any]:
        return {"fields": {fp.field: fp.as_dict() for fp in self.fields}, "chosen": self.chosen()}


# ── ocena ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _Texts:
    own: str          # slug: entity_id bez slugu urządzenia, nazwa, unique_id, translation_key
    device: str       # slug nazwy urządzenia
    name: str | None  # nazwa do pokazania


def _texts(e: EntitySnap, states: Mapping[str, StateSnap], dev: DeviceSnap | None) -> _Texts:
    slug = (dev.slug if dev.slug is not None else _slug(dev.name or "")) if dev else ""
    prefix = f"{slug}_" if slug else ""
    st = states.get(e.entity_id)
    name = e.original_name or (st.attributes.get("friendly_name") if st else None)
    # nazwa urządzenia na początku entity_id / nazwy encji to nie słowo pola („grid_meter_power” → „power”)
    parts = (e.entity_id.split(".", 1)[-1].removeprefix(prefix), _slug(name or "").removeprefix(prefix),
             _slug(e.unique_id or ""), _slug(e.translation_key or ""))
    return _Texts("_".join(p for p in parts if p), slug, name)


def _quantity_points(q: _Quantity, e: EntitySnap,
                     states: Mapping[str, StateSnap]) -> tuple[int, list[str], str | None] | None:
    """Zgodność wielkości: None = encja nie nadaje się do pola (inna klasa albo jednostka, albo brak obu)."""
    dc = _device_class(e, states)
    unit = _unit(e, states) or None
    if (dc is not None and dc != q.device_class) or (unit is not None and unit not in q.units):
        return None
    if dc is None and unit is None:
        return None
    pts, reasons = 0, []
    if dc is not None:
        pts += _PTS_DEVICE_CLASS
        reasons.append(f"device_class:{dc}")
    if unit is not None:
        pts += _PTS_UNIT
        reasons.append(f"unit:{unit}")
    sc = _attr(e, states, "state_class")
    if sc in q.state_classes:
        pts += _PTS_STATE_CLASS
        reasons.append(f"state_class:{sc}")
    return pts, reasons, unit


def _subjects(text: str) -> set[str]:
    return {f for f, s in _SPECS.items() if _find(s.subject, text)}


def _score(field: str, spec: _Spec, t: _Texts, own_subjects: set[str]) -> tuple[int, list[str], bool, bool]:
    """Punkty słów: (punkty, powody, jest słowo pola, jest kara)."""
    pts, reasons, penalized = 0, [], False
    word = _find(spec.subject, t.own)
    subject = word is not None
    if subject:
        pts += _PTS_SUBJECT
        reasons.append(f"name:{word}")
    elif not (own_subjects - {field}) and (word := _find(spec.subject, t.device)):
        # nazwa urządzenia („Battery”, „Grid meter”) liczy się tylko dla encji o nazwie ogólnej
        # („Power”, „Charge/Discharge power”), nie gdy encja sama wskazuje inne pole
        subject = True
        pts += _PTS_SUBJECT
        reasons.append(f"device_name:{word}")
    if word := (_find(spec.hints, t.own) or _find(spec.hints, t.device)):
        pts += _PTS_HINT
        reasons.append(f"hint:{word}")
    penalties = [("other_field", _find(spec.foreign, t.own), _PTS_PENALTY),
                 ("period", _find(_PERIOD, t.own) if spec.lifetime else None, _PTS_PENALTY),
                 ("aux", _find(_AUX, t.own), _PTS_PENALTY)]
    part = _PART_RE.search(t.own)
    penalties.append(("part", part.group(1) if part else None, _PTS_PART))
    for kind, hit, cost in penalties:
        if hit:
            pts -= cost
            penalized = True
            reasons.append(f"{kind}:{hit}")
    return pts, reasons, subject, penalized


def _confidence(points: int, subject: bool, penalized: bool) -> str:
    if subject:
        return HIGH if not penalized and points >= _HIGH_MIN else MEDIUM
    return MEDIUM if not penalized and points >= _MEDIUM_MIN else LOW


def _eligible(e: EntitySnap) -> bool:
    return not e.disabled and e.entity_id.startswith("sensor.") and e.platform not in _SKIP_PLATFORMS


def propose(
    entities: Sequence[EntitySnap],
    states: Mapping[str, StateSnap],
    devices: Sequence[DeviceSnap] = (),
    inverters: Sequence[InverterFinding] = (),
    *,
    fields: Sequence[str] = FIELDS,
    limit: int = MAX_CANDIDATES,
) -> Proposal:
    """Ranking kandydatów dla każdego pola z `fields` (domyślnie wszystkie pola telemetrii).

    `inverters` — znaleziska falowników z `classify` (bonus porządku za urządzenie falownika).
    Nieznane pole → ValueError."""
    unknown = [f for f in fields if f not in _SPECS]
    if unknown:
        raise ValueError(f"unknown telemetry field(s): {', '.join(unknown)}")
    by_device = {d.id: d for d in devices}
    inv_devices = {d.id for f in inverters for d in f.devices}
    inv_entities = {e.entity_id for f in inverters for e in f.entities}

    # przebieg 1: punkty encji i ocena
    scored: dict[str, list[Candidate]] = {f: [] for f in fields}
    for e in entities:
        if not _eligible(e):
            continue
        t = _texts(e, states, by_device.get(e.device_id or ""))
        own_subjects = _subjects(t.own)
        for field in fields:
            spec = _SPECS[field]
            q = _quantity_points(spec.quantity, e, states)
            if q is None:
                continue
            q_pts, q_reasons, unit = q
            w_pts, w_reasons, subject, penalized = _score(field, spec, t, own_subjects)
            points = q_pts + w_pts
            cand = Candidate(entity_id=e.entity_id, confidence=_confidence(points, subject, penalized),
                             score=points, reasons=tuple(q_reasons + w_reasons), device_id=e.device_id,
                             name=t.name, unit=unit)
            scored[field].append(cand)

    sure: dict[str, Candidate] = {}
    for field, items in scored.items():
        highs = [c for c in items if c.confidence == HIGH]
        if len(highs) == 1:
            sure[field] = highs[0]
    # urządzenie pola pewnego („lider”) porządkuje kandydatów innych pól
    leaders = {f: c.device_id for f, c in sure.items() if c.device_id}

    # przebieg 2: bonusy porządku (bez zmiany oceny), sortowanie i limit
    out: list[FieldProposal] = []
    for field in fields:
        ranked: list[Candidate] = []
        for c in scored[field]:
            bonus, extra = 0, []
            if c.device_id in inv_devices or c.entity_id in inv_entities:
                bonus += _PTS_BONUS
                extra.append("inverter_device")
            shared = sorted(f for f, dev in leaders.items() if f != field and dev == c.device_id)
            if shared:
                bonus += _PTS_BONUS
                extra.append("same_device:" + ",".join(shared))
            ranked.append(Candidate(entity_id=c.entity_id, confidence=c.confidence, score=c.score + bonus,
                                    reasons=c.reasons + tuple(extra), device_id=c.device_id,
                                    name=c.name, unit=c.unit))
        ranked.sort(key=lambda c: (_RANK[c.confidence], -c.score, c.entity_id))
        out.append(FieldProposal(field=field, candidates=tuple(ranked[:limit]),
                                 chosen=sure[field].entity_id if field in sure else None))
    return Proposal(fields=tuple(out))
