"""Guardy ze stanem: I-6 (throttling zapisów NVM), I-8 (anty-oscylacja) i budżet zapisów NVM.

Port `vb_throttle_*` i `vb_direction_*` z `vb_guards.c`. Pamięć I-6 przesuwa się
WYŁĄCZNIE po udanym zapisie — nieudana próba, odmowa ani rejestr nieobsługiwany nie
mogą udawać zapisu. Właścicielem stanu jest pętla wykonawcza (jeden obiekt na falownik).
"""
from __future__ import annotations

import logging
import math
from collections import deque
from typing import Any, Iterable, Mapping

_LOGGER = logging.getLogger(__name__)

_DIRECTIONAL = ("charge", "discharge")
_DIRECTIONS = _DIRECTIONAL + ("idle", "neutral")
_UNKNOWN_DIRECTION = "?"
# Rejestry trzymają liczby całkowite: różnica mniejsza niż kwant jest nieobserwowalna.
_REGISTER_QUANTUM = 1.0
# Wartość po próbie zapisu o nieznanym skutku: różna od każdej nastawy.
_UNKNOWN = object()


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

    def known(self, key: str) -> float | str | None:
        """Wartość, którą według pamięci ma falownik: nasz zapis albo przyjęty odczyt.

        None, gdy pamięć jej nie zna (nigdy nie zapisano, rozjazd z odczytem skasował
        wpis albo wynik zapisu jest nieznany).
        """
        value = self._value.get(key)
        return None if value is _UNKNOWN else value

    def mark_unknown(self, keys: Iterable[str], now_s: float) -> None:
        """Wartość kluczy nieznana (zapis mógł dojść albo nie), odstęp I-6 zostaje.

        Pamięć nie może udawać, że falownik ma którąkolwiek wartość: klucz nie jest
        „niezmieniony", więc wróci do zapisu — ale nie wcześniej niż po interwale od
        tej próby, bo mogła trafić do NVM. Odczyt z urządzenia (`reconcile`) przywraca
        wartość, zachowując czas.
        """
        for key in keys:
            self._value[key] = _UNKNOWN
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
            if mine is _UNKNOWN:
                self._value[key] = real          # odczyt rozstrzyga; czas próby zostaje
                continue
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

    def allows(self, direction: str, now_s: float, *, round_trip: bool = False) -> bool:
        """Czy zmiana kierunku mieści się w budżecie.

        `round_trip=True`: zapis może zakończyć się powrotem do obecnego kierunku
        (cofnięcie trybu) — budżet musi pomieścić obie zmiany, tam i z powrotem.
        """
        self._check(direction)
        needed = (1 if self._is_change(direction) else 0) + (1 if round_trip else 0)
        if needed == 0:
            return True
        boundary = now_s - self._window
        # Granica włącznie: wpis sprzed dokładnie `window_s` jeszcze się liczy.
        in_window = sum(1 for t in self._changes if boundary <= t <= now_s)
        return in_window + needed <= self._max

    def mark_unknown(self) -> None:
        """Kierunek na falowniku nieznany (zapis trybu mógł dojść albo nie).

        Następny zapis kierunkowy liczy się wtedy jako zmiana, w którąkolwiek stronę —
        inaczej zmiana, która naprawdę zaszła, mogłaby ominąć budżet.
        """
        self._current = _UNKNOWN_DIRECTION

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


# Znacznik późniejszy niż „teraz" + tyle = skok zegara (NTP/RTC); przycinany do „teraz".
_FUTURE_SLACK_S = 300.0
_EXTRA_ENTRIES = 16


