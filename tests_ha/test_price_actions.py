"""Ceny z akcji HA na prawdziwym rdzeniu: rejestr usług, walidacja schematu, `return_response`.

Atrapy akcji mają schematy przepisane z integracji rdzenia HA (nordpool, tibber, energyzero,
amberelectric — ich biblioteki klienckie nie są instalowane w zestawie testowym) i odpowiedź
w kształcie ich serializacji. Sprawdzamy, że dane, które składa wtyczka (daty doby rynku), przechodzą
prawdziwą walidację usług, że błąd każdego rodzaju kończy się brakiem sekcji `prices`, a zadanie
w tle znika przy zatrzymaniu.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

import pytest
import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import ConfigEntrySelector

from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.volcast.const import DOMAIN
from custom_components.volcast.control.telemetry import TelemetrySender

NOW = datetime(2026, 10, 31, 9, 0, tzinfo=timezone.utc)          # 10:00 w Warszawie
TODAY = date(2026, 10, 31)


class Cloud:
    def __init__(self):
        self.sent = []

    async def async_post_telemetry(self, reading):
        from custom_components.volcast.cloud.client import TelemetryResult
        self.sent.append(reading)
        return TelemetryResult(200, None)


class Exec:
    local_switch = False

    def exec_summary(self):
        return {}


def _quarters(day: date, tz: str):
    from zoneinfo import ZoneInfo
    z = ZoneInfo(tz)
    t = datetime(day.year, day.month, day.day, tzinfo=z).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=z).astimezone(timezone.utc)
    while t < end:
        yield t
        t += timedelta(minutes=15)


def _register_nordpool(hass, calls, *, tomorrow_published=True):
    schema = vol.Schema({
        vol.Required("config_entry"): ConfigEntrySelector({"integration": "nordpool"}),
        vol.Required("date"): cv.date,
        vol.Optional("areas", default=[]): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("currency"): vol.All(cv.string, vol.Upper, vol.In(["EUR", "PLN", "SEK"])),
    })

    async def handle(call: ServiceCall):
        calls.append(dict(call.data))
        asked: date = call.data["date"]                              # schemat zamienił tekst na datę
        if asked > TODAY and not tomorrow_published:
            return {area: [] for area in call.data["areas"]}
        return {area: [{"start": s.isoformat(), "end": (s + timedelta(minutes=15)).isoformat(), "price": 480.0}
                       for s in _quarters(asked, "Europe/Warsaw")] for area in call.data["areas"]}

    hass.services.async_register("nordpool", "get_prices_for_date", handle, schema=schema,
                                 supports_response=SupportsResponse.ONLY)


NORDPOOL = {"action": "nordpool.get_prices_for_date",
            "data": {"config_entry": "np_entry", "areas": "PL", "currency": "PLN"}}


async def _sender(hass, options):
    entry = MockConfigEntry(domain=DOMAIN, title="Volcast", data={}, options=options, entry_id="prices01")
    entry.add_to_hass(hass)
    await hass.config.async_update(time_zone="Europe/Warsaw")
    cloud = Cloud()
    # Odczyt monitoringu, żeby paczka szła także bez cen (brak sekcji `prices` ≠ brak paczki).
    hass.states.async_set("sensor.test_soc", "55", {"unit_of_measurement": "%"})
    sender = TelemetrySender(hass, entry, cloud, Exec(), choice=None, profile_map={},
                             manual_map={"soc": "sensor.test_soc"},
                             grid_negate=False, limits=None, utcnow=lambda: NOW)
    return sender, cloud, entry


async def _refresh_and_flush(sender):
    await asyncio.gather(*sender._kick_price_actions(NOW))
    await sender.async_flush()


async def test_nordpool_action_through_real_service_registry(hass: HomeAssistant):
    calls = []
    _register_nordpool(hass, calls)
    sender, cloud, _entry = await _sender(hass, {"price_buy_action": NORDPOOL})
    await _refresh_and_flush(sender)
    assert [c["date"] for c in calls] == [TODAY, TODAY + timedelta(days=1)]
    assert calls[0]["areas"] == ["PL"] and calls[0]["currency"] == "PLN"
    prices = cloud.sent[0]["prices"]
    assert prices["currency"] == "PLN" and prices["market"] == "PL"  # harness bez kraju → rynek domyślny
    assert prices["intervals"][0]["buy"] == pytest.approx(0.48)     # PLN/MWh → PLN/kWh
    assert len(prices["intervals"]) == 192 and prices["intervals"][0]["startAt"] == "2026-10-30T23:00:00Z"


async def test_tomorrow_not_published_sends_whole_today(hass: HomeAssistant):
    calls = []
    _register_nordpool(hass, calls, tomorrow_published=False)
    sender, cloud, _entry = await _sender(hass, {"price_buy_action": NORDPOOL})
    await _refresh_and_flush(sender)
    assert len(cloud.sent[0]["prices"]["intervals"]) == 96


async def test_tibber_strict_schema_gets_only_dates(hass: HomeAssistant):
    # Schemat Tibbera nie przyjmuje innych kluczy niż start/end — wybór domu nie może trafić do danych.
    calls = []

    async def handle(call: ServiceCall):
        calls.append(dict(call.data))
        start = datetime.fromisoformat(call.data["start"])
        return {"prices": {"Dom": [{"start_time": s.astimezone(start.tzinfo).isoformat(timespec="milliseconds"),
                                    "price": 1.25} for s in _quarters(start.date(), "Europe/Warsaw")]}}

    hass.services.async_register("tibber", "get_prices", handle,
                                 schema=vol.Schema({vol.Optional("start"): str, vol.Optional("end"): str}),
                                 supports_response=SupportsResponse.ONLY)
    sender, cloud, _entry = await _sender(hass, {"price_buy_action": {"action": "tibber.get_prices", "home": "Dom",
                                                                      "currency": "SEK"}})
    await _refresh_and_flush(sender)
    assert calls[0] == {"start": "2026-10-31T00:00:00+01:00", "end": "2026-11-01T00:00:00+01:00"}
    ivs = cloud.sent[0]["prices"]["intervals"]
    assert cloud.sent[0]["prices"]["currency"] == "SEK" and len(ivs) == 192 and ivs[0]["minutes"] == 15


async def test_energyzero_tomorrow_validation_error_keeps_today(hass: HomeAssistant):
    async def handle(call: ServiceCall):
        day = date.fromisoformat(call.data["start"])
        if day > TODAY:
            raise ServiceValidationError("no data for tomorrow")
        return {"prices": [{"price": 0.21, "timestamp": str(s)} for s in _quarters(day, "Europe/Warsaw")][::4]}

    hass.services.async_register("energyzero", "get_energy_prices", handle,
                                 schema=vol.Schema({vol.Required("config_entry"): ConfigEntrySelector(
                                     {"integration": "energyzero"}), vol.Required("incl_vat"): bool,
                                     vol.Optional("start"): str, vol.Optional("end"): str}),
                                 supports_response=SupportsResponse.ONLY)
    sender, cloud, _entry = await _sender(hass, {"price_buy_action": {
        "action": "energyzero.get_energy_prices", "data": {"config_entry": "ez", "incl_vat": True}}})
    await _refresh_and_flush(sender)
    ivs = cloud.sent[0]["prices"]["intervals"]
    assert cloud.sent[0]["prices"]["currency"] == "EUR" and len(ivs) == 24 and ivs[0]["minutes"] == 60


@pytest.mark.parametrize("kind", ["raises", "missing", "no_response_support", "schema", "malformed"])
async def test_every_action_error_means_no_prices_and_logs_type(hass: HomeAssistant, caplog, kind):
    caplog.set_level(logging.DEBUG, logger="custom_components.volcast")
    expected = {"raises": "HomeAssistantError", "missing": "ServiceNotFound",
                "no_response_support": "ServiceValidationError", "schema": "MultipleInvalid",
                "malformed": "InvalidPriceResponse"}[kind]

    async def boom(call: ServiceCall):
        raise HomeAssistantError("np_entry upstream detail")

    async def plain(call: ServiceCall):
        return None

    async def malformed(call: ServiceCall):
        return {"XX": []}

    if kind == "raises":
        hass.services.async_register("nordpool", "get_prices_for_date", boom,
                                     supports_response=SupportsResponse.ONLY)
    elif kind == "no_response_support":
        hass.services.async_register("nordpool", "get_prices_for_date", plain)
    elif kind == "schema":
        _register_nordpool(hass, [])
    elif kind == "malformed":
        hass.services.async_register("nordpool", "get_prices_for_date", malformed,
                                     supports_response=SupportsResponse.ONLY)
    source = NORDPOOL if kind != "schema" else {
        "action": "nordpool.get_prices_for_date",
        "data": {"config_entry": "np_entry", "areas": "PL", "currency": "NOK"}}   # waluta spoza schematu
    sender, cloud, _entry = await _sender(hass, {"price_buy_action": source})
    await _refresh_and_flush(sender)
    assert "prices" not in cloud.sent[0]
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("custom_components.volcast"))
    assert f"nordpool.get_prices_for_date failed ({expected})" in ours
    # Nasz log: nazwa akcji i typ błędu, bez danych akcji i treści wyjątku (rdzeń HA loguje swoje sam).
    assert "np_entry" not in ours and "upstream detail" not in ours


async def test_timer_runs_action_in_background_and_stop_cancels(hass: HomeAssistant):
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(call: ServiceCall):
        started.set()
        await release.wait()
        return {"PL": []}

    hass.services.async_register("nordpool", "get_prices_for_date", slow, supports_response=SupportsResponse.ONLY)
    sender, cloud, _entry = await _sender(hass, {"price_buy_action": NORDPOOL})
    await sender.async_start()
    async_fire_time_changed(hass, datetime.now(timezone.utc) + timedelta(seconds=61))
    await asyncio.wait_for(started.wait(), timeout=5)
    await hass.async_block_till_done()
    # Wysyłka poszła mimo trwającej akcji — bez cen; drugi tik nie woła akcji drugi raz.
    assert len(cloud.sent) == 1 and "prices" not in cloud.sent[0]
    assert sender._kick_price_actions(NOW) == []
    task = sender._price_tasks["price_buy_action"]
    await sender.async_stop()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sender._price_tasks == {}


# ── pozostałe kształty: Amber (kupno + sprzedaż), EasyEnergy (zużycie + zwrot), ENTSO-e (skala) ──


async def test_amber_general_and_feed_in_actions(hass: HomeAssistant):
    from zoneinfo import ZoneInfo
    calls = []

    async def handle(call: ServiceCall):
        calls.append(dict(call.data))
        sign = 0.5 if call.data["channel_type"] == "feed_in" else 1.0
        start = datetime(2026, 10, 31, 20, 0, tzinfo=ZoneInfo("Australia/Sydney"))
        rows = []
        for i in range(48):
            s = start + timedelta(minutes=30 * i)
            rows.append({"duration": 30, "date": s.date().isoformat(), "per_kwh": sign * 0.3, "spot_per_kwh": 0.1,
                         "start_time": (s + timedelta(seconds=1)).isoformat(),
                         "end_time": (s + timedelta(minutes=30)).isoformat(), "renewables": 40,
                         "spike_status": "none", "descriptor": "neutral"})
        return {"forecasts": rows}

    hass.services.async_register("amberelectric", "get_forecasts", handle, schema=vol.Schema({
        "config_entry_id": ConfigEntrySelector({"integration": "amberelectric"}),
        "channel_type": vol.In(["general", "controlled_load", "feed_in"])}), supports_response=SupportsResponse.ONLY)
    amber = {"action": "amberelectric.get_forecasts", "data": {"config_entry_id": "amb", "channel_type": "general"}}
    feed = {**amber, "data": {"config_entry_id": "amb", "channel_type": "feed_in"}}
    sender, cloud, _entry = await _sender(hass, {"price_buy_action": amber, "price_sell_action": feed})
    await _refresh_and_flush(sender)
    assert sorted(c["channel_type"] for c in calls) == ["feed_in", "general"]
    prices = cloud.sent[0]["prices"]
    assert prices["currency"] == "AUD" and len(prices["intervals"]) == 48
    assert prices["intervals"][0]["minutes"] == 30 and prices["intervals"][0]["startAt"] == "2026-10-31T09:00:00Z"
    assert prices["intervals"][0]["buy"] == 0.3 and prices["intervals"][0]["sell"] == 0.15


def _register_easyenergy(hass, calls, *, fail_return=False):
    base = {vol.Required("config_entry"): ConfigEntrySelector({"integration": "easyenergy"}),
            vol.Optional("start"): str, vol.Optional("end"): str}

    def handler(kind):
        async def handle(call: ServiceCall):
            calls.append((kind, dict(call.data)))
            if kind == "return" and fail_return:
                raise HomeAssistantError("upstream down")
            day = date.fromisoformat(call.data["start"])
            price = 0.25 if kind == "usage" else 0.07
            return {"prices": [{"timestamp": str(s), "price": price}
                               for s in list(_quarters(day, "Europe/Warsaw"))[::4]]}
        return handle

    hass.services.async_register("easyenergy", "get_energy_usage_prices", handler("usage"), schema=vol.Schema({
        **base, vol.Required("incl_vat"): bool, vol.Optional("granularity", default="hour"): vol.In(["hour"]),
        vol.Optional("price_type", default="market"): vol.In(["market", "invoice"])}),
        supports_response=SupportsResponse.ONLY)
    hass.services.async_register("easyenergy", "get_energy_return_prices", handler("return"), schema=vol.Schema({
        **base, vol.Optional("granularity", default="hour"): vol.In(["hour"])}),
        supports_response=SupportsResponse.ONLY)


EASY_USAGE = {"action": "easyenergy.get_energy_usage_prices", "data": {"config_entry": "ee", "incl_vat": True}}
EASY_RETURN = {"action": "easyenergy.get_energy_return_prices", "data": {"config_entry": "ee"}}


async def test_easyenergy_usage_and_return_actions(hass: HomeAssistant):
    calls = []
    _register_easyenergy(hass, calls)
    sender, cloud, _entry = await _sender(hass, {"price_buy_action": EASY_USAGE, "price_sell_action": EASY_RETURN})
    await _refresh_and_flush(sender)
    assert {(k, c["start"], c["end"]) for k, c in calls} == {
        (k, d, d) for k in ("usage", "return") for d in ("2026-10-31", "2026-11-01")}
    ivs = cloud.sent[0]["prices"]["intervals"]
    assert cloud.sent[0]["prices"]["currency"] == "EUR" and len(ivs) == 48
    assert ivs[0]["buy"] == 0.25 and all(iv["sell"] == 0.07 for iv in ivs)


async def test_failed_sell_action_sends_no_prices_on_real_bus(hass: HomeAssistant):
    _register_easyenergy(hass, [], fail_return=True)
    sender, cloud, _entry = await _sender(hass, {"price_buy_action": EASY_USAGE, "price_sell_action": EASY_RETURN})
    await _refresh_and_flush(sender)
    assert "prices" not in cloud.sent[0]                             # nie kupno z `sell: null`


def _register_entsoe(hass, calls, price):
    async def handle(call: ServiceCall):
        calls.append(dict(call.data))
        start = datetime.fromisoformat(call.data["start"])
        return {"prices": [{"timestamp": s.isoformat(), "price": price}
                           for s in list(_quarters(start.date(), "Europe/Warsaw"))[::4]]}

    hass.services.async_register("entsoe", "get_energy_prices", handle, schema=vol.Schema({
        vol.Required("config_entry"): str, vol.Optional("start"): str, vol.Optional("end"): str}),
        supports_response=SupportsResponse.ONLY)


@pytest.mark.parametrize(("unit", "price", "expected"), [("MWh", 95.0, 0.095), ("kWh", 0.095, 0.095)])
async def test_entsoe_scale_from_configured_unit(hass: HomeAssistant, unit, price, expected):
    _register_entsoe(hass, [], price)
    sender, cloud, _entry = await _sender(hass, {"price_buy_action": {
        "action": "entsoe.get_energy_prices", "data": {"config_entry": "es"}, "currency": "EUR", "unit": unit}})
    await _refresh_and_flush(sender)
    ivs = cloud.sent[0]["prices"]["intervals"]
    assert len(ivs) == 48 and ivs[0]["buy"] == pytest.approx(expected)


async def test_entsoe_without_unit_is_rejected(hass: HomeAssistant, caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.volcast")
    calls = []
    _register_entsoe(hass, calls, 95.0)
    sender, cloud, _entry = await _sender(hass, {"price_buy_action": {
        "action": "entsoe.get_energy_prices", "data": {"config_entry": "es"}, "currency": "EUR"}})
    await _refresh_and_flush(sender)
    assert calls == [] and "prices" not in cloud.sent[0]
    assert "InvalidPriceSource" in caplog.text
