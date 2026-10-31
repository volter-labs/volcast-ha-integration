"""Wykrywanie źródeł cen i walidacja prognozy przy wyborze (warstwa czysta, bez Home Assistanta)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.volcast.core.price_actions import InvalidPriceSource, parse_source
from custom_components.volcast.core.price_setup import (
    action_candidates, build_action_option, check_series, currency_conflict, needed_fields, pair_entities)

WAW = ZoneInfo("Europe/Warsaw")
NOW = datetime(2026, 10, 31, 9, 0, tzinfo=timezone.utc)


def _hourly(hours, start=NOW.replace(minute=0)):
    return {"currency": "PLN", "unit_of_measurement": "PLN/kWh",
            "raw_today": [{"start": (start + timedelta(hours=i)).isoformat(),
                           "end": (start + timedelta(hours=i + 1)).isoformat(), "value": 0.5} for i in range(hours)]}


def test_pairing_octopus_import_export():
    out = pair_entities([
        {"entity_id": "sensor.octopus_energy_electricity_x_current_rate", "platform": "octopus_energy"},
        {"entity_id": "sensor.octopus_energy_electricity_x_export_current_rate", "platform": "octopus_energy"},
        {"entity_id": "sensor.other", "platform": "something_else"}])
    assert [(p.buy, p.sell) for p in out] == [("sensor.octopus_energy_electricity_x_current_rate",
                                               "sensor.octopus_energy_electricity_x_export_current_rate")]


def test_pairing_pstryk_and_ambiguous_platform_not_paired():
    out = pair_entities([
        {"entity_id": "sensor.pstryk_current_buy_price", "platform": "pstryk"},
        {"entity_id": "sensor.pstryk_current_sell_price", "platform": "pstryk"}])
    assert (out[0].buy, out[0].sell) == ("sensor.pstryk_current_buy_price", "sensor.pstryk_current_sell_price")
    two = pair_entities([
        {"entity_id": "sensor.a_price", "platform": "tibber"}, {"entity_id": "sensor.b_price", "platform": "tibber"}])
    assert [(p.buy, p.sell) for p in two] == [("sensor.a_price", None), ("sensor.b_price", None)]


def test_action_candidates_amber_pair_and_per_entry():
    c = action_candidates({"amberelectric": [("e1", "Home")], "tibber": [("t1", "Tibber")]})
    amber = next(x for x in c if x.domain == "amberelectric")
    assert amber.buy["action"] == "amberelectric.get_forecasts" and amber.sell["action"] == "amberelectric.get_forecasts"
    assert amber.buy["data"] == {"config_entry_id": "e1", "channel_type": "general"}
    assert amber.sell["data"] == {"config_entry_id": "e1", "channel_type": "feed_in"}
    assert needed_fields(amber) == ()
    assert next(x for x in c if x.domain == "tibber").sell is None


@pytest.mark.parametrize("domain,fields", [
    ("entsoe", ("unit", "currency")), ("nordpool", ("area", "currency")),
    ("energyzero", ("incl_vat",)), ("easyenergy", ("incl_vat",)), ("tibber", ("home", "currency"))])
def test_needed_fields(domain, fields):
    c = action_candidates({domain: [("e1", "X")]})[0]
    assert needed_fields(c) == fields


def test_build_options_pass_parse_source():
    ents = action_candidates({"entsoe": [("e1", "E")]})[0]
    buy, sell = build_action_option(ents, {"unit": "MWh", "currency": "eur"})
    assert sell is None and parse_source(buy).unit == "MWh" and parse_source(buy).currency == "EUR"
    npool = action_candidates({"nordpool": [("n1", "N")]})[0]
    buy, _ = build_action_option(npool, {"area": "se3", "currency": "SEK"})
    assert parse_source(buy).area == "SE3"
    ez = action_candidates({"easyenergy": [("z1", "Z")]})[0]
    buy, sell = build_action_option(ez, {"incl_vat": True})
    assert buy["data"]["incl_vat"] is True and "incl_vat" not in sell["data"] and parse_source(sell).currency == "EUR"


def test_build_options_rejects_missing_or_bad():
    ents = action_candidates({"entsoe": [("e1", "E")]})[0]
    with pytest.raises(InvalidPriceSource):
        build_action_option(ents, {"currency": "EUR"})                    # brak jednostki
    with pytest.raises(InvalidPriceSource):
        build_action_option(ents, {"unit": "Wh", "currency": "EUR"})
    npool = action_candidates({"nordpool": [("n1", "N")]})[0]
    with pytest.raises(InvalidPriceSource):
        build_action_option(npool, {"area": "SE3,SE4", "currency": "SEK"})  # dokładnie jeden obszar


def test_currency_conflict():
    assert currency_conflict("EUR", "PLN") is True
    assert currency_conflict("EUR", "eur") is False
    assert currency_conflict("EUR", None) is False and currency_conflict(None, "PLN") is False


def test_check_series_forecast_current_only_short():
    ok = check_series(_hourly(24), None, WAW, NOW)
    assert (ok.status, ok.hours) == ("ok", 24)
    short = check_series(_hourly(3), None, WAW, NOW)
    assert short.status == "short" and short.hours == 3
    cur = check_series({"unit_of_measurement": "PLN/kWh"}, None, WAW, NOW, state="0.61")
    assert cur.status == "current_only"
    assert check_series({"unit_of_measurement": "PLN/kWh"}, None, WAW, NOW, state="unavailable").status == "unusable"
    assert check_series({"x": 1}, None, WAW, NOW, state="0.5").status == "unusable"   # nie wygląda na cenę
    bad = check_series({"currency": "FOO", "raw_today": []}, None, WAW, NOW)
    assert bad.status == "unusable"
