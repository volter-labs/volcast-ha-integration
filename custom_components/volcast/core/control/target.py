"""Cel zapisu cyklu sterowania: encje integracji producenta albo rejestry falownika.

Cykl (`cycle.py`) jest wspólny — grupa tryb+moc, cofnięcie, klucze niepewne, odwrót grupy,
I-6/I-8, zatrzask i przejęcie działają identycznie dla obu celów. Cel dostarcza tylko to,
co zależy od drogi do urządzenia: brakujące klucze, dopasowanie nastaw do tego, co cel
przyjmie, zapisy, widok stanu urządzenia i wiedzę o opcjach trybu.

`EntityTarget` to dotychczasowy kod cyklu przeniesiony 1:1. `RegisterTarget` pracuje na
odczycie z rejestrów (`DirectReading`) i koduje zapisy według map profilu.
"""
from __future__ import annotations

import math
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Protocol

from ..entity_map import entity_value
from ..params import Params
from ..registers import RegisterWrite, encode_writes
from .caps import missing_write_keys
from .entity_fit import SAFE_DIRECTION, control_writes, fit_params

if TYPE_CHECKING:
    from ..modbus.reading import DirectReading
    from .cycle import EntityContext

_NO_READING = ("unavailable", "unknown", "")
# Zakres słowa rejestru według kodowania z profilu.
_REGISTER_RANGE = {"percent": (0.0, 100.0), "watts": (0.0, 65535.0)}


class WriteTarget(Protocol):
    kind: str

    def missing_keys(self, profile) -> tuple[str, ...]: ...

    def fit(self, params: Params, profile) -> tuple[Params, tuple[str, ...], tuple[str, ...]]: ...

    def writes(self, params: Params, profile, keys: Iterable[str] | None) -> tuple[list, tuple[str, ...]]: ...

    def device_view(self, flat: Mapping[str, Any], profile) -> dict[str, float | str]: ...

    def mode_option_unknown(self, mode: str, profile) -> bool: ...

    def has_temperature(self) -> bool: ...


# ── encje ─────────────────────────────────────────────────────────────────


def _device_view(readings: Mapping[str, Any], flat: Mapping[str, Any], profile,
                 ents: "EntityContext") -> dict[str, float | str]:
    """Odczyty kluczy planu w postaci `Params.flatten()`; nieczytelne znikają (brak ≠ rozjazd).

    Wykonawca podaje odczyty już znormalizowane; surowy stan encji (`unavailable`,
    `on`/`off`, opcja wyboru, liczba jako tekst) przechodzi przez ten sam przekład co
    odczyt encji. Liczba tam, gdzie tryb jest nazwą, zostaje — uzgadnianie odrzuci ją
    jako błąd wołającego (cykl bez zapisów). Czytelna opcja spoza profilu to odczyt
    RÓŻNY od każdego trybu (znacznik `?opcja`), nie brak odczytu — nasz tryb wraca.
    """
    out: dict[str, float | str] = {}
    for key in flat:
        if key not in readings:
            continue
        value = readings[key]
        if value is None:
            continue
        if isinstance(value, bool):
            value = 1.0 if value else 0.0
        elif isinstance(value, (int, float)):
            if not math.isfinite(value):
                continue
            value = float(value)
        elif isinstance(value, str) and not (key == "mode" and value in profile.modes):
            try:
                value = entity_value(key, value, profile, ents.domain, unit=ents.units.get(key))
            except (KeyError, ValueError, TypeError):
                value = None
            if value is None:
                if key != "mode" or readings[key] in _NO_READING:
                    continue
                value = "?" + readings[key]
        out[key] = value
    return out


