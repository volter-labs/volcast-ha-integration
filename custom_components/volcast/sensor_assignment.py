"""Propozycja przypisania czujników („Installation details”) — warstwa Home Assistant, tylko odczyt.

`async_propose(hass, entry)` zbiera migawki rejestrów i stanów (te same funkcje co wykrywanie),
woła czysty ranking kandydatów (`core.discovery.assign.propose`) i ocenę znaku mocy sieci i baterii
(`core.discovery.sign_check.check_signs`) na 24 h statystyk 5-minutowych z rejestratora. Bez
rejestratora, bez statystyk albo przy błędzie bierze jedną próbkę z bieżących stanów (ocena wtedy
uczciwie bez zalecenia). Niczego nie zapisuje: ani opcji wpisu, ani do falownika, nie woła usług.
Żaden wyjątek nie wychodzi poza `async_propose` — wynik zawsze jest, w najgorszym razie pusty.

Ocena dotyczy SUROWYCH czujników: bieżące flagi odwrócenia znaku z opcji nie są nakładane na próbki,
wynik mówi, jakie flagi powinny być; bieżące flagi są obok w wyniku (formularz pokazuje różnicę).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping

import homeassistant.util.dt as dt_util
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.helpers import entity_registry as er

from .const import OPT_BATTERY_NEGATE, OPT_GRID_NEGATE, OPT_TELEMETRY_MAP
from .core.discovery import EntitySnap, StateSnap, classify
from .core.discovery.assign import FIELDS, FieldProposal, Proposal, propose
from .core.discovery.sign_check import BATTERY, GRID, LOAD, QUANTITIES, SignCheck, check_signs
from .core.entity_map import canonical_value
from .discovery_runner import snap_devices, snap_entities, snap_entries

_LOGGER = logging.getLogger(__name__)

# podstawa oceny znaku
BASIS_HISTORY = "history"     # statystyki 5-minutowe z doby
BASIS_CURRENT = "current"     # jedna próbka z bieżących stanów (zapas)
BASIS_NONE = "none"           # nie ma czego oceniać (brak czujnika sieci i baterii) albo awaria

# okno i rozdzielczość historii: doba statystyk 5-minutowych (288 próbek) — obejmuje dzień i noc
# oraz ładowanie i rozładowanie baterii; ocena znaku liczy się tylko z przypisanym czujnikiem
HISTORY_WINDOW = timedelta(hours=24)
STATISTICS_PERIOD = "5minute"
# jawna jednostka: rejestrator przelicza każdą statystykę mocy na W, niezależnie od jednostki zapisanej
# i bieżącej jednostki stanu (bez tego skala zależałaby od niejawnej konwersji i mogła wyjść ×1000)
STATISTICS_UNITS = {"power": "W"}
# górna granica czekania na rejestrator — formularz opcji nie może wisieć na zajętej bazie
HISTORY_TIMEOUT_S = 15.0

# kody notatek (dlaczego zapas albo niepełny obraz); bez danych użytkownika
NOTE_RECORDER_UNAVAILABLE = "recorder_unavailable"
NOTE_NO_STATISTICS = "no_statistics"
NOTE_RECORDER_ERROR = "recorder_error"
NOTE_REGISTRY_UNAVAILABLE = "registry_unavailable"
NOTE_FAILED = "failed"

# kody wyłączenia czujnika zużycia z bilansu
EXCLUDED_LOAD_DERIVED = "load_derived"          # platforma wyliczeniowa
EXCLUDED_LOAD_STATE_ONLY = "load_state_only"    # encja spoza rejestru (zwykle szablon YAML bez unique_id)
EXCLUDED_LOAD_UNVERIFIED = "load_unverified"    # rejestr niedostępny — platformy nie da się sprawdzić

# Platformy, których czujnik mocy bywa SUMĄ innych czujników. Zużycie policzone jako `PV + sieć +
# bateria` z tych samych surowych odczytów domyka bilans dla konwencji, w której je policzono —
# także złej — więc ocena znaku byłaby z góry „potwierdzona”. Takiego zużycia nie podajemy do bilansu.
# - template: szablon (YAML lub pomocnik UI) — typowe „zużycie domu” liczone z pozostałych czujników;
# - min_max: pomocnik „Combine the state of several sensors” (typ `sum`);
# - group: grupa czujników z typem `sum` (HA 2023.x+).
# Poza listą (mierzą albo przekształcają JEDNO źródło, nie domykają bilansu): utility_meter (licznik
# energii jednego źródła, kWh — nie moc), integration (całka Riemanna → energia), derivative
# (pochodna jednego licznika), statistics/filter/compensation (wygładzanie lub korekta jednego czujnika).
DERIVED_PLATFORMS: frozenset[str] = frozenset({"template", "min_max", "group"})

ORIGIN_MAP = "map"            # encja z bieżącej mapy wpisu
ORIGIN_PROPOSAL = "proposal"  # kandydat pewny z propozycji (pole mapy puste)


# ── wynik ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AssignmentResult:
    """Propozycja przypisania + ocena znaku + bieżący stan opcji wpisu.

    `sources` — encje użyte w ocenie znaku (pole → entity_id), `source_origin` — skąd (`map` |
    `proposal`); `excluded` — pola pominięte w bilansie z kodem powodu; `samples` — liczba próbek
    podanych do oceny (znaczniki czasu historii albo 1), użyteczne próbki są w `sign_check.samples`."""
    proposal: Proposal
    telemetry_map: dict[str, str]
    grid_negate: bool
    battery_negate: bool
    sign_check: SignCheck
    basis: str
    samples: int
    sources: dict[str, str]
    source_origin: dict[str, str]
    excluded: dict[str, str]
    notes: tuple[str, ...] = ()

    def flag_changes(self) -> dict[str, bool]:
        """Flagi znaku, które według oceny powinny być inne niż bieżące ({opcja: zalecana wartość})."""
        out: dict[str, bool] = {}
        for option, verdict, current in ((OPT_GRID_NEGATE, self.sign_check.grid, self.grid_negate),
                                         (OPT_BATTERY_NEGATE, self.sign_check.battery, self.battery_negate)):
            if verdict.negate is not None and verdict.negate != current:
                out[option] = verdict.negate
        return out

    def as_dict(self) -> dict[str, Any]:
        """Czyste dane (JSON) — odpowiedź usługi i placeholdery formularza."""
        return {
            "proposal": self.proposal.as_dict(),
            "telemetry_map": dict(self.telemetry_map),
            "flags": {OPT_GRID_NEGATE: self.grid_negate, OPT_BATTERY_NEGATE: self.battery_negate},
            "sign_check": self.sign_check.as_dict(),
            "basis": self.basis,
            "samples": self.samples,
            "sources": dict(self.sources),
            "source_origin": dict(self.source_origin),
            "excluded": dict(self.excluded),
            "flag_changes": self.flag_changes(),
            "notes": list(self.notes),
        }


# ── odczyt opcji ────────────────────────────────────────────────────────────

def _current_options(entry: Any) -> tuple[dict[str, str], bool, bool]:
    """Kopia mapy telemetrii i bieżące flagi znaku (wynik nie dzieli obiektów z opcjami wpisu)."""
    options = getattr(entry, "options", None) or {}
    raw = options.get(OPT_TELEMETRY_MAP) or {}
    tmap = {str(k): v for k, v in raw.items() if isinstance(v, str) and v} if isinstance(raw, Mapping) else {}
    return tmap, bool(options.get(OPT_GRID_NEGATE)), bool(options.get(OPT_BATTERY_NEGATE))


# ── migawki ─────────────────────────────────────────────────────────────────

@dataclass
class _Snapshot:
    entities: list[EntitySnap]
    states: dict[str, StateSnap]
    devices: list
    inverters: list
    # platforma każdej encji rejestru; None = rejestr niedostępny
    platforms: dict[str, str | None] | None
    notes: list[str]


def _state_only(st: StateSnap) -> EntitySnap:
    """Encja znana tylko z maszyny stanów (szablon YAML bez unique_id): platforma nieznana."""
    a = st.attributes
    return EntitySnap(
        entity_id=st.entity_id, platform=None, unique_id="", device_id=None, config_entry_id=None,
        device_class=a.get("device_class"), unit=a.get("unit_of_measurement"), translation_key=None,
        original_name=None, disabled=False)


def _snapshot(hass) -> _Snapshot:
    notes: list[str] = []
    try:
        devices, skipped = snap_devices(hass)
    except Exception as err:  # noqa: BLE001 — bez urządzeń ranking działa, tylko bez porządku po urządzeniu
        _LOGGER.debug("Volcast sensor assignment: device registry unavailable (%s)", type(err).__name__)
        devices, skipped = [], frozenset()
    platforms: dict[str, str | None] | None
    try:
        entities = snap_entities(hass, skipped)
        platforms = {e.entity_id: e.platform for e in er.async_get(hass).entities.values()}
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Volcast sensor assignment: entity registry unavailable (%s)", type(err).__name__)
        entities, platforms = [], None
        notes.append(NOTE_REGISTRY_UNAVAILABLE)
    try:
        states = {s.entity_id: StateSnap(s.entity_id, s.state, dict(s.attributes))
                  for s in hass.states.async_all("sensor")}
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Volcast sensor assignment: states unavailable (%s)", type(err).__name__)
        states = {}
    known = platforms or {}
    entities = entities + [_state_only(st) for eid, st in states.items() if eid not in known]
    try:
        # znaleziska falowników tylko porządkują listę kandydatów (bonus za urządzenie falownika)
        inverters = classify(devices, entities, snap_entries(hass), {}).inverters
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Volcast sensor assignment: classification failed (%s)", type(err).__name__)
        inverters = []
    return _Snapshot(entities, states, devices, inverters, platforms, notes)


# ── źródła oceny znaku ──────────────────────────────────────────────────────

def _sources(tmap: Mapping[str, str], proposal: Proposal,
             platforms: Mapping[str, str | None] | None) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Encja na każdą z czterech wielkości: z mapy, a przy pustym polu kandydat pewny. Zużycie
    z platformy wyliczeniowej albo spoza rejestru nie trafia do bilansu (kod w `excluded`)."""
    chosen = proposal.chosen()
    sources: dict[str, str] = {}
    origin: dict[str, str] = {}
    excluded: dict[str, str] = {}
    for q in QUANTITIES:
        if tmap.get(q):
            eid, src = tmap[q], ORIGIN_MAP
        elif chosen.get(q):
            eid, src = chosen[q], ORIGIN_PROPOSAL
        else:
            continue
        if q == LOAD:
            if platforms is None:
                excluded[q] = EXCLUDED_LOAD_UNVERIFIED
                continue
            if eid not in platforms:
                excluded[q] = EXCLUDED_LOAD_STATE_ONLY
                continue
            if platforms[eid] in DERIVED_PLATFORMS:
                excluded[q] = EXCLUDED_LOAD_DERIVED
                continue
        sources[q], origin[q] = eid, src
    return sources, origin, excluded


