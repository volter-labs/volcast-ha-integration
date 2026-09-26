"""Stany encji HA → odczyty w postaci `Params.flatten()` (jednostki kanoniczne).

Wejście to surowy stan i `unit_of_measurement` — przeliczenie jednostek (°F, kW, Wh)
robimy PRZED guardami i uzgadnianiem. Klucz nieczytelny znika (brak odczytu ≠ zero);
zły stan jednej encji nie kasuje pozostałych.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from ..entity_map import canonical_value, entity_value


@dataclass(frozen=True)
class RawState:
    state: str | None
    unit: str | None = None


def normalize_readings(raw: Mapping[str, RawState], profile, integration_domain: str
                       ) -> dict[str, float | str]:
    out: dict[str, float | str] = {}
    for key, rs in raw.items():
        try:
            value = entity_value(key, rs.state, profile, integration_domain, unit=rs.unit)
        except (KeyError, ValueError, TypeError):
            continue          # klucz albo integracja spoza profilu, nieoczekiwany kształt stanu
        if value is not None:
            out[key] = value
    return out


def manual_reading(key: str, raw: RawState, *, negate: bool = False) -> float | None:
    """Odczyt encji wskazanej ręcznie (bez profilu) — tylko jednostki i znak."""
    value = canonical_value(key, raw.state, raw.unit)
    if value is None:
        return None
    if negate:
        value = -value
    return value + 0.0        # bez „-0.0" w telemetrii
