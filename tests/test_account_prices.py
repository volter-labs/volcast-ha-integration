"""Blok `prices` z `get-schedule`: parser (typy, odrzucanie wadliwych slotów) i wybór slotu dla chwili."""
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.volcast.core.account_prices import parse_prices


def hourly(day="2026-11-20", off="+01:00", sell=0.3):
    return [{"start": f"{day}T{h:02d}:00:00{off}", "buy": 1.0 + h / 100, "sell": sell} for h in range(24)]


def quarter(day="2026-11-20", off="+01:00"):
    return [{"start": f"{day}T{h:02d}:{m:02d}:00{off}", "buy": h + m / 100, "sell": None}
            for h in range(24) for m in (0, 15, 30, 45)]


def block(**kw):
    base = {"currency": "PLN", "interval_min": 60, "date_today": "2026-11-20", "date_tomorrow": None,
            "generated_at": "2026-11-20T08:00:00Z", "today": hourly(), "tomorrow": []}
    return base | kw


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def test_parses_a_valid_block():
    p = parse_prices(block())
    assert p.currency == "PLN" and p.interval_min == 60 and len(p.slots) == 24
    assert p.generated_at == "2026-11-20T08:00:00Z"


@pytest.mark.parametrize("bad", [None, [], "x", {}, block(currency=""), block(currency=5),
                                 block(interval_min=0), block(interval_min="60"), block(interval_min=True),
                                 block(today="x"), block(today=[]), block(today=None)])
def test_unusable_block_is_none(bad):
    assert parse_prices(bad) is None


def test_malformed_slots_are_dropped():
    bad = [{"start": "nope", "buy": 1.0, "sell": 0.1},                    # zły czas
           {"start": "2026-11-20T00:00:00", "buy": 1.0, "sell": 0.1},     # bez strefy
           {"start": "2026-11-20T01:00:00+01:00", "buy": "1", "sell": 0.1},   # cena nie jest liczbą
           {"start": "2026-11-20T02:00:00+01:00", "buy": True, "sell": 0.1},
           {"start": "2026-11-20T03:00:00+01:00", "buy": float("nan"), "sell": 0.1},
           {"start": "2026-11-20T04:00:00+01:00", "buy": 1.0, "sell": "x"},   # zła sprzedaż
           "junk", None, {"buy": 1}]
    good = {"start": "2026-11-20T05:00:00+01:00", "buy": 1.5, "sell": 0.2}
    p = parse_prices(block(today=bad + [good]))
    assert [s.buy for s in p.slots] == [1.5]


def test_slot_covering_now_hourly_and_quarter():
    p = parse_prices(block())
    s = p.slot_at(utc(2026, 11, 20, 9, 59))            # 10:59 lokalnie
    assert s.buy == pytest.approx(1.10)
    assert p.slot_at(utc(2026, 11, 20, 10, 0)).buy == pytest.approx(1.11)
    q = parse_prices(block(interval_min=15, today=quarter()))
    assert q.slot_at(utc(2026, 11, 20, 9, 29)).buy == pytest.approx(10.15)   # 10:29 → slot 10:15
    assert q.slot_at(utc(2026, 11, 20, 9, 30)).buy == pytest.approx(10.30)


def test_no_slot_outside_data():
    p = parse_prices(block())
    assert p.slot_at(utc(2026, 11, 19, 22, 59)) is None      # przed 00:00 lokalnie
    assert p.slot_at(utc(2026, 11, 20, 23, 0)) is None       # 00:00 jutra, brak jutra


def test_tomorrow_covers_after_midnight():
    p = parse_prices(block(date_tomorrow="2026-11-21", tomorrow=hourly("2026-11-21", sell=None)))
    s = p.slot_at(utc(2026, 11, 20, 23, 30))                 # 00:30 21.11
    assert s is not None and s.day == "2026-11-21" and s.sell is None


def test_next_boundary():
    p = parse_prices(block())
    assert p.next_boundary(utc(2026, 11, 20, 9, 30)) == utc(2026, 11, 20, 10, 0)
    # po ostatnim slocie: koniec ostatniego slotu (dane się kończą)
    assert p.next_boundary(utc(2026, 11, 20, 22, 30)) == utc(2026, 11, 20, 23, 0)
    assert p.next_boundary(utc(2026, 11, 21, 3, 0)) is None


def test_day_lists_follow_the_slot_of_now():
    p = parse_prices(block(date_tomorrow="2026-11-21", tomorrow=hourly("2026-11-21")))
    today, tomorrow = p.days(utc(2026, 11, 20, 12, 0), "buy")
    assert len(today) == 24 and len(tomorrow) == 24 and today[0]["start"] == "2026-11-20T00:00:00+01:00"
    assert set(today[0]) == {"start", "price"}
    # po północy „jutro" jest już „dziś" (do czasu kolejnego odpytania)
    today, tomorrow = p.days(utc(2026, 11, 20, 23, 30), "buy")
    assert today[0]["start"].startswith("2026-11-21") and tomorrow == []


def test_sell_side_keeps_none():
    p = parse_prices(block(today=hourly(sell=None)))
    today, _ = p.days(utc(2026, 11, 20, 12, 0), "sell")
    assert all(x["price"] is None for x in today)


def test_gap_between_slots_ends_the_current_slot():
    two = [{"start": "2026-11-20T10:00:00+01:00", "buy": 1.0, "sell": 0.1},
           {"start": "2026-11-20T12:00:00+01:00", "buy": 2.0, "sell": 0.2}]
    p = parse_prices(block(today=two))
    assert p.next_boundary(utc(2026, 11, 20, 9, 30)) == utc(2026, 11, 20, 10, 0)    # 10:30 lokalnie → koniec slotu
    assert p.slot_at(utc(2026, 11, 20, 10, 0)) is None                              # 11:00 — luka
    assert p.next_boundary(utc(2026, 11, 20, 10, 0)) == utc(2026, 11, 20, 11, 0)    # do początku 12:00
