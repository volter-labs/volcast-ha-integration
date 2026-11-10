"""Ceny z akcji Home Assistanta (`return_response`) — warstwa czysta.

Odpowiedzi atrap mają DOKŁADNIE kształt serializacji z kodu tych integracji
(rdzeń HA: amberelectric, tibber, nordpool, energyzero, easyenergy; HACS:
entsoe). Odpowiedź zamieniamy na atrybuty, które rozumie adapter atrybutów
(`core/prices.py`), więc obowiązuje ta sama reguła: dane niespójne → pusta
lista, nigdy częściowa.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.volcast.core import price_actions as pa
from custom_components.volcast.core.price_actions import (
    InvalidPriceSource,
    PriceActionPoller,
    attributes_from_responses,
    build_calls,
    parse_source,
)
from custom_components.volcast.core.prices import currency_from_attributes, intervals_from_attributes

WAW = ZoneInfo("Europe/Warsaw")
AMS = ZoneInfo("Europe/Amsterdam")
SYD = ZoneInfo("Australia/Sydney")
OSLO = ZoneInfo("Europe/Oslo")


# ── odpowiedzi w kształcie integracji ───────────────────────────────────────


def _day_starts(day: date, tz, minutes: int):
    start = datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
    t = start
    while t < end:
        yield t.astimezone(tz)
        t += timedelta(minutes=minutes)


def nordpool_response(day: date, area="SE3", minutes=15, base=400.0):
    return {area: [{"start": s.astimezone(timezone.utc).isoformat(),
                    "end": (s + timedelta(minutes=minutes)).astimezone(timezone.utc).isoformat(),
                    "price": base + i} for i, s in enumerate(_day_starts(day, OSLO, minutes))]}


def tibber_response(day: date, homes=("Home",), minutes=15):
    rows = [{"start_time": s.isoformat(timespec="milliseconds"), "price": 1.0 + i / 100}
            for i, s in enumerate(_day_starts(day, OSLO, minutes))]
    return {"prices": {h: list(rows) for h in homes}}


def energyzero_response(day: date, minutes=60):
    # `str(time_range.start_including)` — znacznik ze spacją i offsetem UTC.
    return {"prices": [{"price": 0.1 + i / 100, "timestamp": str(s.astimezone(timezone.utc))}
                       for i, s in enumerate(_day_starts(day, AMS, minutes))]}


def easyenergy_response(day: date, minutes=60, base=0.2):
    return {"prices": [{"timestamp": str(s.astimezone(timezone.utc)), "price": base + i / 100}
                       for i, s in enumerate(_day_starts(day, AMS, minutes))]}


def entsoe_response(day: date):
    return {"prices": [{"timestamp": s.isoformat(), "price": 0.05 + i / 100}
                       for i, s in enumerate(_day_starts(day, AMS, 60))]}


def amber_response(start_local: datetime, count=48, minutes=30, sign=1.0):
    rows = []
    for i in range(count):
        s = start_local + timedelta(minutes=minutes * i)
        e = s + timedelta(minutes=minutes)
        rows.append({"duration": minutes, "date": s.date().isoformat(), "nem_date": e.isoformat(),
                     "per_kwh": sign * (0.2 + i / 1000), "spot_per_kwh": 0.1,
                     # Amber znakuje start sekundą po pełnej minucie.
                     "start_time": (s + timedelta(seconds=1)).isoformat(), "end_time": e.isoformat(),
                     "renewables": 40, "spike_status": "none", "descriptor": "neutral"})
    return {"forecasts": rows}


def _intervals(attrs, sell=None, fallback=None, tz=WAW, now=None):
    currency = currency_from_attributes(attrs, fallback)
    return intervals_from_attributes(attrs, sell, currency, tz, now) if currency else []


# ── źródło: walidacja konfiguracji ──────────────────────────────────────────


@pytest.mark.parametrize("raw", [
    {"action": "amberelectric.get_forecasts", "data": {"config_entry_id": "a1", "channel_type": "general"}},
    {"action": "tibber.get_prices"},
    {"action": "nordpool.get_prices_for_date", "data": {"config_entry": "n1", "areas": "SE3", "currency": "SEK"}},
    {"action": "entsoe.get_energy_prices", "data": {"config_entry": "e1"}, "currency": "EUR", "unit": "kWh"},
    {"action": "entsoe.get_energy_prices", "data": {"config_entry": "e1"}, "currency": "EUR", "unit": "MWh"},
    {"action": "energyzero.get_energy_prices", "data": {"config_entry": "z1", "incl_vat": True}},
    {"action": "easyenergy.get_energy_usage_prices", "data": {"config_entry": "y1", "incl_vat": True}},
    {"action": "easyenergy.get_energy_return_prices", "data": {"config_entry": "y1"}},
])
def test_supported_actions_parse(raw):
    src = parse_source(raw)
    assert src.action == raw["action"] and src.domain == raw["action"].split(".")[0]
    assert set(pa.SUPPORTED_ACTIONS) == {
        "amberelectric.get_forecasts", "tibber.get_prices", "nordpool.get_prices_for_date",
        "entsoe.get_energy_prices", "energyzero.get_energy_prices",
        "easyenergy.get_energy_usage_prices", "easyenergy.get_energy_return_prices"}


@pytest.mark.parametrize("raw", [
    "tibber.get_prices",                                             # nie słownik
    {"action": "light.turn_on"},                                     # akcja spoza listy
    {"action": "zonneplan.get_prices"},                              # Zonneplan — tylko atrybuty
    {"action": "tibber.get_prices", "data": ["x"]},                  # dane nie słownikiem
    {"action": "tibber.get_prices", "currency": "zł"},               # waluta nie kodem
    {"action": "tibber.get_prices", "home": 7},
    {"action": "amberelectric.get_forecasts", "data": {"config_entry_id": "a1"}},   # bez kanału
    {"action": "nordpool.get_prices_for_date", "data": {"config_entry": "n1", "currency": "SEK"}},
    {"action": "nordpool.get_prices_for_date",
     "data": {"config_entry": "n1", "areas": ["SE3", "SE4"], "currency": "SEK"}},  # dwie strefy
    {"action": "nordpool.get_prices_for_date", "data": {"config_entry": "n1", "areas": "SE3"}},  # bez waluty
    {"action": "energyzero.get_energy_prices", "data": {"config_entry": "z1"}},     # bez incl_vat
    # ENTSO-e (HACS) ma własną skalę (kWh/MWh) i walutę, a odpowiedź nie niesie jednostki — obie jawnie.
    {"action": "entsoe.get_energy_prices", "data": {"config_entry": "e1"}, "currency": "EUR"},
    {"action": "entsoe.get_energy_prices", "data": {"config_entry": "e1"}, "currency": "EUR", "unit": "Wh"},
    {"action": "entsoe.get_energy_prices", "data": {"config_entry": "e1"}, "unit": "MWh"},
])
def test_invalid_sources_rejected(raw):
    with pytest.raises(InvalidPriceSource):
        parse_source(raw)


@pytest.mark.parametrize("areas", ["se3", ["se3"], " SE3 "])
def test_nordpool_area_is_sent_as_one_string(areas):
    # Rdzeń HA (starsze wersje) sprawdza `areas` przez `vol.In` przed `ensure_list` — lista nie przechodzi.
    src = parse_source({"action": "nordpool.get_prices_for_date",
                        "data": {"config_entry": "n1", "areas": areas, "currency": "SEK"}})
    assert src.data["areas"] == "SE3" and src.area == "SE3"


# ── wywołania: daty doby rynku ─────────────────────────────────────────────


NOW_WAW_10 = datetime(2026, 10, 31, 9, 0, tzinfo=timezone.utc)      # 10:00 w Warszawie


def test_per_day_calls_cover_today_and_tomorrow_in_market_time():
    src = parse_source({"action": "nordpool.get_prices_for_date",
                        "data": {"config_entry": "n1", "areas": ["se3"], "currency": "sek"}})
    calls = build_calls(src, NOW_WAW_10, WAW)
    assert [(day, data["date"]) for day, data in calls] == [(0, "2026-10-31"), (1, "2026-11-01")]
    assert calls[0][1]["config_entry"] == "n1" and calls[0][1]["currency"] == "SEK"

    before_dst = datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)
    tib = build_calls(parse_source({"action": "tibber.get_prices", "home": "Home"}), before_dst, WAW)
    # Tibber: chwile z offsetem; jutro to doba zmiany czasu (25 h), dane bez klucza `home`.
    assert tib == [(0, {"start": "2026-10-24T00:00:00+02:00", "end": "2026-10-25T00:00:00+02:00"}),
                   (1, {"start": "2026-10-25T00:00:00+02:00", "end": "2026-10-26T00:00:00+01:00"})]

    for action in ("energyzero.get_energy_prices", "easyenergy.get_energy_usage_prices"):
        z = build_calls(parse_source({"action": action, "data": {"config_entry": "z", "incl_vat": False}}),
                        NOW_WAW_10, WAW)
        assert [d for _o, d in z] == [{"config_entry": "z", "incl_vat": False, "start": "2026-10-31",
                                        "end": "2026-10-31"},
                                       {"config_entry": "z", "incl_vat": False, "start": "2026-11-01",
                                        "end": "2026-11-01"}]


def test_market_day_from_market_timezone_not_utc():
    late = datetime(2026, 10, 31, 23, 30, tzinfo=timezone.utc)        # już 1.11 w Warszawie
    src = parse_source({"action": "entsoe.get_energy_prices", "data": {"config_entry": "e1"}, "currency": "EUR",
                        "unit": "kWh"})
    assert build_calls(src, late, WAW)[0][1]["start"] == "2026-11-01T00:00:00+01:00"


def test_amber_is_one_rolling_call_with_user_data_only():
    src = parse_source({"action": "amberelectric.get_forecasts",
                        "data": {"config_entry_id": "a1", "channel_type": "feed_in"}})
    assert build_calls(src, NOW_WAW_10, SYD) == [(None, {"config_entry_id": "a1", "channel_type": "feed_in"})]


# ── odpowiedź → atrybuty → przedziały kontraktu ────────────────────────────


TODAY = date(2026, 10, 31)


def test_nordpool_mwh_to_kwh_today_and_tomorrow():
    src = parse_source({"action": "nordpool.get_prices_for_date",
                        "data": {"config_entry": "n1", "areas": "SE3", "currency": "SEK"}})
    attrs, last = attributes_from_responses(src, nordpool_response(TODAY), nordpool_response(TODAY + timedelta(1)),
                                            today=TODAY, tz=OSLO)
    out = _intervals(attrs, tz=OSLO)
    assert last == TODAY + timedelta(1)
    assert out[0]["currency"] == "SEK" and out[0]["minutes"] == 15
    assert out[0]["buy"] == pytest.approx(0.4)                        # 400 SEK/MWh → 0,4 SEK/kWh
    # 31.10 ma 96 kwadransów, 1.11 też (zmiana czasu w Norwegii była 25.10).
    assert len(out) == 96 * 2


def test_tomorrow_missing_or_failed_keeps_today_whole():
    src = parse_source({"action": "nordpool.get_prices_for_date",
                        "data": {"config_entry": "n1", "areas": "SE3", "currency": "SEK"}})
    for tomorrow in ({"SE3": []}, RuntimeError("no data"), None):
        attrs, last = attributes_from_responses(src, nordpool_response(TODAY), tomorrow, today=TODAY, tz=OSLO)
        assert last == TODAY and len(_intervals(attrs, tz=OSLO)) == 96


def test_tibber_single_home_and_selected_home():
    src = parse_source({"action": "tibber.get_prices", "currency": "NOK"})
    attrs, _ = attributes_from_responses(src, tibber_response(TODAY), None, today=TODAY, tz=OSLO)
    out = _intervals(attrs, tz=OSLO)
    assert len(out) == 96 and out[0]["currency"] == "NOK" and out[0]["buy"] == 1.0
    two = tibber_response(TODAY, homes=("Cabin", "Home"))
    two["prices"]["Cabin"] = [{"start_time": r["start_time"], "price": 9.0} for r in two["prices"]["Cabin"]]
    picked, _ = attributes_from_responses(parse_source({"action": "tibber.get_prices", "home": "Home"}),
                                          two, None, today=TODAY, tz=OSLO)
    assert _intervals(picked, fallback="NOK", tz=OSLO)[0]["buy"] == 1.0
    # Dwa domy bez wskazania — zgadywanie cudzego rachunku; to błąd odpowiedzi.
    with pytest.raises(pa.InvalidPriceResponse):
        attributes_from_responses(parse_source({"action": "tibber.get_prices"}), two, None, today=TODAY, tz=OSLO)


def test_tibber_currency_falls_back_to_option_and_none_without_it():
    attrs, _ = attributes_from_responses(parse_source({"action": "tibber.get_prices"}), tibber_response(TODAY),
                                         None, today=TODAY, tz=OSLO)
    assert currency_from_attributes(attrs, None) is None             # bez waluty nie zgadujemy
    assert _intervals(attrs, fallback="SEK", tz=OSLO)[0]["currency"] == "SEK"


def test_energyzero_easyenergy_entsoe_shapes():
    z = parse_source({"action": "energyzero.get_energy_prices", "data": {"config_entry": "z", "incl_vat": True}})
    attrs, _ = attributes_from_responses(z, energyzero_response(TODAY), energyzero_response(TODAY + timedelta(1)),
                                         today=TODAY, tz=AMS)
    out = _intervals(attrs, tz=AMS)
    assert out[0]["currency"] == "EUR" and out[0]["minutes"] == 60 and len(out) == 48
    assert out[0]["startAt"] == "2026-10-30T23:00:00Z"               # północ CET (po zmianie czasu)

    y = parse_source({"action": "easyenergy.get_energy_usage_prices", "data": {"config_entry": "y", "incl_vat": True}})
    r = parse_source({"action": "easyenergy.get_energy_return_prices", "data": {"config_entry": "y"}})
    buy, _ = attributes_from_responses(y, easyenergy_response(TODAY), None, today=TODAY, tz=AMS)
    sell, _ = attributes_from_responses(r, easyenergy_response(TODAY, base=0.05), None, today=TODAY, tz=AMS)
    paired = _intervals(buy, sell, tz=AMS)
    assert paired[0]["buy"] == 0.2 and paired[0]["sell"] == 0.05 and len(paired) == 24

    e = parse_source({"action": "entsoe.get_energy_prices", "data": {"config_entry": "e"}, "currency": "EUR",
                      "unit": "kWh"})
    ent, _ = attributes_from_responses(e, entsoe_response(TODAY), None, today=TODAY, tz=AMS)
    assert _intervals(ent, tz=AMS)[0]["buy"] == 0.05


def test_entsoe_mwh_scaled_to_kwh_kwh_unchanged():
    mwh = parse_source({"action": "entsoe.get_energy_prices", "data": {"config_entry": "e"}, "currency": "eur",
                        "unit": "mwh"})
    resp = {"prices": [{**r, "price": r["price"] * 1000} for r in entsoe_response(TODAY)["prices"]]}
    attrs, _ = attributes_from_responses(mwh, resp, None, today=TODAY, tz=AMS)
    out = _intervals(attrs, tz=AMS)
    assert out[0]["buy"] == pytest.approx(0.05) and out[0]["currency"] == "EUR" and len(out) == 24
    kwh = parse_source({"action": "entsoe.get_energy_prices", "data": {"config_entry": "e"}, "currency": "EUR",
                        "unit": "kWh"})
    attrs, _ = attributes_from_responses(kwh, entsoe_response(TODAY), None, today=TODAY, tz=AMS)
    assert _intervals(attrs, tz=AMS)[0]["buy"] == 0.05


def test_amber_general_and_feed_in_pair_as_buy_and_sell():
    start = datetime(2026, 10, 31, 14, 0, tzinfo=SYD)
    gen = parse_source({"action": "amberelectric.get_forecasts",
                        "data": {"config_entry_id": "a1", "channel_type": "general"}})
    feed = parse_source({"action": "amberelectric.get_forecasts",
                         "data": {"config_entry_id": "a1", "channel_type": "feed_in"}})
    buy, last = attributes_from_responses(gen, amber_response(start), None, today=TODAY, tz=SYD)
    sell, _ = attributes_from_responses(feed, amber_response(start, sign=0.5), None, today=TODAY, tz=SYD)
    out = _intervals(buy, sell, tz=SYD)
    assert last == date(2026, 11, 1)                                  # prognoza sięga jutra
    assert out[0]["currency"] == "AUD" and out[0]["minutes"] == 30 and len(out) == 48
    assert out[0]["startAt"] == "2026-10-31T03:00:00Z"               # sekunda jittera ścięta
    assert out[0]["buy"] == 0.2 and out[0]["sell"] == 0.1


def test_amber_five_minute_forecasts():
    start = datetime(2026, 10, 31, 14, 0, tzinfo=SYD)
    gen = parse_source({"action": "amberelectric.get_forecasts",
                        "data": {"config_entry_id": "a1", "channel_type": "general"}})
    attrs, _ = attributes_from_responses(gen, amber_response(start, count=288, minutes=5), None, today=TODAY, tz=SYD)
    out = _intervals(attrs, tz=SYD)
    assert len(out) == 288 and {iv["minutes"] for iv in out} == {5}


def test_inconsistent_series_gives_empty_never_partial():
    src = parse_source({"action": "nordpool.get_prices_for_date",
                        "data": {"config_entry": "n1", "areas": "SE3", "currency": "SEK"}})
    holed = nordpool_response(TODAY)
    del holed["SE3"][40]                                              # dziura w środku doby
    attrs, _ = attributes_from_responses(src, holed, None, today=TODAY, tz=OSLO)
    assert _intervals(attrs, tz=OSLO) == []
    bad = nordpool_response(TODAY)
    bad["SE3"][3]["price"] = "n/a"
    attrs, _ = attributes_from_responses(src, bad, nordpool_response(TODAY + timedelta(1)), today=TODAY, tz=OSLO)
    assert _intervals(attrs, tz=OSLO) == []
    # Jutro niespójne kasuje też dziś — seria jest jedna (tak jak raw_today/raw_tomorrow).
    bad_tomorrow = nordpool_response(TODAY + timedelta(1))
    bad_tomorrow["SE3"][5]["end"] = bad_tomorrow["SE3"][5]["start"]
    attrs, _ = attributes_from_responses(src, nordpool_response(TODAY), bad_tomorrow, today=TODAY, tz=OSLO)
    assert _intervals(attrs, tz=OSLO) == []


@pytest.mark.parametrize("response", [
    None, [], "x", {"prices": "x"}, {"prices": {"Home": "x"}}, {"other": []}, {"prices": []},
])
def test_malformed_or_empty_today_is_an_error(response):
    src = parse_source({"action": "tibber.get_prices"})
    with pytest.raises(pa.InvalidPriceResponse):
        attributes_from_responses(src, response, None, today=TODAY, tz=OSLO)


def test_oversized_response_rejected():
    src = parse_source({"action": "energyzero.get_energy_prices", "data": {"config_entry": "z", "incl_vat": True}})
    huge = {"prices": [{"timestamp": "2026-10-31 00:00:00+00:00", "price": 0.1}] * (pa.MAX_RESPONSE_ENTRIES + 1)}
    with pytest.raises(pa.InvalidPriceResponse):
        attributes_from_responses(src, huge, None, today=TODAY, tz=AMS)


# ── poller: harmonogram, błędy, brak duplikatów ─────────────────────────────


class FakeHA:
    """Atrapa wywołania akcji: odpowiedź albo wyjątek na dzień (klucz = data w danych)."""

    def __init__(self, responder):
        self.calls: list[tuple[str, str, dict]] = []
        self.responder = responder

    async def __call__(self, domain, service, data):
        self.calls.append((domain, service, dict(data)))
        out = self.responder(domain, service, data)
        if isinstance(out, BaseException):
            raise out
        if asyncio.iscoroutine(out):
            return await out
        return out


NP = {"action": "nordpool.get_prices_for_date", "data": {"config_entry": "n1", "areas": "SE3", "currency": "SEK"}}


def _np_responder(tomorrow_published=True):
    def respond(_d, _s, data):
        day = date.fromisoformat(data["date"])
        if day > TODAY and not tomorrow_published:
            return {"SE3": []}
        return nordpool_response(day)
    return respond


def _run(coro):
    return asyncio.run(coro)


def test_poller_caches_attributes_and_reports_last_day():
    ha = FakeHA(_np_responder())
    p = PriceActionPoller(parse_source(NP), ha)
    assert p.attributes is None and p.due(NOW_WAW_10, OSLO)
    assert _run(p.async_refresh(NOW_WAW_10, OSLO)) is True
    assert [c[2]["date"] for c in ha.calls] == ["2026-10-31", "2026-11-01"]
    assert ha.calls[0][:2] == ("nordpool", "get_prices_for_date")
    assert len(_intervals(p.attributes, tz=OSLO)) == 192


def test_schedule_every_six_hours_when_tomorrow_known():
    ha = FakeHA(_np_responder())
    p = PriceActionPoller(parse_source(NP), ha)
    _run(p.async_refresh(NOW_WAW_10, OSLO))
    assert not p.due(NOW_WAW_10 + timedelta(hours=5, minutes=59), OSLO)
    assert p.due(NOW_WAW_10 + timedelta(hours=6), OSLO)


def test_schedule_hourly_after_13_until_tomorrow_appears():
    ha = FakeHA(_np_responder(tomorrow_published=False))
    p = PriceActionPoller(parse_source(NP), ha)
    t0 = datetime(2026, 10, 31, 7, 0, tzinfo=timezone.utc)             # 08:00 w Oslo
    _run(p.async_refresh(t0, OSLO))
    assert not p.due(t0 + timedelta(hours=1), OSLO)                    # 09:00 — przed 13:00 tylko co 6 h
    t13 = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)           # 13:00 w Oslo, 5 h po pobraniu
    assert p.due(t13, OSLO)
    _run(p.async_refresh(t13, OSLO))
    assert not p.due(t13 + timedelta(minutes=59), OSLO)
    assert p.due(t13 + timedelta(hours=1), OSLO)                       # dalej brak jutra → co godzinę
    ha.responder = _np_responder(tomorrow_published=True)
    _run(p.async_refresh(t13 + timedelta(hours=1), OSLO))
    assert not p.due(t13 + timedelta(hours=2), OSLO)                   # jutro jest → znowu co 6 h
    assert p.due(t13 + timedelta(hours=7), OSLO)


def test_today_failure_clears_prices_logs_type_and_backs_off(caplog):
    caplog.set_level(logging.DEBUG)
    ha = FakeHA(_np_responder())
    p = PriceActionPoller(parse_source(NP), ha)
    _run(p.async_refresh(NOW_WAW_10, OSLO))
    assert p.attributes is not None

    class ServiceValidationError(Exception):
        pass

    ha.responder = lambda *_a: ServiceValidationError("config entry n1 secret detail")
    t1 = NOW_WAW_10 + timedelta(hours=6)
    assert _run(p.async_refresh(t1, OSLO)) is False
    assert p.attributes is None                                        # błąd = brak sekcji, nie stare dane
    assert "ServiceValidationError" in caplog.text and "nordpool.get_prices_for_date" in caplog.text
    assert "secret" not in caplog.text and "n1" not in caplog.text
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    # Odstępy po kolejnych błędach: 5, 10, 20, 40, 60, 60 min.
    t = t1
    for gap in (5, 10, 20, 40, 60, 60):
        assert not p.due(t + timedelta(minutes=gap) - timedelta(seconds=1), OSLO)
        t += timedelta(minutes=gap)
        assert p.due(t, OSLO)
        _run(p.async_refresh(t, OSLO))
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == len(warnings)  # bez zalewu logu
    ha.responder = _np_responder()
    assert _run(p.async_refresh(t + timedelta(hours=1), OSLO)) is True and p.attributes is not None


def test_timeout_is_an_error_and_call_cancelled(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    cancelled = []

    async def hang():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    ha = FakeHA(lambda *_a: hang())
    p = PriceActionPoller(parse_source(NP), ha, timeout_s=0.01)
    assert _run(p.async_refresh(NOW_WAW_10, OSLO)) is False
    assert cancelled == [True] and p.attributes is None and "TimeoutError" in caplog.text
    assert len(ha.calls) == 1                                          # dzisiaj padło — jutra nie pytamy


def test_no_parallel_duplicate_refresh():
    gate = {}

    async def slow(day):
        await gate["event"].wait()
        return nordpool_response(day)

    ha = FakeHA(lambda _d, _s, data: slow(date.fromisoformat(data["date"])))
    p = PriceActionPoller(parse_source(NP), ha)

    async def scenario():
        gate["event"] = asyncio.Event()
        first = asyncio.ensure_future(p.async_refresh(NOW_WAW_10, OSLO))
        await asyncio.sleep(0)
        assert p.busy and not p.due(NOW_WAW_10, OSLO)
        assert await p.async_refresh(NOW_WAW_10, OSLO) is False      # drugi start w trakcie: nic
        gate["event"].set()
        assert await first is True

    _run(scenario())
    assert len(ha.calls) == 2 and not p.busy


def test_cancellation_propagates_and_releases_busy():
    async def scenario():
        ha = FakeHA(lambda *_a: asyncio.sleep(3600))
        p = PriceActionPoller(parse_source(NP), ha)
        task = asyncio.ensure_future(p.async_refresh(NOW_WAW_10, OSLO))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not p.busy

    _run(scenario())


def test_missing_today_coverage_refreshes_hourly_even_before_13():
    # Wczoraj jutro się nie pojawiło: dziś rano dane nie obejmują już bieżącej doby.
    ha = FakeHA(_np_responder(tomorrow_published=False))
    p = PriceActionPoller(parse_source(NP), ha)
    evening = datetime(2026, 10, 31, 21, 0, tzinfo=timezone.utc)      # 22:00 w Oslo
    _run(p.async_refresh(evening, OSLO))
    morning = datetime(2026, 11, 1, 2, 0, tzinfo=timezone.utc)        # 03:00 następnego dnia, 5 h później
    assert p.due(morning, OSLO)


def test_fresh_attributes_only_from_a_recent_success_covering_today():
    ha = FakeHA(_np_responder(tomorrow_published=False))
    p = PriceActionPoller(parse_source(NP), ha)
    assert p.fresh_attributes(NOW_WAW_10, OSLO) is None                # przed pierwszym wynikiem
    _run(p.async_refresh(NOW_WAW_10, OSLO))
    assert p.fresh_attributes(NOW_WAW_10 + timedelta(hours=6), OSLO) is p.attributes
    # Wynik starszy niż odświeżenie + zapas (odświeżenie nie doszło do skutku) — już nie świeży.
    assert p.fresh_attributes(NOW_WAW_10 + timedelta(seconds=pa.REFRESH_S + pa.FRESH_GRACE_S), OSLO) is None
    # Dane bez dzisiejszej doby (jutro się nie pojawiło, minęła północ) — nie świeże, choć młode.
    late = PriceActionPoller(parse_source(NP), FakeHA(_np_responder(tomorrow_published=False)))
    _run(late.async_refresh(datetime(2026, 10, 31, 21, 0, tzinfo=timezone.utc), OSLO))      # 22:00 w Oslo
    assert late.fresh_attributes(datetime(2026, 10, 31, 22, 30, tzinfo=timezone.utc), OSLO) is not None
    assert late.fresh_attributes(datetime(2026, 10, 31, 23, 30, tzinfo=timezone.utc), OSLO) is None
    ha.responder = lambda *_a: RuntimeError("down")
    _run(p.async_refresh(NOW_WAW_10 + timedelta(hours=1), OSLO))
    assert p.fresh_attributes(NOW_WAW_10 + timedelta(hours=1), OSLO) is None


def test_amber_rolling_forecast_refreshes_every_30_minutes_other_actions_every_6_hours():
    amber = parse_source({"action": "amberelectric.get_forecasts",
                          "data": {"config_entry_id": "a1", "channel_type": "general"}})
    ha = FakeHA(lambda *_: amber_response(NOW_WAW_10.astimezone(SYD).replace(minute=0, second=0, microsecond=0)))
    p = PriceActionPoller(amber, ha)
    _run(p.async_refresh(NOW_WAW_10, SYD))
    assert not p.due(NOW_WAW_10 + timedelta(minutes=29), SYD)
    assert p.due(NOW_WAW_10 + timedelta(minutes=31), SYD)
    # świeżość wyniku liczona od tego samego okresu (30 min + zapas), nie od 6 h
    assert p.fresh_attributes(NOW_WAW_10 + timedelta(minutes=20), SYD) is p.attributes
    assert p.fresh_attributes(NOW_WAW_10 + timedelta(minutes=45), SYD) is None
    other = PriceActionPoller(parse_source(NP), FakeHA(_np_responder()))
    _run(other.async_refresh(NOW_WAW_10, OSLO))
    assert not other.due(NOW_WAW_10 + timedelta(minutes=31), OSLO)
