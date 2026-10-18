"""PV przed siecią w slocie `charge_grid`: faza `grid` (plan) albo `pv` (tryb ładowania z PV).

Slot `charge_grid` idzie na falownik jako ładowanie stałą mocą slotu; gdy PV na żywo daje
więcej, nadwyżka płynie do sieci, a bateria i tak kupuje prąd. Ten moduł rozstrzyga co cykl,
czy w bieżącym slocie podmienić ładowanie z sieci na tryb ładowania z PV. Czysta logika:
bez HA i bez I/O; czas monotoniczny podaje wołający.

* Wejście do `pv`: nadwyżka `pv − load` ≥ moc slotu nieprzerwanie przez ≥ `PV_FIRST_ENTER_S`
  (licząc od PIERWSZEJ obserwacji; tick co 60 s = 4 odczyty: 0, 60, 120, 180 s). Nieważny
  odczyt albo przerwa między tickami > `PV_FIRST_GAP_S` liczą zegar wejścia od nowa.
* Powrót do `grid`: nadwyżka < moc slotu − `PV_FIRST_MARGIN_W` nieprzerwanie przez
  ≥ `PV_FIRST_EXIT_S`. Strefa między tymi progami trzyma fazę i kasuje zegar wyjścia.
* Cel energii: `charged_wh` = całka `max(0, −battery_w)·dt` od początku slotu (moc baterii
  dodatnia = rozładowanie), wartość z bieżącego ticku × czas od poprzedniego. Przedział
  > `PV_FIRST_GAP_S` nie jest liczony (luka). Cel = moc slotu × długość slotu. Osiągnięty
  w fazie `pv` trzyma ją do końca slotu (zakup nigdy ponad plan). W fazie `grid` NIE jest
  wejściem do `pv` — wejście zawsze wymaga nadwyżki (wątpliwość = intencja planu).
  Brak odczytu baterii = cel nieaktywny w tym ticku (nota); logika fazy działa dalej.
  Bezpieczne: w fazie `grid` falownik ładuje co najwyżej mocą slotu.
* Nieważny lub przestarzały odczyt PV albo poboru (wołający podaje już zwalidowane wartości,
  None = nieważny): pierwszy z rzędu, nie dalej niż `PV_FIRST_HELD_S` od poprzedniego ticku,
  trzyma bieżącą fazę (jak przetrzymana para w `live_export`); drugi z rzędu albo po dłuższej
  ciszy = faza `grid`.
* Inny klucz slotu `(start, end, intent)` = pamięć od zera. Niepoprawna moc albo długość
  slotu = faza `grid`.

Pamięć (`PvFirstMemory`) jest niezmienna — `step` zwraca nową. Żyje tylko w RAM: po restarcie
liczniki startują od zera (zachowawczo: od fazy `grid`).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any

from ..profile_schema import SLOT_POWER_KINDS
from .live_export import SlotKey

PV_FIRST_ENTER_S = 180.0
PV_FIRST_EXIT_S = 300.0
PV_FIRST_MARGIN_W = 200.0
# przedział między tickami dłuższy niż tyle = luka (bez całkowania, zegar wejścia od nowa)
PV_FIRST_GAP_S = 150.0
# najdłuższa przerwa od ostatniego ticku, przez którą jeden nieważny odczyt trzyma fazę
PV_FIRST_HELD_S = 150.0

PHASE_GRID = "grid"
PHASE_PV = "pv"

NOTE_PV_FIRST = "pv_first"
# cel energii osiągnięty — faza `pv` do końca slotu
NOTE_TARGET = "pv_first_target"
# brak odczytu mocy baterii — cel energii nieaktywny w tym ticku
NOTE_NO_BATTERY = "pv_first_no_battery"
# jeden nieważny odczyt PV/poboru — faza przetrzymana
NOTE_HELD = "pv_first_reading_held"
# brak ważnego odczytu PV/poboru — faza `grid`
NOTE_NO_READING = "pv_first_no_reading"
# moc albo długość slotu niepoprawna — faza `grid`
NOTE_NO_POWER = "pv_first_no_power"


@dataclass(frozen=True)
class PvFirstMemory:
    # slot (początek, koniec, intencja), którego dotyczą liczniki; None = jeszcze żaden
    key: SlotKey | None = None
    phase: str = PHASE_GRID
    # czas monotoniczny początku nieprzerwanej nadwyżki ≥ moc slotu (zegar wejścia)
    above_since: float | None = None
    # czas monotoniczny początku nieprzerwanej nadwyżki < moc slotu − margines (zegar wyjścia)
    below_since: float | None = None
    # energia, którą bateria przyjęła od początku slotu [Wh]
    charged_wh: float = 0.0
    # czas monotoniczny poprzedniego ticku (każdego, także z nieważnym odczytem)
    last_mono: float | None = None
    # kolejne ticki bez ważnej pary (PV, pobór)
    invalid_streak: int = 0


@dataclass(frozen=True)
class PvFirstStep:
    """Wynik jednego ticku: faza do wykonania, nowa pamięć i noty (pierwsza zawsze główna)."""
    phase: str
    memory: PvFirstMemory
    notes: tuple[str, ...]


def applies(profile: Any, intent: str) -> bool:
    """Czy korekta PV przed siecią ma sens dla tej intencji i profilu.

    Tylko `charge_grid` z mocą ze slotu (inaczej falownik i tak nie ładuje stałą mocą)
    w modelu `mode_setpoint` (podmiana trybu) i tylko, gdy profil ma intencję `charge_pv`
    (jest na co podmienić).
    """
    if intent != "charge_grid" or profile.control_model != "mode_setpoint":
        return False
    return (profile.power_kind("charge_grid") in SLOT_POWER_KINDS
            and profile.power_kind("charge_pv") is not None)


def _finite(value: float | None) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return None
    return float(value)


def _note(surplus: float | None, power_w: float | None, charged_wh: float, phase: str) -> str:
    def w(v: float | None) -> str:
        return "-" if v is None else f"{v:.0f}"
    return f"{NOTE_PV_FIRST}:surplus={w(surplus)},slot={w(power_w)},charged_wh={charged_wh:.0f},phase={phase}"


def step(*, memory: PvFirstMemory, key: SlotKey, power_w: float, duration_s: float,
         pv_w: float | None, load_w: float | None, battery_w: float | None,
         now_mono: float) -> PvFirstStep:
    """Jeden tick slotu `charge_grid` (`pv_w`/`load_w` już zwalidowane albo None)."""
    if memory.key != key:
        memory = PvFirstMemory(key=key)          # nowy slot albo intencja — liczniki od zera
    power = _finite(power_w)
    duration = _finite(duration_s)
    if power is None or power <= 0.0 or duration is None or duration <= 0.0:
        mem = PvFirstMemory(key=key, last_mono=now_mono)
        return PvFirstStep(PHASE_GRID, mem, (_note(None, power, 0.0, PHASE_GRID), NOTE_NO_POWER))

    dt = None if memory.last_mono is None else now_mono - memory.last_mono
    gap = dt is None or not 0.0 <= dt <= PV_FIRST_GAP_S
    battery = _finite(battery_w)
    charged = memory.charged_wh
    if battery is not None and not gap:
        charged += max(0.0, -battery) * dt / 3600.0
    reached = charged >= power * duration / 3600.0
    extra: list[str] = [] if battery is not None else [NOTE_NO_BATTERY]

    phase, above, below = memory.phase, memory.above_since, memory.below_since
    surplus = None if pv_w is None or load_w is None else pv_w - load_w
    if surplus is None:
        streak = memory.invalid_streak + 1
        held = streak == 1 and dt is not None and 0.0 <= dt < PV_FIRST_HELD_S
        if phase == PHASE_PV and reached:
            pass                                 # cel osiągnięty: odczyt PV już niepotrzebny
        elif held:
            above = None                         # wejście wymaga nieprzerwanie ważnej nadwyżki
            extra.append(NOTE_HELD)
        else:
            phase, above, below = PHASE_GRID, None, None
            extra.append(NOTE_NO_READING)
    else:
        streak = 0
        if phase == PHASE_GRID:
            below = None
            if surplus >= power:
                above = now_mono if above is None or gap else above
                if now_mono - above >= PV_FIRST_ENTER_S:
                    phase, above = PHASE_PV, None
            else:
                above = None
        else:
            above = None
            if surplus < power - PV_FIRST_MARGIN_W and not reached:
                below = now_mono if below is None else below
                if now_mono - below >= PV_FIRST_EXIT_S:
                    phase, below = PHASE_GRID, None
            else:
                below = None
    if phase == PHASE_PV and reached:
        extra.append(NOTE_TARGET)

    mem = replace(memory, phase=phase, above_since=above, below_since=below, charged_wh=charged,
                  last_mono=now_mono, invalid_streak=streak)
    return PvFirstStep(phase, mem, (_note(surplus, power, charged, phase), *extra))
