"""Guardy ze stanem: I-6 (throttling zapisów NVM) i I-8 (anty-oscylacja).

Port `vb_throttle_*` i `vb_direction_*` z `vb_guards.c`. Pamięć I-6 przesuwa się
WYŁĄCZNIE po udanym zapisie — nieudana próba, odmowa ani rejestr nieobsługiwany nie
mogą udawać zapisu. Właścicielem stanu jest pętla wykonawcza (jeden obiekt na falownik).
"""
from __future__ import annotations

from collections import deque
from typing import Iterable, Mapping

_DIRECTIONAL = ("charge", "discharge")
_DIRECTIONS = _DIRECTIONAL + ("idle", "neutral")
# Rejestry trzymają liczby całkowite: różnica mniejsza niż kwant jest nieobserwowalna.
_REGISTER_QUANTUM = 1.0


class WriteThrottle:
    """I-6: każdy zapis nastawy to cykl pamięci nieulotnej falownika.

    Wartość niezmieniona nie jedzie nigdy; zmieniona — nie częściej niż co
    `min_interval_s` na klucz. Klucze to wynik `Params.flatten()`.

    `now_s` MUSI pochodzić z zegara monotonicznego (`time.monotonic()`,
    `loop.time()`), nie ze ściennego. Cofnięcie zegara jest traktowane jak upływ
    interwału — błąd idzie w stronę ponownej synchronizacji, nie godzinnej ciszy.
    """

    def __init__(self, min_interval_s: float) -> None:
        self._min = min_interval_s
        self._value: dict[str, float | str] = {}
        self._at: dict[str, float] = {}

    def filter(self, flat: Mapping[str, float | str], now_s: float) -> set[str]:
        """Klucze, które wolno teraz zapisać."""
        out: set[str] = set()
        for key, value in flat.items():
            if key in self._value:
                # Wartość niezmieniona nie jedzie NIGDY — nawet po upływie interwału.
                if self._value[key] == value:
                    continue
                at = self._at[key]
                if at <= now_s and now_s - at < self._min:
                    continue
            out.add(key)
        return out

    def pending(self, flat: Mapping[str, float | str], now_s: float) -> set[str]:
        """Klucze zmienione względem pamięci, które interwał jeszcze wstrzymuje.

        Dopełnienie `filter` po stronie zmian: wołający, który zapisuje grupę kluczy
        razem (tryb i jego nastawa), musi wiedzieć, że członek grupy czeka.
        """
        out: set[str] = set()
        for key, value in flat.items():
            if key in self._value and self._value[key] != value:
                at = self._at[key]
                if at <= now_s and now_s - at < self._min:
                    out.add(key)
        return out

    def record(self, flat: Mapping[str, float | str], written: Iterable[str], now_s: float) -> None:
        """Zapamiętuje TYLKO klucze faktycznie zapisane (lista udanych zapisów)."""
        for key in written:
            if key in flat:
                self._value[key] = flat[key]
                self._at[key] = now_s

    def reconcile(self, actual: Mapping[str, float | str]) -> int:
        """Kasuje pamięć tam, gdzie falownik ma co innego (zmiana z zewnątrz).

        Niczego nie zapisuje — tylko przywraca throttlingowi prawdę o stanie
        urządzenia, żeby następny cykl mógł zapisać wartość z planu. `actual` ma
        zawierać wyłącznie klucze, które da się odczytać: brak odczytu udawałby
        rozjazd i kasował pamięć w każdym cyklu. Zwraca liczbę skasowanych kluczy.

        Wartości muszą mieć postać `Params.flatten()`: tryb jako NAZWA, flagi jako
        liczby 0/1, wielkości w jednostkach fizycznych. Tekst tam, gdzie pamięć ma
        liczbę (lub odwrotnie), to błąd wołającego — `TypeError`, bo po cichu
        kasowałby pamięć w każdym cyklu i przepisywał nastawę do NVM.
        """
        dropped = 0
        for key, real in actual.items():
            if key not in self._value:
                continue
            mine = self._value[key]
            if isinstance(mine, str) != isinstance(real, str):
                raise TypeError(
                    f"reconcile({key!r}): {type(real).__name__} zamiast "
                    f"{type(mine).__name__} — odczyt musi mieć postać Params.flatten()"
                )
            if isinstance(mine, str):
                same = mine == real
            else:
                # Plan niesie ułamki (np. 625,6 W), rejestr przyjmuje 626 — to nie rozjazd.
                same = abs(float(mine) - float(real)) < _REGISTER_QUANTUM
            if not same:
                del self._value[key]
                del self._at[key]
                dropped += 1
        return dropped


class DirectionLimiter:
    """I-8: budżet zmian kierunku ładowanie ↔ rozładowanie w oknie czasu.

    Używany wyłącznie dla modelu `mode_setpoint` — przy `time_window` falownik sam
    przełącza się między programami, a budżet z profilu jest ignorowany.
    Kierunki: `charge`/`discharge` (kierunkowe) oraz `idle`/`neutral` (neutralne —
    nie zużywają budżetu i nie kasują pamięci ostatniego kierunku). Każdy inny
    napis to `ValueError` — nieznany kierunek nie może po cichu omijać budżetu.

    `now_s` MUSI pochodzić z zegara monotonicznego. Wpisy „z przyszłości" (zegar
    cofnięty) są odrzucane, żeby cofnięcie nie blokowało zmian przez godzinę.
    """

    def __init__(self, max_changes_per_hour: int, window_s: float = 3600.0, history: int = 16) -> None:
        self._max = max_changes_per_hour
        self._window = window_s
        # Pojemność nie mniejsza niż budżet — inaczej wypadający najstarszy wpis
        # po cichu wyłączałby I-8 dla budżetów z profilu większych niż historia.
        self._changes: deque[float] = deque(maxlen=max(history, max_changes_per_hour))
        self._current: str | None = None

    @staticmethod
    def _check(direction: str) -> None:
        if direction not in _DIRECTIONS:
            raise ValueError(f"nieznany kierunek: {direction!r}")

    def _prune(self, now_s: float) -> None:
        # Poza oknem — bez wpływu na wynik; „z przyszłości" — zegar się cofnął.
        boundary = now_s - self._window
        kept = [t for t in self._changes if boundary <= t <= now_s]
        if len(kept) != len(self._changes):
            self._changes = deque(kept, maxlen=self._changes.maxlen)

    def _is_change(self, direction: str) -> bool:
        # Pierwsze ustawienie nie jest ZMIANĄ — nie ma względem czego.
        return direction in _DIRECTIONAL and self._current is not None and self._current != direction

    def allows(self, direction: str, now_s: float) -> bool:
        self._check(direction)
        if not self._is_change(direction):
            return True
        boundary = now_s - self._window
        # Granica włącznie: wpis sprzed dokładnie `window_s` jeszcze się liczy.
        in_window = sum(1 for t in self._changes if boundary <= t <= now_s)
        return in_window < self._max

    def record(self, direction: str, now_s: float) -> None:
        """Zapisuje kierunek wykonanego cyklu.

        Wołać w KAŻDYM cyklu, który przeszedł `allows`, po próbie zapisu —
        niezależnie od jej wyniku (jak referencyjny wykonawca). Kierunek to
        kierunek zapisywanego trybu, nie intencji planu.
        """
        self._check(direction)
        self._prune(now_s)
        if self._is_change(direction):
            self._changes.append(now_s)
        # Przejście przez tryb neutralny nie kasuje pamięci kierunku.
        if direction in _DIRECTIONAL:
            self._current = direction