class EntityTarget:
    kind = "entities"

    def __init__(self, ents: "EntityContext") -> None:
        self.ents = ents

    def missing_keys(self, profile) -> tuple[str, ...]:
        return missing_write_keys(profile, self.ents.mapped)

    def fit(self, params: Params, profile) -> tuple[Params, tuple[str, ...], tuple[str, ...]]:
        e = self.ents
        return fit_params(params, profile, e.domain, e.mapped, e.units, e.attrs)

    def writes(self, params: Params, profile, keys: Iterable[str] | None) -> tuple[list, tuple[str, ...]]:
        e = self.ents
        writes, unmapped = control_writes(params, profile, e.domain, e.mapped, keys=keys, units=e.units)
        return writes, tuple(unmapped)

    def device_view(self, flat: Mapping[str, Any], profile) -> dict[str, float | str]:
        return _device_view(self.ents.readings, flat, profile, self.ents)

    def mode_option_unknown(self, mode: str, profile) -> bool:
        """Opcja trybu spoza listy `options` encji (sprawdzana z góry, przed zapisem mocy)."""
        e = self.ents
        options = (e.attrs.get(e.mapped.get("mode", "")) or {}).get("options")
        return isinstance(options, (list, tuple)) and profile.modes[mode].ha_option not in options

    def has_temperature(self) -> bool:
        return "battery_temp_c" in self.ents.mapped


# ── rejestry ──────────────────────────────────────────────────────────────


def _fit_register(value: float, lo: float, hi: float, direction: int) -> float:
    """Obcięcie do zakresu słowa i zaokrąglenie do całości w stronę bezpieczną dla klucza."""
    v = min(max(value, lo), hi)
    if math.isclose(v, round(v), abs_tol=1e-9):
        return float(round(v))
    return float(math.ceil(v) if direction > 0 else math.floor(v))


class RegisterTarget:
    """Cel rejestrowy: odczyt z rejestrów jest widokiem urządzenia, zapisy to `RegisterWrite`.

    Klucze bez odczytu zwrotnego (`unreadable`, wynik sondy) nigdy nie dostają zapisu — wykonawca
    trzyma je też w `memory.unsupported`, więc cykl traktuje je jak nieobsługiwane (bez
    wstrzymania trybu). `tou` obejmuje wszystkie pola programów i włącznik.
    """

    kind = "direct"

    def __init__(self, reading: "DirectReading", *, unreadable: frozenset[str] = frozenset()) -> None:
        self.reading = reading
        self.unreadable = frozenset(unreadable)

    def _skipped(self, keys: Iterable[str]) -> set[str]:
        return {k for k in keys if k in self.unreadable
                or ("tou" in self.unreadable and (k.startswith("tou.") or k == "tou_enable"))}

    def missing_keys(self, profile) -> tuple[str, ...]:
        return ()

    def fit(self, params: Params, profile) -> tuple[Params, tuple[str, ...], tuple[str, ...]]:
        spec = profile.raw["write"]
        changes: dict[str, float] = {}
        adjusted: list[str] = []
        for key, direction in SAFE_DIRECTION.items():
            value = getattr(params, key)
            enc = (spec.get(key) or {}).get("encode")
            if value is None or enc not in _REGISTER_RANGE:
                continue
            fitted = _fit_register(float(value), *_REGISTER_RANGE[enc], direction)
            if not math.isclose(fitted, float(value), abs_tol=1e-9):
                changes[key] = fitted
                adjusted.append(key)
        order = list(profile.write_order)
        adjusted.sort(key=lambda k: order.index(k) if k in order else len(order))
        return replace(params, **changes), tuple(adjusted), ()

    def writes(self, params: Params, profile, keys: Iterable[str] | None) -> tuple[list, tuple[str, ...]]:
        wanted = None if keys is None else set(keys)
        if self.unreadable:
            wanted = set(params.flatten()) if wanted is None else wanted
            wanted -= self._skipped(wanted)
        out: list[RegisterWrite] = encode_writes(params, profile, wanted, current=self.reading.image)
        return out, ()

    def device_view(self, flat: Mapping[str, Any], profile) -> dict[str, float | str]:
        dev = self.reading.device
        return {k: dev[k] for k in flat if k in dev}

    def mode_option_unknown(self, mode: str, profile) -> bool:
        return False

    def has_temperature(self) -> bool:
        # `values` niesie każdy klucz mapy `read` profilu (None = brak odczytu).
        return "battery_temp_c" in self.reading.values
