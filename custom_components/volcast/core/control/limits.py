"""Limity wykonawcy w trybie encji: moc znamionowa z nazwy modelu + korekta ręczna.

Górna granica mocy to ta sama stała, którą sprawdzają strażnicy (`MAX_POWER_W`) —
jedno źródło; falownik powyżej niej nie jest obsługiwany.
"""
from __future__ import annotations

import math
import re

from ..guards import MAX_POWER_W

_KW = re.compile(r"(?<![\d.])(\d{1,2}(?:[.,]\d)?)K(?!WH)")
_W_MIN, _W_MAX = 100.0, MAX_POWER_W
_RATED_MIN, _RATED_MAX = 1_000.0, MAX_POWER_W
# Zakres mocy znamionowej (W) — także dla pola w opcjach integracji.
RATED_POWER_RANGE_W = (_RATED_MIN, _RATED_MAX)
_KWH_MIN, _KWH_MAX = 0.5, 200.0
# Zakres pojemności baterii (kWh) — także dla pola w opcjach integracji.
BATTERY_CAPACITY_RANGE_KWH = (_KWH_MIN, _KWH_MAX)
# Dozwolone źródła limitów w bloku `driver.limits` (kontrakt telemetrii).
LIMIT_SOURCES = frozenset({"profile", "registers", "entities", "user"})


def rated_power_from_model(model: str | None) -> float | None:
    if not isinstance(model, str):
        return None
    m = _KW.search(model)
    if m is None:
        return None
    watts = float(round(float(m.group(1).replace(",", ".")) * 1000.0))
    return watts if _RATED_MIN <= watts <= _RATED_MAX else None


def _ok(v: float | None, lo: float, hi: float) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v) and lo <= v <= hi)


def executor_limits(*, rated_power_w: float | None, battery_capacity_kwh: float | None = None,
                    max_charge_w: float | None = None, max_discharge_w: float | None = None,
                    source: str = "entities") -> dict | None:
    if source not in LIMIT_SOURCES:
        raise ValueError(f"unknown limits source: {source!r}")
    out: dict = {}
    if _ok(rated_power_w, _RATED_MIN, _RATED_MAX):
        out["rated_power_w"] = round(rated_power_w)
    for key, v in (("max_charge_w", max_charge_w), ("max_discharge_w", max_discharge_w)):
        if _ok(v, _W_MIN, _W_MAX):
            out[key] = round(v)
    if _ok(battery_capacity_kwh, _KWH_MIN, _KWH_MAX):
        out["battery_capacity_kwh"] = round(battery_capacity_kwh, 2)
    if not out:
        return None
    out["source"] = source
    return out
