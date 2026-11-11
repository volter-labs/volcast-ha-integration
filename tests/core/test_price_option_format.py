"""Zapisany format opcji źródła cen — bajt w bajt (istniejące wpisy działają bez migracji).

Opcje `price_buy_action` / `price_sell_action` zapisuje formularz (`build_action_option`), a czyta
runtime (`parse_source`). Kolejność kluczy też jest częścią formatu: HA zapisuje opcje jako JSON.
"""
from __future__ import annotations

import json

import pytest

from custom_components.volcast.core.price_actions import parse_source
from custom_components.volcast.core.price_setup import action_candidates, build_action_option


def _dump(option) -> str | None:
    return None if option is None else json.dumps(option, ensure_ascii=False)


@pytest.mark.parametrize("domain,values,buy,sell", [
    ("amberelectric", {},
     '{"action": "amberelectric.get_forecasts", "data": {"config_entry_id": "e1", "channel_type": "general"}}',
     '{"action": "amberelectric.get_forecasts", "data": {"config_entry_id": "e1", "channel_type": "feed_in"}}'),
    ("tibber", {},
     '{"action": "tibber.get_prices", "data": {}}', None),
    ("tibber", {"home": "Dom", "currency": " sek "},
     '{"action": "tibber.get_prices", "data": {}, "currency": "SEK", "home": "Dom"}', None),
    ("tibber", {"home": "", "currency": ""},
     '{"action": "tibber.get_prices", "data": {}}', None),
    ("nordpool", {"area": " se3 ", "currency": "sek"},
     '{"action": "nordpool.get_prices_for_date", "data": {"config_entry": "e1", "areas": "SE3", "currency": "SEK"}}',
     None),
    ("entsoe", {"unit": " MWh ", "currency": "eur"},
     '{"action": "entsoe.get_energy_prices", "data": {"config_entry": "e1"}, "unit": "MWh", "currency": "EUR"}',
     None),
    ("energyzero", {"incl_vat": False},
     '{"action": "energyzero.get_energy_prices", "data": {"config_entry": "e1", "incl_vat": false}}', None),
    ("easyenergy", {"incl_vat": 1},
     '{"action": "easyenergy.get_energy_usage_prices", "data": {"config_entry": "e1", "incl_vat": true}}',
     '{"action": "easyenergy.get_energy_return_prices", "data": {"config_entry": "e1"}}'),
])
def test_saved_action_options_are_byte_identical(domain, values, buy, sell):
    cand = action_candidates({domain: [("e1", "X")]})[0]
    got_buy, got_sell = build_action_option(cand, values)
    assert (_dump(got_buy), _dump(got_sell)) == (buy, sell)


@pytest.mark.parametrize("stored", [
    # Opcje zapisane wcześniejszymi wersjami (także Nord Pool ze strefą jako listą) czyta runtime bez migracji.
    {"action": "amberelectric.get_forecasts", "data": {"config_entry_id": "e1", "channel_type": "general"}},
    {"action": "tibber.get_prices", "data": {}, "currency": "SEK", "home": "Dom"},
    {"action": "tibber.get_prices"},
    {"action": "nordpool.get_prices_for_date", "data": {"config_entry": "e1", "areas": ["se3"], "currency": "sek"}},
    {"action": "nordpool.get_prices_for_date", "data": {"config_entry": "e1", "areas": "SE3", "currency": "SEK"}},
    {"action": "entsoe.get_energy_prices", "data": {"config_entry": "e1"}, "unit": "kWh", "currency": "EUR"},
    {"action": "energyzero.get_energy_prices", "data": {"config_entry": "e1", "incl_vat": True}},
    {"action": "easyenergy.get_energy_usage_prices", "data": {"config_entry": "e1", "incl_vat": False}},
    {"action": "easyenergy.get_energy_return_prices", "data": {"config_entry": "e1"}},
])
def test_stored_options_still_parse(stored):
    src = parse_source(stored)
    assert src.action == stored["action"]


def test_parsed_stored_sources_keep_their_runtime_shape():
    amber = parse_source({"action": "amberelectric.get_forecasts",
                          "data": {"config_entry_id": "e1", "channel_type": "general"}})
    assert (amber.currency, amber.unit, amber.area, amber.refresh_s) == ("AUD", None, None, 1800)
    npool = parse_source({"action": "nordpool.get_prices_for_date",
                          "data": {"config_entry": "e1", "areas": ["se3"], "currency": "sek"}})
    assert (npool.currency, npool.unit, npool.area, dict(npool.data), npool.refresh_s) == (
        "SEK", "MWh", "SE3", {"config_entry": "e1", "areas": "SE3", "currency": "SEK"}, 6 * 3600)
    ents = parse_source({"action": "entsoe.get_energy_prices", "data": {"config_entry": "e1"},
                         "unit": "kwh", "currency": "eur"})
    assert (ents.currency, ents.unit) == ("EUR", "kWh")
    tib = parse_source({"action": "tibber.get_prices", "home": "Dom"})
    assert (tib.currency, tib.home, tib.unit) == (None, "Dom", None)
    for action in ("energyzero.get_energy_prices", "easyenergy.get_energy_usage_prices",
                   "easyenergy.get_energy_return_prices"):
        src = parse_source({"action": action, "data": {"config_entry": "e1", "incl_vat": True}})
        assert (src.currency, src.unit) == ("EUR", None)
