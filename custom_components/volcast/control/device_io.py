"""Wejście/wyjście urządzenia dla wykonawcy: skąd odczyt, dokąd zapis, czym jest własność.

Wykonawca (`executor.py`) podejmuje decyzje rdzeniem i prowadzi stan (własność, migawka, pauza);
to, jak falownik jest czytany i pisany, dostarcza obiekt `DeviceIO`:

* `EntityIO` — tryb encji: stany encji integracji producenta, zapis usługami HA, zdarzenia
  zmiany stanu jako sygnał obcej zmiany (kod przeniesiony z wykonawcy bez zmian logiki);
* tryb bezpośredni (rejestry) dostarcza własną implementację tego samego protokołu.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Protocol

from homeassistant.helpers.event import async_track_state_change_event

from ..core.control.cycle import EntityContext
from ..core.control.entity_fit import control_writes, fit_params
from ..core.control.readings import RawState, normalize_readings
from ..core.control.target import EntityTarget, WriteTarget
from ..core.params import Params

NO_READING = ("unavailable", "unknown", "")


@dataclass(frozen=True)
class Reading:
    """Odczyt urządzenia jednego cyklu (w trybie encji: stany encji)."""
    readings: dict[str, float | str]      # znormalizowane (migawka, powrót do bazowego)
    raw_mode: str | None                  # surowa opcja trybu (obca opcja ≠ brak odczytu)
    units: dict[str, str | None]
    attrs: dict[str, dict]                # atrybuty encji, także `options` wyboru trybu
    soc_age_s: float

    @property
    def foreign_mode(self) -> bool:
        """Czytelna opcja trybu, której profil nie zna."""
        return self.raw_mode is not None and self.raw_mode not in NO_READING \
            and "mode" not in self.readings

    def for_cycle(self) -> dict[str, float | str]:
        """Odczyty dla cyklu: tryb jako surowa opcja — cykl sam rozpozna obcą."""
        out = dict(self.readings)
        if self.raw_mode is not None:
            out["mode"] = self.raw_mode
        return out


class DeviceIO(Protocol):
    kind: str                                            # "entities" | "direct"
    writer: Any                                          # async_write(w) -> str

    def read(self, now_utc) -> Reading: ...

    def target(self, rd: Reading) -> WriteTarget: ...

    def cycle_input(self, rd: Reading) -> dict: ...         # argumenty celu dla `decide_cycle`

    def owner(self) -> dict: ...

    def owner_matches(self, record: Mapping[str, str]) -> bool: ...

    def snapshot_keys(self) -> tuple[str, ...]: ...

    def restore_fit(self, rd: Reading, params: Params) -> tuple[Params, tuple[str, ...]]: ...

    def restore_writes(self, fitted: Params, keys: Iterable[str], rd: Reading) -> list: ...

    def subscribe_foreign(self, on_change: Callable) -> Callable[[], None]: ...

    def entity_of(self, key: str) -> str | None: ...


class EntityIO:
    """Tryb encji: dokładnie to, co wykonawca robił dotąd na encjach."""

    kind = "entities"

    def __init__(self, hass, profile, domain: str | None, mapped: Mapping[str, str], writer, *,
                 mode_unique_id: str | None = None) -> None:
        self._hass = hass
        self._profile = profile
        self._domain = domain
        self.mapped = dict(mapped) if domain else {}
        self._mode_uid = mode_unique_id if domain and "mode" in self.mapped else None
        self.writer = writer

    # ── odczyt ──

    def read(self, now_utc) -> Reading:
        raw: dict[str, RawState] = {}
        units: dict[str, str | None] = {}
        attrs: dict[str, dict] = {}
        soc_state = None
        raw_mode = None
        for key, eid in self.mapped.items():
            st = self._hass.states.get(eid)
            if st is None:
                continue
            unit = st.attributes.get("unit_of_measurement")
            raw[key], units[key] = RawState(st.state, unit), unit
            attrs[eid] = dict(st.attributes)
            if key == "mode" and isinstance(st.state, str):
                raw_mode = st.state
            if key == "soc":
                soc_state = st
        readings = normalize_readings(raw, self._profile, self._domain) if self._domain else {}
        return Reading(readings, raw_mode, units, attrs, self.age(soc_state, now_utc))

    @staticmethod
    def age(st, now_utc) -> float:
        if st is None:
            return math.inf
        ts = getattr(st, "last_reported", None) or getattr(st, "last_updated", None)
        if ts is None:
            return math.inf
        age = (now_utc - ts).total_seconds()
        return 0.0 if -5.0 < age < 0.0 else age

    def _context(self, rd: Reading) -> EntityContext:
        return EntityContext(domain=self._domain or "", mapped=self.mapped, units=rd.units,
                             attrs=rd.attrs, readings=rd.for_cycle())

    def target(self, rd: Reading) -> EntityTarget:
        return EntityTarget(self._context(rd))

    def cycle_input(self, rd: Reading) -> dict:
        # Tryb encji woła cykl jak dotąd (`ents=`) — cykl sam owija kontekst w `EntityTarget`.
        return {"ents": self._context(rd)}

    # ── własność (po identyfikatorze rejestru encji trybu, entity_id obok) ──

    def owner(self) -> dict:
        owner = {"profile": self._profile.id if self._profile else "", "domain": self._domain or ""}
        if self._mode_uid:
            owner["mode_uid"] = self._mode_uid
        # entity_id zapisywany dalej obok — poprzednia wersja dopasowuje rekord po nim.
        owner["mode_entity"] = self.mapped.get("mode", "")
        return owner

    def owner_matches(self, record: Mapping[str, str]) -> bool:
        current = self.owner()
        if record.get("profile") != current["profile"] or record.get("domain") != current["domain"]:
            return False
        if record.get("mode_uid") and "mode_uid" in current:
            return record["mode_uid"] == current["mode_uid"]
        return record.get("mode_entity") == current["mode_entity"]

    def snapshot_keys(self) -> tuple[str, ...]:
        return tuple(self.mapped)

    # ── powrót do trybu bazowego ──

    def restore_fit(self, rd: Reading, params: Params) -> tuple[Params, tuple[str, ...]]:
        fitted, _, unfit = fit_params(params, self._profile, self._domain, self.mapped, rd.units, rd.attrs)
        return fitted, unfit

    def restore_writes(self, fitted: Params, keys: Iterable[str], rd: Reading) -> list:
        writes, _ = control_writes(fitted, self._profile, self._domain, self.mapped, keys=list(keys),
                                   units=rd.units)
        return writes

    # ── obca zmiana ──

    def subscribe_foreign(self, on_change: Callable) -> Callable[[], None]:
        order = (self._profile.raw.get("write_policy") or {}).get("order") or () if self._profile else ()
        watched = [self.mapped[k] for k in order if k in self.mapped]
        if not watched:
            return lambda: None
        return async_track_state_change_event(self._hass, watched, on_change)

    def entity_of(self, key: str) -> str | None:
        return self.mapped.get(key)
