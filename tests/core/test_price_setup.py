"""Wykrywanie źródeł cen i walidacja prognozy przy wyborze (warstwa czysta, bez Home Assistanta)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.volcast.core.price_actions import InvalidPriceSource, parse_source
from custom_components.volcast.core.price_setup import (
    action_candidates, build_action_option, check_series, currency_conflict, effective_currency,
    enrich_attributes, is_excluded_entity, pair_entities)

WAW = ZoneInfo("Europe/Warsaw")
NOW = datetime(2026, 10, 31, 9, 0, tzinfo=timezone.utc)


def _hourly(hours, start=NOW.replace(minute=0)):
    return {"currency": "PLN", "unit_of_measurement": "PLN/kWh",
            "raw_today": [{"start": (start + timedelta(hours=i)).isoformat(),
                           "end": (start + timedelta(hours=i + 1)).isoformat(), "value": 0.5} for i in range(hours)]}


def _c(eid, platform, entry=None):
    return {"entity_id": eid, "platform": platform, "config_entry_id": entry}


def test_pairing_octopus_events_import_export():
    out = pair_entities([
        _c("event.octopus_energy_electricity_aaa_111_current_day_rates", "octopus_energy"),
        _c("event.octopus_energy_electricity_bbb_222_export_current_day_rates", "octopus_energy"),
        _c("sensor.other", "something_else")])
    assert [(p.buy, p.sell, p.ambiguous) for p in out] == [
        ("event.octopus_energy_electricity_aaa_111_current_day_rates",
         "event.octopus_energy_electricity_bbb_222_export_current_day_rates", False)]


def test_pairing_pstryk_and_amber_by_stem_and_controlled_load_skipped():
    out = pair_entities([_c("sensor.pstryk_current_buy_price", "pstryk"), _c("sensor.pstryk_current_sell_price", "pstryk")])
    assert (out[0].buy, out[0].sell) == ("sensor.pstryk_current_buy_price", "sensor.pstryk_current_sell_price")
    amber = pair_entities([_c("sensor.home_general_forecast", "amberelectric", "a"),
                           _c("sensor.home_feed_in_forecast", "amberelectric", "a"),
                           _c("sensor.home_controlled_load_forecast", "amberelectric", "a")])
    assert [(p.buy, p.sell) for p in amber] == [("sensor.home_general_forecast", "sensor.home_feed_in_forecast")]


def test_pairing_groups_by_config_entry_and_ambiguity_is_offered_not_hidden():
    two = pair_entities([_c("sensor.a_price", "tibber", "e1"), _c("sensor.b_price", "tibber", "e2")])
    assert [(p.buy, p.sell) for p in two] == [("sensor.a_price", None), ("sensor.b_price", None)]
    # jedno kupno i dwie sprzedaże bez wspólnego pnia: wszystkie kombinacje, oznaczone jako niejednoznaczne
    amb = pair_entities([_c("sensor.x_buy", "pstryk"), _c("sensor.y_sell", "pstryk"), _c("sensor.z_export", "pstryk")])
    assert sorted((p.buy, p.sell, p.ambiguous) for p in amb) == [
        ("sensor.x_buy", "sensor.y_sell", True), ("sensor.x_buy", "sensor.z_export", True)]
    # sprzedaż bez kupna nie jest podpowiadana
    assert pair_entities([_c("sensor.only_sell", "pstryk")]) == []


def test_action_candidates_amber_pair_and_per_entry():
    c = action_candidates({"amberelectric": [("e1", "Home")], "tibber": [("t1", "Tibber")]})
    amber = next(x for x in c if x.domain == "amberelectric")
    assert amber.buy["action"] == "amberelectric.get_forecasts" and amber.sell["action"] == "amberelectric.get_forecasts"
    assert amber.buy["data"] == {"config_entry_id": "e1", "channel_type": "general"}
    assert amber.sell["data"] == {"config_entry_id": "e1", "channel_type": "feed_in"}
    assert amber.fields == ()
    assert next(x for x in c if x.domain == "tibber").sell is None


@pytest.mark.parametrize("domain,fields", [
    ("entsoe", ("unit", "currency")), ("nordpool", ("area", "currency")),
    ("energyzero", ("incl_vat",)), ("easyenergy", ("incl_vat",)), ("tibber", ("home", "currency"))])
def test_needed_fields(domain, fields):
    c = action_candidates({domain: [("e1", "X")]})[0]
    assert c.fields == fields


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


def test_series_starting_in_the_future_is_not_a_forecast():
    future = _hourly(30, start=NOW.replace(minute=0) + timedelta(hours=2))
    assert check_series(future, None, WAW, NOW).status == "unusable"


def test_nordpool_option_is_saved_normalized():
    npool = action_candidates({"nordpool": [("n1", "N")]})[0]
    buy, _ = build_action_option(npool, {"area": "se3", "currency": "sek"})
    assert buy["data"]["areas"] == "SE3" and buy["data"]["currency"] == "SEK"


def test_amber_dollar_unit_without_currency_is_aud_only_for_amber():
    attrs = {"unit_of_measurement": "$/kWh"}
    assert effective_currency("amberelectric", attrs) == "AUD"
    assert effective_currency("some_other_platform", attrs) is None      # reguła dotyczy tylko Ambera
    assert effective_currency("octopus_energy", attrs) is None
    assert enrich_attributes("amberelectric", {**attrs, "currency": "NZD"})["currency"] == "NZD"   # atrybut wygrywa
    assert effective_currency("amberelectric", {**attrs, "currency": "NZD"}) == "NZD"
    assert "currency" not in enrich_attributes("pstryk", attrs)
    assert "currency" not in enrich_attributes("amberelectric", {"unit_of_measurement": "kr/kWh"})
    assert enrich_attributes("amberelectric", None) == {}


def _amber(hours, minutes=30, now=NOW):
    """Prognoza w kształcie Ambera: BEZ bieżącego przedziału, od następnej granicy siatki,
    start znakowany sekundą po pełnej minucie."""
    krok = minutes * 60
    start = datetime.fromtimestamp((int(now.timestamp()) // krok + 1) * krok, timezone.utc)
    return {"unit_of_measurement": "$/kWh", "forecasts": [
        {"start_time": (start + timedelta(minutes=minutes * i, seconds=1)).isoformat(),
         "end_time": (start + timedelta(minutes=minutes * (i + 1))).isoformat(), "per_kwh": 0.3}
        for i in range(hours * 60 // minutes)]}


def test_amber_series_with_dollar_unit_is_usable_with_enrichment():
    attrs = _amber(24)
    assert check_series(attrs, None, WAW, NOW).status == "unusable"                      # bez reguły: waluta niejasna
    ok = check_series(enrich_attributes("amberelectric", attrs), None, WAW, NOW)
    assert (ok.status, ok.hours) == ("ok", 24)


@pytest.mark.parametrize("minutes, after", [(5, 0), (5, 3), (30, 0), (30, 17)])
def test_amber_forecast_without_current_interval_is_usable(minutes, after):
    now = NOW + timedelta(minutes=after)
    assert check_series(enrich_attributes("amberelectric", _amber(24, minutes, now)), None, WAW, now).status == "ok"


def test_short_amber_forecast_is_short_not_unusable():
    short = check_series(enrich_attributes("amberelectric", _amber(3)), None, WAW, NOW)
    assert (short.status, short.hours) == ("short", 3)


def test_event_entity_before_its_first_event_is_unusable_in_setup_too():
    # Encja `event` HA ma stan „unknown” tylko przed pierwszym zdarzeniem (stan = chwila ostatniego zdarzenia,
    # przywracana razem z jego atrybutami) — wtedy atrybuty to sam `event_type` bez stawek. Formularz odrzuca ją
    # tak samo jak telemetria, która stanu „unknown” nie czyta.
    attrs = {"event_type": None, "event_types": ["octopus_energy_electricity_current_day_rates"]}
    check = check_series(enrich_attributes("octopus_energy", attrs), None, WAW, NOW, state="unknown")
    assert check.status == "unusable"


def test_octopus_gas_is_not_an_electricity_candidate():
    gas = "event.octopus_energy_gas_aaa_111_current_day_rates"
    assert is_excluded_entity("octopus_energy", gas) and not is_excluded_entity("pstryk", gas)
    assert not is_excluded_entity("octopus_energy", "event.octopus_energy_electricity_aaa_111_current_day_rates")
    out = pair_entities([
        _c(gas, "octopus_energy"),
        _c("event.octopus_energy_electricity_aaa_111_current_day_rates", "octopus_energy"),
        _c("event.octopus_energy_electricity_bbb_222_export_current_day_rates", "octopus_energy")])
    assert [(p.buy, p.sell, p.ambiguous) for p in out] == [
        ("event.octopus_energy_electricity_aaa_111_current_day_rates",
         "event.octopus_energy_electricity_bbb_222_export_current_day_rates", False)]


def test_role_vocabulary_is_the_generic_words_plus_registry_hints():
    """Słowa ról z rejestru dochodzą do ogólnego słownika — pary tworzą się tak samo jak przed rejestrem."""
    from custom_components.volcast.core import price_setup as ps
    assert set(ps._SELL_WORDS) == {"export", "feed_in", "feedin", "sell", "sprzed", "return", "teruglever", "inject"}
    assert ps._SIDE_TOKENS == {"export", "feed", "in", "feedin", "sell", "sprzedaz", "return", "buy", "import",
                               "general", "kupno", "teruglevering"}