def _unit(eid: str, snap: _Snapshot) -> str | None:
    """Jednostka bieżącego odczytu czujnika: stan, potem rejestr (historia przychodzi już w W)."""
    st = snap.states.get(eid)
    if st is not None and st.attributes.get("unit_of_measurement"):
        return st.attributes["unit_of_measurement"]
    return next((e.unit for e in snap.entities if e.entity_id == eid and e.unit), None)


def _ts(start: Any) -> float | None:
    """Znacznik czasu wiersza statystyk (HA 2023.3+: liczba; starsze: datetime)."""
    if isinstance(start, datetime):
        return start.timestamp()
    if isinstance(start, (int, float)) and not isinstance(start, bool):
        return float(start)
    return None


def _current_sample(sources: Mapping[str, str], units: Mapping[str, str | None],
                    snap: _Snapshot) -> list[dict[str, float | None]]:
    # klucz każdego przypisanego czujnika obecny także bez wartości (kontrakt `check_signs`)
    sample: dict[str, float | None] = {}
    for q, eid in sources.items():
        st = snap.states.get(eid)
        sample[q] = canonical_value(q, st.state if st else None, units[q])
    return [sample]


async def _history(hass, sources: Mapping[str, str]) -> tuple[list[dict[str, float | None]] | None, str | None]:
    """Próbki wyrównane po znaczniku czasu albo (None, kod notatki) — wtedy zapas z bieżących stanów."""
    if "recorder" not in getattr(hass.config, "components", ()):
        return None, NOTE_RECORDER_UNAVAILABLE
    start = dt_util.utcnow() - HISTORY_WINDOW
    try:
        # jedno zapytanie dla wszystkich encji, w wątku rejestratora (pętla zdarzeń nie czeka na bazę)
        rows = await asyncio.wait_for(
            get_instance(hass).async_add_executor_job(
                statistics_during_period, hass, start, None, set(sources.values()),
                STATISTICS_PERIOD, STATISTICS_UNITS, {"mean"}),
            HISTORY_TIMEOUT_S)
    except Exception as err:  # noqa: BLE001 — także TimeoutError; bez treści wyjątku (ścieżki, dane)
        _LOGGER.debug("Volcast sensor assignment: recorder query failed (%s)", type(err).__name__)
        return None, NOTE_RECORDER_ERROR
    series: dict[str, dict[float, float | None]] = {}
    for q, eid in sources.items():
        points: dict[float, float | None] = {}
        for row in (rows or {}).get(eid) or ():
            ts = _ts(row.get("start")) if isinstance(row, Mapping) else None
            if ts is not None:
                mean = row.get("mean")
                points[ts] = None if mean is None else canonical_value(q, str(mean), "W")
        if not any(v is not None for v in points.values()):
            return None, NOTE_NO_STATISTICS
        series[q] = points
    stamps = sorted({ts for points in series.values() for ts in points})
    return [{q: series[q].get(ts) for q in sources} for ts in stamps], None


