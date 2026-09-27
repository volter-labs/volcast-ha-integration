"""Nastawy w postaci, którą encja NAPRAWDĘ przyjmie — przed throttlingiem.

Encja `number` ma min/max/krok (DoD GoodWe: 0..99). Gdy dopasowanie robi dopiero
warstwa zapisu, pamięć throttlingu trzyma wartość, której falownik nigdy nie pokaże,
i każdy cykl widzi „rozjazd" → zapis do NVM co minutę. Dlatego dopasowujemy
w przestrzeni encji i wracamy do jednostek kanonicznych, zanim cokolwiek trafi do
throttlingu. Encja bez poprawnego zakresu = niedopasowalna (nie zgadujemy).

Dopasowanie działa PO strażnikach, więc krok zaokrąglamy zawsze w stronę bezpieczną
dla klucza (moc i limit eksportu w dół, próg dolny SoC w górę, górny w dół) —
najbliższy krok potrafiłby oddać pół kroku ponad limit ustalony przez strażnika.
Gdy bezpieczny krok wypada poza zakres encji albo zakres wymusza obcięcie w stronę
niebezpieczną (np. DoD min 10 → próg 95 % spadłby do 90 %), klucz jest niedopasowalny
i sterowanie go wstrzymuje. Obcięcie w stronę bezpieczną to zwykłe dopasowanie.
"""
from __future__ import annotations

import math
from dataclasses import replace
from typing import Any, Iterable, Mapping

from ..entity_map import EntityWrite, entity_value, entity_writes
from ..params import Params

_NUMERIC_KEYS = ("power_w", "soc_min", "soc_max", "export_limit_w")
# kierunek bezpieczny w jednostkach kanonicznych: +1 = w górę, -1 = w dół
SAFE_DIRECTION = {"power_w": -1, "export_limit_w": -1, "soc_min": 1, "soc_max": -1}
_SAFE_DIRECTION = SAFE_DIRECTION             # dawna nazwa
_ROUNDING = ("nearest", "down", "up")
# transformacje profilu odwracające kierunek (kanoniczny ↔ encja)
_FLIPPING_TRANSFORMS = ("invert_percent", "negate")


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _close(a: float, b: float) -> bool:
    """Równość z tolerancją na szum binarny przeliczeń (W ↔ kW, krok 0.1)."""
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)


def _steps(x: float, rounding: str) -> int:
    """Liczba kroków od `min`; wartość leżąca na kroku (z szumem binarnym) zostaje."""
    nearest = round(x)
    if math.isclose(x, nearest, rel_tol=1e-9, abs_tol=1e-9):
        return int(nearest)
    if rounding == "down":
        return math.floor(x)
    if rounding == "up":
        return math.ceil(x)
    return math.floor(x + 0.5)


def fit_number(value: float, attrs: Mapping[str, Any], rounding: str = "nearest") -> float | None:
    """Wartość przycięta do [min, max] i dopasowana do kroku encji; brak zakresu → None.

    `rounding`: "nearest" (domyślnie), "down" albo "up". Kierunek jest bezpieczny, więc
    ruch w przeciwną stronę daje None: obcięcie do zakresu pod prąd kierunku („down" przy
    wartości pod `min`, „up" ponad `max`) i krok ponad `max` (krok nie dzieli zakresu).
    """
    if rounding not in _ROUNDING:
        raise ValueError(f"fit_number: nieznane zaokrąglenie {rounding!r}")
    lo, hi = _num(attrs.get("min")), _num(attrs.get("max"))
    if lo is None or hi is None or lo > hi or not math.isfinite(value):
        return None
    if rounding == "down" and value < lo and not _close(value, lo):
        return None           # obcięcie w górę = niebezpieczne
    if rounding == "up" and value > hi and not _close(value, hi):
        return None           # obcięcie w dół = niebezpieczne
    v = min(max(value, lo), hi)
    step = _num(attrs.get("step"))
    if step is not None and step > 0:
        v = lo + _steps((v - lo) / step, rounding) * step
        if v > hi + 1e-9:
            if rounding == "up":
                return None
            v -= step         # krok nigdy nie wychodzi poza max
        v = round(v, 6)
    return v


def _entity_rounding(key: str, profile, integration_domain: str) -> str:
    """Bezpieczny kierunek klucza przełożony na przestrzeń encji (transformacja profilu)."""
    direction = _SAFE_DIRECTION[key]
    for integ in profile.raw["ha"]["integrations"]:
        if integ["domain"] == integration_domain:
            if integ["entities"][key].get("transform") in _FLIPPING_TRANSFORMS:
                direction = -direction
            break
    return "up" if direction > 0 else "down"


def control_writes(params: Params, profile, integration_domain: str, mapped: Mapping[str, str], *,
                   keys: Iterable[str] | None, units: Mapping[str, str | None]
                   ) -> tuple[list[EntityWrite], list[str]]:
    """`entity_writes` z OBOWIĄZKOWYMI jednostkami — W do encji w kW to 1000× za dużo."""
    if units is None:
        raise TypeError("control_writes: units jest obowiązkowe")
    return entity_writes(params, profile, integration_domain, mapped, keys=keys, units=units)


def fit_params(params: Params, profile, integration_domain: str, mapped: Mapping[str, str],
               units: Mapping[str, str | None], attrs_by_entity: Mapping[str, Mapping[str, Any]]
               ) -> tuple[Params, tuple[str, ...], tuple[str, ...]]:
    """(dopasowane nastawy, klucze zmienione, klucze niedopasowalne) — w kolejności profilu."""
    writes, _ = control_writes(params, profile, integration_domain, mapped, keys=None, units=units)
    changes: dict[str, float] = {}
    adjusted: list[str] = []
    unfit: list[str] = []
    for w in writes:
        if w.domain != "number" or w.key not in _NUMERIC_KEYS:
            continue
        raw = float(w.data["value"])
        fitted = fit_number(raw, attrs_by_entity.get(w.entity_id) or {},
                            _entity_rounding(w.key, profile, integration_domain))
        if fitted is None:
            unfit.append(w.key)
            continue
        if _close(fitted, raw):
            continue
        # powrót do jednostki kanonicznej tą samą drogą co odczyt encji (jednostka, transformacja)
        back = entity_value(w.key, repr(fitted), profile, integration_domain, unit=units.get(w.key))
        if not isinstance(back, float):
            unfit.append(w.key)
            continue
        changes[w.key] = back
        adjusted.append(w.key)
    return replace(params, **changes), tuple(adjusted), tuple(unfit)
