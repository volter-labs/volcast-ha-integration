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