# ── wejście ─────────────────────────────────────────────────────────────────

def _empty_proposal() -> Proposal:
    return Proposal(fields=tuple(FieldProposal(field=f, candidates=()) for f in FIELDS))


async def async_propose(hass, entry, *, with_proposal: bool = True) -> AssignmentResult:
    """Propozycja przypisania czujników i ocena znaku dla wpisu. Tylko odczyt; nigdy nie rzuca.

    `with_proposal=False` — bez kandydatów: źródła i ocena znaku dotyczą wyłącznie czujników zapisanych
    w mapie wpisu (gdy podpowiedź jest niedozwolona, np. falownik rozpoznany)."""
    try:
        return await _async_propose(hass, entry, with_proposal)
    except Exception as err:  # noqa: BLE001 — formularz i usługa muszą dostać wynik
        _LOGGER.debug("Volcast sensor assignment failed (%s)", type(err).__name__)
    try:
        tmap, grid_negate, battery_negate = _current_options(entry)
    except Exception:  # noqa: BLE001
        tmap, grid_negate, battery_negate = {}, False, False
    return AssignmentResult(
        proposal=_empty_proposal(), telemetry_map=tmap, grid_negate=grid_negate,
        battery_negate=battery_negate, sign_check=check_signs([]), basis=BASIS_NONE, samples=0,
        sources={}, source_origin={}, excluded={}, notes=(NOTE_FAILED,))


