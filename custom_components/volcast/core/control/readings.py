"""Stany encji HA → odczyty w postaci `Params.flatten()` (jednostki kanoniczne).

Wejście to surowy stan i `unit_of_measurement` — przeliczenie jednostek (°F, kW, Wh)
robimy PRZED guardami i uzgadnianiem. Klucz nieczytelny znika (brak odczytu ≠ zero);
zły stan jednej encji nie kasuje pozostałych — `normalize_readings_report` mówi, które zniknęły.

Źródło odczytu (`driver.meta.readings_source`, kontrakt sygnałów §6.1.1): jedno na klucz, w kolejności
encja HA (profil albo mapa ręczna) → rejestr (tryb bezpośredni) → wartość wpisana ręcznie → `none`.
Klucze i wartości to zamknięte listy chmury — nigdy nazwa encji.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Mapping

from ..entity_map import canonical_value, entity_value

# Odczyty, które HA zna od zawsze — w mapie źródeł zawsze, także jako `none`.
MEASURED_KEYS = (
    "soc", "battery_temp_c", "battery_voltage_v", "battery_current_a", "battery_power_w", "pv_power_w",
    "active_power_w", "load_power_w", "grid_power_w", "pv_energy_total_kwh", "grid_export_total_kwh",
    "grid_import_total_kwh",
)
# Liczniki i limity — w mapie tylko z wartością albo gdy profil je opisuje (inaczej pomijamy).
EXTRA_KEYS = (
    "load_energy_total_kwh", "load_energy_day_kwh", "battery_charge_total_kwh", "battery_discharge_total_kwh",
    "bms_charge_current_max_a", "bms_discharge_current_max_a", "battery_capacity_kwh",
)
# Zamknięta lista kluczy chmury (`READINGS_SOURCE_KEYS` w `device-telemetry/driver-block.ts`).
SOURCE_KEYS = MEASURED_KEYS + EXTRA_KEYS
SOURCES = ("entity", "register", "manual", "none")
_SOURCE_KEY_SET = frozenset(SOURCE_KEYS)


@dataclass(frozen=True)
class RawState:
    state: str | None
    unit: str | None = None


def normalize_readings_report(raw: Mapping[str, RawState], profile, integration_domain: str
                              ) -> tuple[dict[str, float | str], tuple[str, ...]]:
    """(odczyty, posortowane klucze bez odczytu) — encja jest, ale nic z niej nie wyszło."""
    out: dict[str, float | str] = {}
    dropped: list[str] = []
    for key, rs in raw.items():
        try:
            value = entity_value(key, rs.state, profile, integration_domain, unit=rs.unit)
        except (KeyError, ValueError, TypeError):
            value = None      # klucz albo integracja spoza profilu, nieoczekiwany kształt stanu
        if value is None:
            dropped.append(key)
        else:
            out[key] = value
    return out, tuple(sorted(dropped))


def normalize_readings(raw: Mapping[str, RawState], profile, integration_domain: str
                       ) -> dict[str, float | str]:
    return normalize_readings_report(raw, profile, integration_domain)[0]


def _present(value) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return math.isfinite(value)
    return isinstance(value, str) and value != ""


def pick_source(key: str, entity_val, register_val, manual_val) -> str | None:
    """Źródło jednego klucza: pierwsza czytelna wartość w kolejności encja → rejestr → ręczna, inaczej
    `none`; None dla klucza spoza zamkniętej listy (takiego nie wysyłamy)."""
    if key not in _SOURCE_KEY_SET:
        return None
    for source, value in (("entity", entity_val), ("register", register_val), ("manual", manual_val)):
        if _present(value):
            return source
    return "none"


def readings_sources(*, entity: Mapping[str, object], register: Mapping[str, object],
                     manual: Mapping[str, object], declared: Iterable[str]) -> dict[str, str]:
    """Pełna mapa źródeł: każdy odczyt z `MEASURED_KEYS`, licznik/limit tylko z wartością albo opisany
    w profilu (`declared`). Wołający maskuje wartości tak, jak telemetria (encja ręczna wygrywa
    także nieczytelna)."""
    known = set(declared)
    out: dict[str, str] = {}
    for key in SOURCE_KEYS:
        source = pick_source(key, entity.get(key), register.get(key), manual.get(key))
        if source != "none" or key in MEASURED_KEYS or key in known:
            out[key] = source
    return out


def manual_reading(key: str, raw: RawState, *, negate: bool = False) -> float | None:
    """Odczyt encji wskazanej ręcznie (bez profilu) — tylko jednostki i znak."""
    value = canonical_value(key, raw.state, raw.unit)
    if value is None:
        return None
    if negate:
        value = -value
    return value + 0.0        # bez „-0.0" w telemetrii
