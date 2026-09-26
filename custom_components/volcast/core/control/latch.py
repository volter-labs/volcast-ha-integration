"""Zatrzask progu rezerwy dla I-1 (S-4/S-4b) — chroni pamięć nieulotną falownika.

Sam warunek `soc <= rezerwa` nie ma histerezy: bateria stojąca na progu w slocie
sprzedaży przełączałaby tryb rozładowania i neutralny w każdym cyklu. Trzy reguły:
1. pasmo `band_pp` — zwolnienie wyżej niż założenie (szum czujnika),
2. minimalny czas trwania stanu (drgania o dowolnej amplitudzie),
3. minimalny odstęp między zwolnieniami (zamyka ścieżkę awaryjną).
Wyraźne zejście pod rezerwę (`< rezerwa − deep_pp`) załącza natychmiast — ochrona
nie czeka nigdy; czekać wolno tylko ze zwolnieniem. `now` = zegar monotoniczny.

Zatrzask ma zawsze zakładać wołającego, który podał już przefiltrowany, świeży
odczyt (jak w referencji — po I-9/I-10). Ten port jest wołany PRZED strażnikami,
więc może dostać nieużywalny wsad (`None`/NaN/inf/spoza 0..100).
Fail-open (zwolnienie zatrzasku na taki wsad) rozjeżdżałby zatrzask na kolejnych,
poprawnych tikach — dlatego `engaged()` na nieużywalnym wejściu jest fail-closed:
zwraca `True`, stanu NIE zmienia (ten tik i tak blokuje strażnik wyżej).
"""
from __future__ import annotations

import math

BAND_PP = 3.0
ENGAGED_MIN_S = 1800.0
RELEASED_MIN_S = 7200.0


class ReserveLatch:
    def __init__(self, band_pp: float = BAND_PP, engaged_min_s: float = ENGAGED_MIN_S,
                 released_min_s: float = RELEASED_MIN_S, deep_pp: float | None = None) -> None:
        self.band_pp = band_pp
        self.engaged_min_s = engaged_min_s
        self.released_min_s = released_min_s
        self.deep_pp = band_pp if deep_pp is None else deep_pp
        self._engaged = False
        self._since: float | None = None
        self._last_release: float | None = None

    @property
    def cycle_min_s(self) -> float:
        return self.engaged_min_s + self.released_min_s

    @property
    def is_engaged(self) -> bool:
        return self._engaged

    def engaged(self, soc: float | None, reserve: float | None, now: float | None) -> bool:
        if not self._usable(soc) or not self._usable(reserve) or not self._usable(now):
            return True
        if not (0.0 <= soc <= 100.0) or not (0.0 <= reserve <= 100.0):
            return True
        new = self._decide(soc, reserve, now)
        if new != self._engaged:
            self._since = now
            if not new:
                self._last_release = now
        self._engaged = new
        return new

    def _decide(self, soc: float, reserve: float, now: float) -> bool:
        held = None if self._since is None else max(0.0, now - self._since)
        if self._engaged:
            if soc < reserve + self.band_pp:
                return True
            if held is not None and held < self.engaged_min_s:
                return True
            if self._last_release is not None and (now - self._last_release) < self.cycle_min_s:
                return True
            return False
        if soc < reserve - self.deep_pp:
            return True
        if soc <= reserve:
            if held is not None and held < self.released_min_s:
                return False
            return True
        return False

    @staticmethod
    def _usable(value: float | None) -> bool:
        return value is not None and math.isfinite(value)

    def copy(self) -> "ReserveLatch":
        c = ReserveLatch(self.band_pp, self.engaged_min_s, self.released_min_s, self.deep_pp)
        c._engaged, c._since, c._last_release = self._engaged, self._since, self._last_release
        return c