async def _async_propose(hass, entry, with_proposal: bool = True) -> AssignmentResult:
    tmap, grid_negate, battery_negate = _current_options(entry)
    snap = _snapshot(hass)
    proposal = propose(snap.entities, snap.states, snap.devices, snap.inverters) if with_proposal \
        else Proposal(())
    sources, origin, excluded = _sources(tmap, proposal, snap.platforms)
    notes = list(snap.notes)

    samples: list[dict[str, float | None]]
    if GRID not in sources and BATTERY not in sources:
        # bez czujnika sieci i baterii nie ma znaku do oceny — rejestratora nie pytamy
        samples, basis = [], BASIS_NONE
    else:
        units = {q: _unit(eid, snap) for q, eid in sources.items()}
        history, note = await _history(hass, sources)
        if history is not None:
            samples, basis = history, BASIS_HISTORY
        else:
            notes.append(note or NOTE_NO_STATISTICS)
            samples, basis = _current_sample(sources, units, snap), BASIS_CURRENT

    return AssignmentResult(
        proposal=proposal, telemetry_map=tmap, grid_negate=grid_negate, battery_negate=battery_negate,
        sign_check=check_signs(samples), basis=basis,
        samples=len(samples), sources=sources, source_origin=origin, excluded=excluded,
        notes=tuple(notes))
