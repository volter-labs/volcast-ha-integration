"""Sprzedaż z mocą `slot_live_export`: nastawa eksportu z odczytów bieżącego cyklu.

Moc slotu sprzedaży to moc baterii po stronie AC; tryb sprzedaży falownika czyta nastawę
jako eksport do sieci ponad pokrycie domu. Cykl zamienia więc moc baterii na nastawę
`sell_xset(bateria + PV − dom)` — PO strażnikach (działają na mocy baterii) i PRZED
dopasowaniem do encji i throttlingiem (te działają już na nastawie eksportu).

Odczyt PV albo poboru jest ważny tylko, gdy jest liczbą skończoną, NIEUJEMNĄ, nie większą
niż 2 × moc znamionowa (gdy ją znamy) i świeżą według reguły strażnika I-9 (ten sam limit
wieku profilu; wiek `inf` = brak znacznika czasu, wiek ujemny = przesunięcie zegara —
oba nieświeże). Odczyt ujemny albo absurdalny NIE jest przycinany do 0: pobór liczony
(PV + bateria − sieć) bywa chwilowo ujemny, a przycięty do 0 podniósłby nastawę eksportu.
Nieważny odczyt = brak odczytu (ścieżka zapasowa `bateria − ostatni ważny pobór`).

Pamięć (`LiveExportMemory`) jest niezmienna — cykl i `commit` podmieniają ją w całości.
Ostatni ważny pobór odświeża każdy ważny odczyt; nastawę pamiętamy dopiero po udanym
zapisie i tylko dla tego samego slotu i intencji.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime

from ..engines.sell_xset import sell_xset
from ..guards import state_fresh

LIVE_EXPORT_KIND = "slot_live_export"
NOTE_SELL_XSET = "sell_xset"
NOTE_SELL_NO_LOAD = "sell_no_load"
NOTE_SELL_BELOW_MIN = "sell_below_min"
# Odczyt ponad tyle × moc znamionowa to błąd czujnika, nie moc.
_ABSURD_RATED_FACTOR = 2.0

SlotKey = tuple[datetime, datetime, str]


@dataclass(frozen=True)
class LiveExportMemory:
    # ostatni ważny odczyt poboru domu [W] — podstawa ścieżki zapasowej
    last_load_w: float | None = None
    # nastawa ostatnio ZAPISANA do falownika i jej slot (początek, koniec, intencja)
    written_for: SlotKey | None = None
    written_w: float | None = None

    def with_load(self, load_w: float | None) -> "LiveExportMemory":
        return self if load_w is None else replace(self, last_load_w=load_w)

    def prev_for(self, key: SlotKey) -> float | None:
        return self.written_w if self.written_for == key else None

    def with_written(self, key: SlotKey, value_w: float) -> "LiveExportMemory":
        return replace(self, written_for=key, written_w=value_w)

    def forget_written(self) -> "LiveExportMemory":
        if self.written_for is None and self.written_w is None:
            return self
        return replace(self, written_for=None, written_w=None)


@dataclass(frozen=True)
class LiveExport:
    """Przeliczenie jednego cyklu (do decyzji, notatki i `commit`)."""
    key: SlotKey
    battery_w: float
    pv_w: float | None
    load_w: float | None
    xset_w: float
    no_load: bool

    def note(self) -> str:
        def w(v: float | None) -> str:
            return "-" if v is None else f"{v:.0f}"
        return (f"{NOTE_SELL_XSET}:battery={w(self.battery_w)},pv={w(self.pv_w)},"
                f"load={w(self.load_w)},xset={w(self.xset_w)}")


def valid_reading(value: float | None, age_s: float | None, max_state_age_s: float,
                  rated_power_w: float) -> float | None:
    """Odczyt mocy, któremu wolno ufać, albo None (reguła w opisie modułu)."""
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return None
    if value < 0.0:
        return None
    if math.isfinite(rated_power_w) and rated_power_w > 0.0 and value > _ABSURD_RATED_FACTOR * rated_power_w:
        return None
    if not state_fresh(age_s, max_state_age_s):
        return None
    return float(value)


def export_ceiling(export_limit_enabled: bool | None, export_limit_w: float | None) -> float | None:
    """Limit eksportu liczy się tylko przy włączonym ograniczniku; włączony bez wartości = 0."""
    if export_limit_enabled is not True:
        return None
    return math.nan if export_limit_w is None else export_limit_w   # NaN → pułap 0 w `sell_xset`


def compute(*, key: SlotKey, battery_w: float, pv_w: float | None, load_w: float | None,
            export_limit_w: float | None, rated_power_w: float, memory: LiveExportMemory
            ) -> tuple[LiveExport, LiveExportMemory]:
    """Nastawa eksportu i pamięć po odczycie (`pv_w`/`load_w` już zwalidowane albo None)."""
    memory = memory.with_load(load_w)
    if memory.written_for is not None and memory.written_for != key:
        memory = memory.forget_written()        # inny slot albo intencja — histereza od nowa
    xset = sell_xset(battery_w=battery_w, pv_w=pv_w, load_w=load_w,
                     readings_ok=pv_w is not None and load_w is not None,
                     last_known_load_w=memory.last_load_w, export_limit_w=export_limit_w,
                     rated_power_w=rated_power_w, prev_xset_w=memory.prev_for(key))
    live = LiveExport(key=key, battery_w=battery_w, pv_w=pv_w, load_w=load_w, xset_w=xset,
                      no_load=memory.last_load_w is None)
    return live, memory
