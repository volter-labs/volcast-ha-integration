"""Ceny konta z bloku `prices` odpowiedzi `get-schedule` (kontrakt 5) — tylko do odczytu w encjach.

Blok niesie waluta, okno rynku (`interval_min`) i sloty `today` / `tomorrow` z cenami `buy` / `sell` za kWh
(ten sam plan, który pokazuje aplikacja). Parser nigdy nie rzuca: blok bez użytecznych slotów to `None`,
wadliwy slot jest pomijany. Ceny nie wchodzą do planu ani do jego podpisu.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

import homeassistant.util.dt as dt_util

_MAX_INTERVAL_MIN = 1440


def _price(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


@dataclass(frozen=True)
class PriceSlot:
    start: datetime          # chwila ze strefą
    start_raw: str           # napis z serwera (czas lokalny rynku) — do atrybutów
    day: str                 # data lokalna slotu (`YYYY-MM-DD`, z napisu serwera)
    buy: float
    sell: float | None       # brak ceny sprzedaży = None


@dataclass(frozen=True)
class AccountPrices:
    currency: str
    interval_min: int
    generated_at: str | None
    slots: tuple[PriceSlot, ...]          # rosnąco po starcie

    def slot_at(self, now: datetime) -> PriceSlot | None:
        """Slot z `start <= now < start + interval_min` (dziś, a po północy — jutro); brak = None."""
        width = timedelta(minutes=self.interval_min)
        for s in self.slots:
            if s.start <= now < s.start + width:
                return s
        return None

    def next_boundary(self, now: datetime) -> datetime | None:
        """Najbliższy początek slotu po `now`; gdy dane się kończą — koniec slotu bieżącego; inaczej None."""
        for s in self.slots:
            if s.start > now:
                return s.start
        cur = self.slot_at(now)
        return cur.start + timedelta(minutes=self.interval_min) if cur else None

    def days(self, now: datetime, side: str) -> tuple[list[dict], list[dict]]:
        """Listy `{start, price}` strony `side` (`buy`/`sell`) dla „dziś" i „jutro" — dzień „dziś" to dzień
        slotu z `now`, więc po północy (przed odpytaniem) jutrzejsze dane są już „dzisiaj"."""
        cur = self.slot_at(now)
        if cur is None:
            return [], []
        days = sorted({s.day for s in self.slots})
        later = days[days.index(cur.day) + 1] if days.index(cur.day) + 1 < len(days) else None
        pick = lambda day: [{"start": s.start_raw, "price": getattr(s, side)}      # noqa: E731
                            for s in self.slots if s.day == day]
        return pick(cur.day), (pick(later) if later else [])


def _slot(raw) -> PriceSlot | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("start"), str):
        return None
    start = dt_util.parse_datetime(raw["start"])
    buy = _price(raw.get("buy"))
    sell = raw.get("sell")
    if start is None or start.tzinfo is None or buy is None:
        return None
    if sell is not None and (sell := _price(sell)) is None:
        return None
    return PriceSlot(start, raw["start"], raw["start"][:10], buy, sell)


def parse_prices(block) -> AccountPrices | None:
    """Blok z odpowiedzi → `AccountPrices`; zły kształt, brak waluty/okna albo brak użytecznych slotów → None."""
    if not isinstance(block, dict):
        return None
    currency, interval = block.get("currency"), block.get("interval_min")
    if not isinstance(currency, str) or not currency.strip() or len(currency) > 8:
        return None
    if isinstance(interval, bool) or not isinstance(interval, int) or not 0 < interval <= _MAX_INTERVAL_MIN:
        return None
    raw = [r for side in ("today", "tomorrow") for r in (block.get(side) if isinstance(block.get(side), list) else [])]
    slots = sorted({s.start: s for s in map(_slot, raw) if s is not None}.values(), key=lambda s: s.start)
    if not slots:
        return None
    generated = block.get("generated_at")
    return AccountPrices(currency.strip(), interval, generated if isinstance(generated, str) else None, tuple(slots))
