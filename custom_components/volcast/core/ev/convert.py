"""Przeliczenia moc (W) ↔ nastawa (A) ładowarki i wybór slotu — czyste funkcje."""
from __future__ import annotations

import math
from datetime import datetime
from typing import Iterable

from .model import LoadSlot

# Napięcie fazowe zgodne z planerem po stronie chmury.
PHASE_VOLTAGE_V = 230.0
_EPS = 1e-9


def amps_to_watts(amps: float, phases: int) -> float:
    return amps * PHASE_VOLTAGE_V * phases


def watts_to_amps(power_w: float, phases: int, step: float, lo: float, hi: float) -> float | None:
    """Największa wykonalna nastawa (A) ≤ mocy: podłoga do kroku, przycięta do `hi`.

    `None`, gdy po podłodze wypada poniżej `lo` (ładowarki nie da się włączyć) albo wejście
    jest bezsensowne (fazy/krok ≤ 0, moc ≤ 0).
    """
    if phases < 1 or step <= 0 or not math.isfinite(power_w) or power_w <= 0:
        return None
    amps = power_w / (PHASE_VOLTAGE_V * phases)
    floored = math.floor(amps / step + _EPS) * step
    clamped = min(floored, hi)
    if clamped <= 0 or clamped + _EPS < lo:
        return None
    return round(clamped, 6)


def slot_for(now: datetime, loads: Iterable[LoadSlot]) -> LoadSlot | None:
    """Slot obejmujący `now`, przedział [from, to). `now` musi mieć strefę."""
    for s in loads:
        if s.start <= now < s.end:
            return s
    return None
