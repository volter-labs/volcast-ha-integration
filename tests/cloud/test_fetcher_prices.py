import asyncio

from custom_components.volcast.cloud.fetcher import ScheduleFetcher

from .test_fetcher import Cloud, doc

PRICES = {"currency": "PLN", "interval_min": 60, "date_today": "2026-11-20", "date_tomorrow": None,
          "generated_at": "2026-11-20T08:00:00Z",
          "today": [{"start": "2026-11-20T10:00:00+01:00", "buy": 1.0, "sell": 0.3}], "tomorrow": []}
OTHER = PRICES | {"today": [{"start": "2026-11-20T10:00:00+01:00", "buy": 2.0, "sell": 0.4}]}


def make(*responses, with_cb=True):
    got = {"plans": [], "prices": []}

    async def on_plan(raw, sched):
        got["plans"].append(raw)

    async def noop(*a):
        return None

    kw = {"on_prices": got["prices"].append} if with_cb else {}
    return ScheduleFetcher(Cloud(*responses), on_plan=on_plan, on_consent=noop, on_auth_failure=noop, **kw), got


def test_prices_outside_signature_and_plan_raw():
    f, got = make(doc(), doc() | {"prices": PRICES}, doc() | {"prices": OTHER})
    assert asyncio.run(f.async_refresh()) == "accepted"
    assert asyncio.run(f.async_refresh()) == "unchanged"      # nowe ceny = nie nowy plan
    assert asyncio.run(f.async_refresh()) == "unchanged"
    assert len(got["plans"]) == 1 and "prices" not in got["plans"][0]


def test_prices_not_stored_when_plan_changes():
    f, got = make(doc(sid="a") | {"prices": PRICES}, doc(sid="b") | {"prices": PRICES})
    asyncio.run(f.async_refresh())
    asyncio.run(f.async_refresh())
    assert len(got["plans"]) == 2 and all("prices" not in p for p in got["plans"])


def test_block_is_parsed_and_published():
    f, got = make(doc() | {"prices": PRICES}, doc() | {"prices": OTHER})
    assert f.prices is None
    asyncio.run(f.async_refresh())
    assert f.prices.currency == "PLN" and f.prices.slots[0].buy == 1.0
    asyncio.run(f.async_refresh())
    assert f.prices.slots[0].buy == 2.0 and len(got["prices"]) == 2


def test_explicit_null_clears():
    f, got = make(doc() | {"prices": PRICES}, doc() | {"prices": None})
    asyncio.run(f.async_refresh())
    assert f.prices is not None
    assert asyncio.run(f.async_refresh()) == "unchanged"
    assert f.prices is None and got["prices"][-1] is None


def test_absent_key_keeps_last_good_block():
    f, got = make(doc() | {"prices": PRICES}, doc(), doc() | {"prices": OTHER})
    asyncio.run(f.async_refresh())
    first = f.prices
    asyncio.run(f.async_refresh())
    assert f.prices is first and len(got["prices"]) == 1       # bez zmiany i bez powiadomienia
    asyncio.run(f.async_refresh())
    assert f.prices.slots[0].buy == 2.0


def test_absent_key_without_previous_block_stays_none():
    f, _ = make(doc())
    asyncio.run(f.async_refresh())
    assert f.prices is None


def test_malformed_block_keeps_previous_and_logs_once(caplog):
    import logging
    f, got = make(doc() | {"prices": PRICES}, doc() | {"prices": "junk"}, doc() | {"prices": {"today": 5}},
                  doc() | {"prices": OTHER}, doc() | {"prices": 5})
    asyncio.run(f.async_refresh())
    first = f.prices
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(f.async_refresh()) == "unchanged" and f.prices is first
        assert asyncio.run(f.async_refresh()) == "unchanged" and f.prices is first
    assert sum("malformed" in r.message for r in caplog.records) == 1
    asyncio.run(f.async_refresh())                              # poprawny blok kasuje serię
    assert f.prices.slots[0].buy == 2.0
    with caplog.at_level(logging.DEBUG):
        asyncio.run(f.async_refresh())
    assert sum("malformed" in r.message for r in caplog.records) == 2


def test_failed_poll_keeps_previous_data():
    f, got = make(doc() | {"prices": PRICES}, None, RuntimeError("x"))
    asyncio.run(f.async_refresh())
    assert asyncio.run(f.async_refresh()) == "network"
    assert asyncio.run(f.async_refresh()) == "error"
    assert f.prices is not None and len(got["prices"]) == 1


def test_prices_applied_even_when_plan_rejected():
    f, _ = make(doc(slots=[{"mode": "nonsense"}]) | {"prices": PRICES})
    assert asyncio.run(f.async_refresh()) == "rejected" and f.prices is not None


def test_callback_failure_does_not_stop_the_plan():
    def boom(_):
        raise RuntimeError("x")

    f, got = make(doc() | {"prices": PRICES})
    f._on_prices = boom
    assert asyncio.run(f.async_refresh()) == "accepted" and len(got["plans"]) == 1


def test_works_without_callback():
    f, _ = make(doc() | {"prices": PRICES}, with_cb=False)
    assert asyncio.run(f.async_refresh()) == "accepted" and f.prices is not None
