"""Sprzedaż z mocą `slot_live_export`: nastawa eksportu z odczytów bieżącego cyklu.

Moc slotu sprzedaży to moc baterii po stronie AC; tryb sprzedaży falownika czyta nastawę
jako eksport do sieci ponad pokrycie domu. Cykl zamienia więc moc baterii na nastawę
`sell_xset(bateria + PV − dom)` — PO strażnikach (działają na mocy baterii) i PRZED
dopasowaniem do encji i throttlingiem (te działają już na nastawie eksportu).

Odczyt PV albo poboru jest ważny tylko, gdy jest liczbą skończoną, NIEUJEMNĄ, wiarygodną
i świeżą według reguły strażnika I-9 (ten sam limit wieku profilu; wiek `inf` = brak
znacznika czasu, wiek ujemny = przesunięcie zegara — oba nieświeże). Wiarygodność: PV nie
większe niż 2 × moc znamionowa (gdy ją znamy; przewymiarowanie DC mieści się w tym z
zapasem), pobór nie większy niż `LOAD_MAX_W` — dom bierze z sieci niezależnie od mocy
falownika, więc pobór ponad jego moc to prawdziwy pobór, nie błąd czujnika. Odczyt ujemny
albo absurdalny NIE jest przycinany do 0: pobór liczony (PV + bateria − sieć) bywa chwilowo
ujemny, a przycięty do 0 podniósłby nastawę eksportu. Nieważny odczyt = brak odczytu
(ścieżka zapasowa `bateria − ostatni ważny pobór`).

Pamięć (`LiveExportMemory`) jest niezmienna — cykl i `commit` podmieniają ją w całości.
Ostatni ważny pobór to OBSERWACJA: odświeża go każdy ważny odczyt w każdym cyklu, w każdym
trybie, także na sucho (jak w implementacji referencyjnej — ścieżka zapasowa ma najnowszy
ważny odczyt). Nastawę pamiętamy dopiero po udanym zapisie i tylko dla tego samego slotu
i intencji; pauza, powrót do stanu bazowego i każda porażka zapisu ją kasują.

Tryb bezpośredni (rejestry) liczy tę samą nastawę z odczytu falownika (PV i pobór z mapy
`read` profilu, wiek = wiek odczytu), z trzema różnicami:

* bez zgadywania: brak, stary albo niewiarygodny odczyt PV lub poboru (albo nieznana moc
  znamionowa) = slot w trybie neutralnym, nie ścieżka zapasowa `bateria − ostatni pobór`;
* strefa martwa dla NVM: do wzoru idzie szczyt poboru NETTO (pobór − PV) z ostatnich
  `DIRECT_PEAK_WINDOW_S` (próbki tylko z ważnych odczytów, w każdym cyklu i trybie).
  Wzrost poboru obniża nastawę od razu, spadek podnosi ją dopiero po wyjściu szczytu z okna.
  Bateria nie oddaje więc więcej niż plan + histereza 150 W (jak referencja), a wahania
  domu ±300 W nie przepisują 47512 co minutę: sama histereza 150 W dawała przy nich średnio
  34 zapisy/h (symulacja cyklu, 200 godzin) — budżet 144 zapisy/klucz/dobę wyczerpany po
  ~4 h sprzedaży i powrót do trybu bazowego w środku slotu; z oknem 10 min średnio 2,6/h,
  najwyżej 7 (udział godzinowy budżetu: 144/24 = 6). Ceną jest eksport niższy od planu
  o rozrzut poboru w oknie (bezpieczny kierunek: bateria oddaje mniej), a duży spadek
  poboru podnosi eksport z opóźnieniem do 10 min;
* pamięć próbek żyje tylko w procesie: po restarcie okno i zapisana nastawa są puste.
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
NOTE_SELL_NO_RATED = "sell_no_rated"
NOTE_SELL_BELOW_MIN = "sell_below_min"
NOTE_SELL_NO_READING = "sell_no_reading"
# tryb bezpośredni: jedna nieważna próbka — ostatnia ważna para (PV, pobór) przez jeden cykl
NOTE_SELL_READING_HELD = "sell_reading_held"
# tryb bezpośredni: puste okno, a PV dopycha nastawę do mocy znamionowej — PV nieprzyjęte w tym cyklu
NOTE_SELL_PV_UNCONFIRMED = "sell_pv_unconfirmed"
# Odczyt ponad tyle × moc znamionowa to błąd czujnika, nie moc.
PV_MAX_RATED_FACTOR = 2.0
# Pobór domu ponad tyle to błąd czujnika (przyłącze domu jest dużo mniejsze).
LOAD_MAX_W = 100_000.0
# Tryb bezpośredni: okno szczytu poboru netto (strefa martwa NVM, opis w nagłówku modułu).
DIRECT_PEAK_WINDOW_S = 600.0
# Tryb bezpośredni: najstarsza para (PV, pobór), którą wolno przetrzymać na jedną nieważną próbkę —
# para z poprzedniego cyklu (co 60 s) z zapasem na opóźnienie; starsza (cykle bez decyzji) = brak pary.
HELD_PAIR_MAX_AGE_S = 150.0

SlotKey = tuple[datetime, datetime, str]


@dataclass(frozen=True)
class LiveExportMemory:
    # ostatni ważny odczyt poboru domu [W] — podstawa ścieżki zapasowej
    last_load_w: float | None = None
    # nastawa ostatnio ZAPISANA do falownika i jej slot (początek, koniec, intencja)
    written_for: SlotKey | None = None
    written_w: float | None = None
    # tryb bezpośredni: próbki (czas monotoniczny, pobór − PV [W]) z ważnych odczytów w oknie
    net_samples: tuple[tuple[float, float], ...] = ()
    # tryb bezpośredni: ostatnia ważna para (PV, pobór) i liczba kolejnych cykli bez niej — jedna
    # nieważna próbka korzysta z pary przez jeden cykl, druga z rzędu = slot w trybie neutralnym;
    # `last_pair_at` — czas monotoniczny obserwacji pary (wiek, `HELD_PAIR_MAX_AGE_S`)
    last_pair: tuple[float, float] | None = None
    last_pair_at: float | None = None
    invalid_streak: int = 0

    def with_pair(self, pv_w: float | None, load_w: float | None, now_mono: float) -> "LiveExportMemory":
        """Obserwacja pary (PV, pobór) w KAŻDYM cyklu (już zwalidowanej; None = nieważna)."""
        if pv_w is not None and load_w is not None:
            return replace(self, last_pair=(pv_w, load_w), last_pair_at=now_mono, invalid_streak=0)
        return replace(self, invalid_streak=self.invalid_streak + 1)

    def held_pair(self, now_mono: float) -> tuple[float, float] | None:
        """Para z poprzedniego cyklu dla PIERWSZEJ nieważnej próbki z rzędu, nie starsza niż
        `HELD_PAIR_MAX_AGE_S`; inaczej None."""
        at = self.last_pair_at
        if self.invalid_streak != 1 or at is None or not 0.0 <= now_mono - at < HELD_PAIR_MAX_AGE_S:
            return None
        return self.last_pair

    def window_count(self, now_mono: float, window_s: float) -> int:
        return sum(1 for t, _ in self.net_samples if 0.0 <= now_mono - t < window_s)

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

    def with_net(self, now_mono: float, net_w: float | None, window_s: float) -> "LiveExportMemory":
        """Próbka poboru netto (None = brak ważnego odczytu) i okno przycięte do `window_s`.

        Próbka z przyszłości (zegar cofnięty) wypada — okno nie może jej trzymać bez końca.
        """
        kept = tuple((t, v) for t, v in self.net_samples if 0.0 <= now_mono - t < window_s)
        if net_w is not None:
            kept += ((now_mono, net_w),)
        return self if kept == self.net_samples else replace(self, net_samples=kept)

    def net_peak(self, now_mono: float, window_s: float) -> float | None:
        values = [v for t, v in self.net_samples if 0.0 <= now_mono - t < window_s]
        return max(values) if values else None


@dataclass(frozen=True)
class LiveExport:
    """Przeliczenie jednego cyklu (do decyzji, notatki i `commit`)."""
    key: SlotKey
    battery_w: float
    pv_w: float | None
    load_w: float | None
    xset_w: float
    no_load: bool
    # pułap nieznany (ani mocy znamionowej, ani zakresu encji mocy) — nastawa 0 W
    no_rated: bool = False
    # nastawa pod minimum encji mocy — slot zszedł do trybu neutralnego, nic nie zapisujemy
    below_min: bool = False
    # tryb bezpośredni: brak ważnego odczytu PV/poboru (stary, brakujący, niewiarygodny)
    no_reading: bool = False
    # tryb bezpośredni: slot zszedł do trybu neutralnego (brak odczytu albo mocy znamionowej)
    degraded: bool = False

    def note(self) -> str:
        def w(v: float | None) -> str:
            return "-" if v is None else f"{v:.0f}"
        return (f"{NOTE_SELL_XSET}:battery={w(self.battery_w)},pv={w(self.pv_w)},"
                f"load={w(self.load_w)},xset={w(self.xset_w)}")


def pv_limit_w(rated_power_w: float | None) -> float | None:
    """Górna granica wiarygodnego PV; None = moc znamionowa nieznana (bez granicy)."""
    if rated_power_w is None or not math.isfinite(rated_power_w) or rated_power_w <= 0.0:
        return None
    return PV_MAX_RATED_FACTOR * rated_power_w


def valid_reading(value: float | None, age_s: float | None, max_state_age_s: float,
                  limit_w: float | None) -> float | None:
    """Odczyt mocy, któremu wolno ufać, albo None (reguła w opisie modułu)."""
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return None
    if value < 0.0:
        return None
    if limit_w is not None and value > limit_w:
        return None
    if not state_fresh(age_s, max_state_age_s):
        return None
    return float(value)


def valid_load(value: float | None, age_s: float | None, max_state_age_s: float) -> float | None:
    """Pobór domu, któremu wolno ufać, albo None."""
    return valid_reading(value, age_s, max_state_age_s, LOAD_MAX_W)


def export_ceiling(export_limit_enabled: bool | None, export_limit_w: float | None) -> float | None:
    """Limit eksportu liczy się tylko przy włączonym ograniczniku; włączony bez wartości = 0."""
    if export_limit_enabled is not True:
        return None
    return math.nan if export_limit_w is None else export_limit_w   # NaN → pułap 0 w `sell_xset`


def compute(*, key: SlotKey, battery_w: float, pv_w: float | None, load_w: float | None,
            export_limit_w: float | None, rated_power_w: float | None, memory: LiveExportMemory
            ) -> tuple[LiveExport, LiveExportMemory]:
    """Nastawa eksportu i pamięć po zmianie slotu (`pv_w`/`load_w` już zwalidowane albo None).

    Wołający odświeżył już w `memory` ostatni ważny pobór obserwacją tego cyklu.
    """
    if memory.written_for is not None and memory.written_for != key:
        memory = memory.forget_written()        # inny slot albo intencja — histereza od nowa
    xset = sell_xset(battery_w=battery_w, pv_w=pv_w, load_w=load_w,
                     readings_ok=pv_w is not None and load_w is not None,
                     last_known_load_w=memory.last_load_w, export_limit_w=export_limit_w,
                     rated_power_w=rated_power_w, prev_xset_w=memory.prev_for(key))
    live = LiveExport(key=key, battery_w=battery_w, pv_w=pv_w, load_w=load_w, xset_w=xset,
                      no_load=memory.last_load_w is None, no_rated=rated_power_w is None)
    return live, memory