class WriteBudget:
    """Budżet ramek zapisu do pamięci nieulotnej w oknie kroczącym (zegar ścienny UTC).

    Liczy się wpis z `ts ≥ now − window`. Stan przeżywa restart (`to_list`/`from_list`),
    dlatego zegar ścienny — i dlatego odporność na jego skoki: wpis późniejszy niż
    `now + 5 min` jest przycinany do `now` (skok do przodu nie wyłącza budżetu, a zapisany
    znacznik „z przyszłości" nie blokuje zapisów na lata); cofnięty zegar niczego nie kasuje.
    Klucze to klucze płaskie (`mode`, `tou.3.soc`); `total` liczy wszystkie razem.
    """

    def __init__(self, per_key: int, total: int, window_s: float = 86400.0) -> None:
        for name, v in (("per_key", per_key), ("total", total)):
            if isinstance(v, bool) or not isinstance(v, int) or v < 1:
                raise ValueError(f"{name} musi być dodatnią liczbą całkowitą")
        if isinstance(window_s, bool) or not isinstance(window_s, (int, float)) \
                or not math.isfinite(window_s) or window_s <= 0:
            raise ValueError("okno budżetu musi być dodatnie")
        self.per_key = per_key
        self.total = total
        self.window_s = float(window_s)
        self._entries: deque[tuple[str, float]] = deque(maxlen=total + _EXTRA_ENTRIES)
        self._last_now: float | None = None
        self._warned = False
        self.hit = False

    @classmethod
    def for_profile(cls, profile) -> "WriteBudget | None":
        b = getattr(profile, "nvm_budget", None)
        if b is None:
            return None
        return cls(per_key=b.per_key, total=b.total, window_s=b.window_s)

    def _normalize(self, now_wall: float) -> None:
        self._last_now = now_wall
        limit = now_wall + _FUTURE_SLACK_S
        if any(ts > limit for _, ts in self._entries):
            if not self._warned:
                # Bez wartości czasu w logu — wystarczy fakt skoku.
                _LOGGER.warning("write budget: timestamps ahead of the clock clamped")
                self._warned = True
            entries = [(k, now_wall if ts > limit else ts) for k, ts in self._entries]
            entries.sort(key=lambda e: e[1])
            self._entries = deque(entries, maxlen=self._entries.maxlen)
        # Filtr, nie ucinanie z lewej: po cofnięciu zegara wpisy nie muszą być posortowane.
        boundary = now_wall - self.window_s
        if any(ts < boundary for _, ts in self._entries):
            self._entries = deque(((k, ts) for k, ts in self._entries if ts >= boundary),
                                  maxlen=self._entries.maxlen)

    def exhausted(self, keys: Iterable[str], now_wall: float) -> set[str]:
        """Klucze, dla których kolejna ramka przekroczyłaby budżet (odmowa zapisu — flaga `hit`)."""
        out = self._full(keys, now_wall)
        if out:
            self.hit = True
        return out

    def would_exceed(self, key: str, now_wall: float) -> bool:
        """Czy kolejna ramka klucza przekroczyłaby budżet — bez flagi `hit` (np. pominięta ponowna
        wysyłka zapisu po ciszy: żaden zapis nie został odmówiony)."""
        return bool(self._full((key,), now_wall))

    def _full(self, keys: Iterable[str], now_wall: float) -> set[str]:
        self._normalize(now_wall)
        per: dict[str, int] = {}
        for k, _ in self._entries:
            per[k] = per.get(k, 0) + 1
        full = len(self._entries) >= self.total
        return {k for k in keys if full or per.get(k, 0) >= self.per_key}

    def note(self, key: str, now_wall: float) -> None:
        """Jedna wysłana ramka zapisu (także ponowiona i cofająca)."""
        self._normalize(now_wall)
        self._entries.append((key, now_wall))

    def counts(self, now_wall: float) -> dict[str, int]:
        """Ramki w oknie na klucz (diagnostyka)."""
        self._normalize(now_wall)
        out: dict[str, int] = {}
        for k, _ in self._entries:
            out[k] = out.get(k, 0) + 1
        return out

    def to_list(self) -> list[list]:
        limit = None if self._last_now is None else self._last_now + _FUTURE_SLACK_S
        return [[k, ts if limit is None or ts <= limit else self._last_now] for k, ts in self._entries]

    @classmethod
    def from_list(cls, raw: Any, per_key: int, total: int, window_s: float, *,
                  now_wall: float) -> "WriteBudget":
        b = cls(per_key=per_key, total=total, window_s=window_s)
        entries: list[tuple[str, float]] = []
        for item in raw if isinstance(raw, list) else ():
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            key, ts = item
            if not isinstance(key, str) or not key:
                continue
            if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not math.isfinite(ts):
                continue
            entries.append((key, float(ts)))
        entries.sort(key=lambda e: e[1])
        b._entries.extend(entries)
        b._normalize(now_wall)
        return b
