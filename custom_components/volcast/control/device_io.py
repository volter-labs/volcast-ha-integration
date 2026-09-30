"""Wejście/wyjście urządzenia dla wykonawcy: skąd odczyt, dokąd zapis, czym jest własność.

Wykonawca (`executor.py`) podejmuje decyzje rdzeniem i prowadzi stan (własność, migawka, pauza);
to, jak falownik jest czytany i pisany, dostarcza obiekt `DeviceIO`:

* `EntityIO` — tryb encji: stany encji integracji producenta, zapis usługami HA, zdarzenia
  zmiany stanu jako sygnał obcej zmiany (kod przeniesiony z wykonawcy bez zmian logiki);
* `DirectIO` — tryb bezpośredni: odczyt z połączenia z falownikiem (`DirectConnection`), zapis
  pisarzem rejestrów z odczytem zwrotnym (w próbie — `NoWriteWriter`), własność związana z odciskiem
  celu połączenia i urządzenia. Obca zmiana to rozjazd odczytu względem naszego ostatniego zapisu
  (`DriftTracker`, liczony przez wykonawcę raz na cykl sterowania), nie zdarzenie.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Protocol

from homeassistant.helpers.event import async_track_state_change_event

from ..core.control.baseline import SNAPSHOT_KEYS
from ..core.control.conflict import DriftTracker
from ..core.control.cycle import EntityContext
from ..core.control.entity_fit import control_writes, fit_params
from ..core.control.readings import RawState, normalize_readings
from ..core.control.target import EntityTarget, RegisterTarget, WriteTarget
from ..core.modbus.reading import DirectReading
from ..core.modbus.writer import NoWriteWriter, RegisterWriter
from ..core.params import Params
from ..core.registers import RegisterImage
from ..core.write_sequence import ERROR
from .direct import target_fingerprint

NO_READING = ("unavailable", "unknown", "")
# Odczyty mocy na żywo z własnym wiekiem (osobno od wieku SoC i guardu I-9).
LIVE_KEYS = ("pv_power_w", "load_power_w")


@dataclass(frozen=True)
class Reading:
    """Odczyt urządzenia jednego cyklu (w trybie encji: stany encji)."""
    readings: dict[str, float | str]      # znormalizowane (migawka, powrót do bazowego)
    raw_mode: str | None                  # surowa opcja trybu (obca opcja ≠ brak odczytu)
    units: dict[str, str | None]
    attrs: dict[str, dict]                # atrybuty encji, także `options` wyboru trybu
    soc_age_s: float
    source: Any = None                    # tryb bezpośredni: `DirectReading` cyklu (None = brak odczytu)
    # wiek odczytu kluczy mocy na żywo (PV, pobór) — tylko gdy odczyt jest w `readings`
    live_ages: dict[str, float] = field(default_factory=dict)

    def live(self, key: str) -> tuple[float | None, float | None]:
        """Wartość i wiek odczytu mocy na żywo; (None, None) bez odczytu."""
        value = self.readings.get(key)
        if not isinstance(value, float) or key not in self.live_ages:
            return None, None
        return value, self.live_ages[key]

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
        states = {}
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
            states[key] = st
        readings = normalize_readings(raw, self._profile, self._domain) if self._domain else {}
        # Wiek tylko dla czytelnego odczytu: `unavailable`/`unknown` znika z `readings` → brak wieku.
        live_ages = {k: self.age(states[k], now_utc) for k in LIVE_KEYS if k in readings}
        return Reading(readings, raw_mode, units, attrs, self.age(soc_state, now_utc),
                       live_ages=live_ages)

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


# ── tryb bezpośredni ──────────────────────────────────────────────────────


_EMPTY = DirectReading(values={}, device={}, programs=None, tou_enabled=None, image=RegisterImage({}),
                       at_mono=-math.inf, at_utc=datetime(1970, 1, 1, tzinfo=timezone.utc))
_RATED_RANGE_W = (1000.0, 30000.0)


class _NoLinkWriter:
    """Pisarz bez połączenia: nic nie wysyła (wykonawca i tak nie pisze bez potwierdzonej tożsamości)."""

    async def async_write(self, w) -> str:
        return ERROR


class DirectIO:
    """Tryb bezpośredni: rejestry falownika przez `DirectConnection`."""

    kind = "direct"

    def __init__(self, conn, profile, *, trial: bool, unreadable: Iterable[str] = (),
                 capabilities: Mapping[str, bool] | None = None, salt: bytes,
                 now_wall: Callable[[], float] = time.time, clock: Callable[[], float] = time.monotonic) -> None:
        self.conn = conn
        self._profile = profile
        self.trial = bool(trial)
        # klucze bez odczytu zwrotnego z sondy — jedyne źródło dla klienta, pisarza i celu (profil osobno)
        self.unreadable = frozenset(unreadable)
        self.capabilities = {k: v for k, v in (capabilities or {}).items() if isinstance(v, bool)}
        self._salt = bytes(salt)
        self._now_wall = now_wall
        self._clock = clock
        self.budget = None                    # budżet NVM wykonawcy (`bind_budget`) — liczy `on_send`
        self.drift = DriftTracker()
        self._sent: list[str] = []
        self._nowrite = NoWriteWriter() if self.trial else None
        self._writer: RegisterWriter | None = None
        self._writer_client = None

    # ── pisarz, budżet, bariera rozjazdu ──

    @property
    def writer(self):
        if self._nowrite is not None:
            return self._nowrite              # próba: druga, niezależna blokada obok bramek
        client = self.conn.client
        if client is None:
            return _NoLinkWriter()
        if self._writer is None or self._writer_client is not client:
            self._writer = RegisterWriter(client, self._profile, on_send=self._on_send, unreadable=self.unreadable)
            self._writer_client = client
        return self._writer

    def bind_budget(self, budget, *, now_wall: Callable[[], float] | None = None) -> None:
        """Budżet wykonawcy i jego zegar UTC (ten sam, którym cykl sprawdza wyczerpanie)."""
        self.budget = budget
        if now_wall is not None:
            self._now_wall = now_wall

    def _on_send(self, key: str) -> None:
        self._sent.append(key)
        if self.budget is not None:
            self.budget.note(key, self._now_wall())

    def end_writes(self, end_mono: float) -> tuple[str, ...]:
        """Koniec naszej wymiany zapisów: odczyt rozpoczęty wcześniej nie świadczy o rozjeździe."""
        sent, self._sent = tuple(self._sent), []
        for key in sent:
            self.drift.note_own_write(key, end_mono)
        return sent

    def unsupported_seed(self) -> set[str]:
        """Klucze nieobsługiwane na całą sesję: bez odczytu zwrotnego (sonda) i bez rejestru (wyjątek 2)."""
        return set(self.unreadable) | {k for k, v in self.capabilities.items() if v is False}

    # ── tożsamość ──

    async def async_identity_ok(self) -> bool:
        """Zapis, powrót i decyzja próbna wymagają potwierdzonej tożsamości w bieżącej sesji połączenia."""
        conn = self.conn
        if conn.client is None or conn.refused() is not None:
            return False
        if conn.identity != "confirmed" or conn.identity_check_due():
            await conn.async_confirm_identity()
        return conn.identity == "confirmed" and not conn.identity_check_due()

    def rated_power_w(self) -> float | None:
        r = self.conn.reading
        v = r.values.get("rated_power_w") if r is not None else None
        if isinstance(v, (int, float)) and not isinstance(v, bool) and _RATED_RANGE_W[0] <= v <= _RATED_RANGE_W[1]:
            return float(v)
        return None

    # ── odczyt ──

    def read(self, now_utc) -> Reading:
        r = self.conn.reading
        if r is None:
            return Reading({}, None, {}, {}, math.inf)
        readings: dict[str, float | str] = {}
        raw_mode = None
        for key, v in r.device.items():
            if key == "mode" and isinstance(v, str):
                raw_mode = v
                if v.startswith("?"):
                    continue                  # czytelna wartość spoza profilu — obcy tryb
            readings[key] = v
        for key in ("soc", "battery_temp_c"):
            v = r.values.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                readings[key] = float(v)
        return Reading(readings, raw_mode, {}, {}, self.conn.age_s(), r)

    def target(self, rd: Reading) -> RegisterTarget:
        return RegisterTarget(rd.source if rd.source is not None else _EMPTY, unreadable=self.unreadable)

    def cycle_input(self, rd: Reading) -> dict:
        return {"target": self.target(rd)}

    # ── własność: profil + odcisk celu połączenia (z solą instalacji) + odcisk urządzenia ──

    def owner(self) -> dict:
        fp = self.conn.target.get("device_fp")
        return {"profile": self._profile.id if self._profile else "", "mode": "direct",
                "target": target_fingerprint(self.conn.target, self._salt),
                "device": fp if isinstance(fp, str) else ""}

    def owner_matches(self, record: Mapping[str, str]) -> bool:
        return dict(record) == self.owner()

    def snapshot_keys(self) -> tuple[str, ...]:
        write = self._profile.raw.get("write") or {} if self._profile else {}
        skip = self.unsupported_seed() | set(self._profile.modbus.echo_only if self._profile else ())
        return tuple(k for k in SNAPSHOT_KEYS if k in write and k not in skip)

    # ── powrót do trybu bazowego ──

    def restore_fit(self, rd: Reading, params: Params) -> tuple[Params, tuple[str, ...]]:
        fitted, _, unfit = self.target(rd).fit(params, self._profile)
        return fitted, unfit

    def restore_writes(self, fitted: Params, keys: Iterable[str], rd: Reading) -> list:
        keys = list(keys)
        if not keys:
            return []
        writes, _ = self.target(rd).writes(fitted, self._profile, keys)
        return writes

    # ── obca zmiana: rozjazd liczy wykonawca ──

    def subscribe_foreign(self, on_change: Callable) -> Callable[[], None]:
        return lambda: None

    def entity_of(self, key: str) -> str | None:
        return None
