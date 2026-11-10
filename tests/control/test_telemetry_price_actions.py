"""Telemetria: ceny z akcji HA (`return_response`) obok cen z encji.

Wywołanie akcji idzie w zadaniu w tle (tik zegara go zleca), a wysyłka czyta tylko
gotowy wynik — wolna albo zawieszona akcja nie opóźnia telemetrii.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from custom_components.volcast.control.telemetry import TelemetrySender

from ..core.test_price_actions import nordpool_response
from .ha_fakes import GOODWE_ENTITIES as E, goodwe_hass
from .test_telemetry import GW, LOGGER, Cloud, Exec, _nordpool

NOW = datetime(2026, 10, 31, 9, 0, tzinfo=timezone.utc)          # 10:00 w Warszawie
NP = {"action": "nordpool.get_prices_for_date",
      "data": {"config_entry": "np1", "areas": "PL", "currency": "PLN"}}


def _services(h, responder):
    calls = []

    async def async_call(domain, service, data=None, blocking=False, context=None, target=None,
                         return_response=False):
        calls.append((domain, service, dict(data or {}), blocking, return_response))
        out = responder(domain, service, data or {})
        if isinstance(out, BaseException):
            raise out
        if asyncio.iscoroutine(out):
            return await out
        return out

    h.services.async_call = async_call
    return calls


def _np(_d, _s, data):
    return nordpool_response(date.fromisoformat(data["date"]), area="PL", base=500.0)


def _sender(h, cloud, options, clock=None):
    entry = SimpleNamespace(entry_id="e1", options=options)
    return TelemetrySender(h, entry, cloud, Exec(), choice=GW, profile_map=E, manual_map={}, grid_negate=False,
                           limits=None, utcnow=clock or (lambda: NOW))


async def _refresh(s):
    await asyncio.gather(*s._kick_price_actions(NOW))


def test_buy_prices_from_action():
    async def scenario():
        h, cloud = goodwe_hass(), Cloud()
        calls = _services(h, _np)
        s = _sender(h, cloud, {"price_buy_action": NP})
        await _refresh(s)
        assert await s.async_flush() is True
        return calls, cloud

    calls, cloud = asyncio.run(scenario())
    assert [(c[0], c[1], c[2]["date"], c[3], c[4]) for c in calls] == [
        ("nordpool", "get_prices_for_date", "2026-10-31", True, True),
        ("nordpool", "get_prices_for_date", "2026-11-01", True, True)]
    prices = cloud.sent[0]["prices"]
    assert prices["currency"] == "PLN" and prices["intervals"][0]["minutes"] == 15
    assert prices["intervals"][0]["buy"] == 0.5 and prices["intervals"][0]["sell"] is None


def test_action_failure_means_no_prices_section_but_reading_sent(caplog):
    caplog.set_level(logging.DEBUG)

    class HomeAssistantError(Exception):
        pass

    async def scenario():
        h, cloud = goodwe_hass(), Cloud()
        _services(h, lambda *_a: HomeAssistantError("np1 details"))
        s = _sender(h, cloud, {"price_buy_action": NP})
        await _refresh(s)
        assert await s.async_flush() is True
        return cloud

    cloud = asyncio.run(scenario())
    assert "prices" not in cloud.sent[0] and cloud.sent[0]["battery_soc"] == 60.0
    assert "HomeAssistantError" in caplog.text and "np1" not in caplog.text


def test_action_wins_over_entity_and_invalid_action_never_falls_back(caplog):
    caplog.set_level(logging.DEBUG)

    async def scenario():
        h, cloud = goodwe_hass(), Cloud()
        _nordpool(h)                                                # encja z cenami też jest
        calls = _services(h, _np)
        bad = {"action": "light.turn_on"}
        s = _sender(h, cloud, {"entity_price_buy": "sensor.nordpool", "price_buy_action": bad})
        await _refresh(s)
        await s.async_flush()
        await _refresh(s)
        await s.async_flush()
        return calls, cloud

    calls, cloud = asyncio.run(scenario())
    assert calls == [] and all("prices" not in r for r in cloud.sent)
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING
                and "price action" in r.getMessage()]) == 1         # raz na konfigurację


def test_sell_from_action_buy_from_entity():
    async def scenario():
        h, cloud = goodwe_hass(), Cloud()
        # Kupno z encji: doba 31.10 w 15-min, w PLN za kWh; sprzedaż z akcji (PLN/MWh).
        h.states.set("sensor.buy", "0.9", {"currency": "PLN", "raw_today": [
            {"start": s["start"], "end": s["end"], "value": 0.9}
            for s in nordpool_response(date(2026, 10, 31), area="PL")["PL"]]})
        _services(h, lambda _d, _s, data: nordpool_response(date.fromisoformat(data["date"]), area="PL", base=300.0))
        s = _sender(h, cloud, {"entity_price_buy": "sensor.buy", "price_sell_action": NP})
        await _refresh(s)
        await s.async_flush()
        return cloud

    ivs = asyncio.run(scenario()).sent[0]["prices"]["intervals"]
    assert len(ivs) == 96 and ivs[0]["buy"] == 0.9 and ivs[0]["sell"] == 0.3


def test_hanging_action_never_delays_telemetry():
    async def scenario():
        h, cloud = goodwe_hass(), Cloud()
        _services(h, lambda *_a: asyncio.sleep(3600))
        s = _sender(h, cloud, {"price_buy_action": NP})
        await asyncio.wait_for(s._timer(), timeout=1)               # tik: zlecenie w tle + wysyłka
        assert len(cloud.sent) == 1 and "prices" not in cloud.sent[0]
        # Kolejny tik w trakcie trwającego wywołania nie zleca drugiego.
        assert s._kick_price_actions(NOW + timedelta(minutes=1)) == []
        await s.async_stop()
        await asyncio.sleep(0)
        assert all(t.done() for t in h.tasks)                       # zatrzymanie anuluje zadanie cen

    asyncio.run(scenario())


def test_no_action_configured_makes_no_calls():
    async def scenario():
        h, cloud = goodwe_hass(), Cloud()
        calls = _services(h, _np)
        _nordpool(h)
        s = _sender(h, cloud, {"entity_price_buy": "sensor.nordpool"})
        await s._timer()
        return calls, cloud

    calls, cloud = asyncio.run(scenario())
    assert calls == [] and len(cloud.sent[0]["prices"]["intervals"]) == 24


def test_refresh_follows_schedule_not_every_tick():
    t = {"now": NOW}

    async def scenario():
        h, cloud = goodwe_hass(), Cloud()
        calls = _services(h, _np)
        s = _sender(h, cloud, {"price_buy_action": NP}, clock=lambda: t["now"])
        for minutes in (0, 1, 2, 30, 359):
            t["now"] = NOW + timedelta(minutes=minutes)
            await asyncio.gather(*s._kick_price_actions(t["now"]))
        t["now"] = NOW + timedelta(hours=6)
        await asyncio.gather(*s._kick_price_actions(t["now"]))
        return calls

    assert len(asyncio.run(scenario())) == 4                        # dwa odświeżenia × (dziś + jutro)


def test_kick_error_never_blocks_flush(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)

    async def scenario():
        h, cloud = goodwe_hass(), Cloud()
        s = _sender(h, cloud, {"price_buy_action": NP})

        def boom(*_a, **_k):
            raise RuntimeError("sensor.secret")
        monkeypatch.setattr(s, "_kick_price_actions", boom)
        await s._timer()
        return cloud

    assert len(asyncio.run(scenario()).sent) == 1
    assert "secret" not in caplog.text and "RuntimeError" in caplog.text


# ── strona sprzedaży z akcji: bez wyniku → bez sekcji `prices` (nie kupno z `sell: null`) ──


def _buy_entity(h):
    h.states.set("sensor.buy", "0.9", {"currency": "PLN", "raw_today": [
        {"start": s["start"], "end": s["end"], "value": 0.9}
        for s in nordpool_response(date(2026, 10, 31), area="PL")["PL"]]})


def _np_sell(_d, _s, data):
    return nordpool_response(date.fromisoformat(data["date"]), area="PL", base=300.0)


def test_sell_action_pending_at_start_sends_no_prices():
    async def scenario():
        h, cloud = goodwe_hass(), Cloud()
        _buy_entity(h)
        _services(h, lambda *_a: asyncio.sleep(3600))
        s = _sender(h, cloud, {"entity_price_buy": "sensor.buy", "price_sell_action": NP})
        await s._timer()                                            # zadanie w tle trwa
        await s.async_stop()
        return cloud

    sent = asyncio.run(scenario()).sent
    assert len(sent) == 1 and "prices" not in sent[0]


def test_sell_action_failed_sends_no_prices_even_after_earlier_success():
    t = {"now": NOW}

    async def scenario():
        h, cloud = goodwe_hass(), Cloud()
        _buy_entity(h)
        state = {"fail": True}
        _services(h, lambda *a: RuntimeError("down") if state["fail"] else _np_sell(*a))
        s = _sender(h, cloud, {"entity_price_buy": "sensor.buy", "price_sell_action": NP}, clock=lambda: t["now"])
        await asyncio.gather(*s._kick_price_actions(t["now"]))
        await s.async_flush()                                       # błąd od początku
        state["fail"] = False
        t["now"] = NOW + timedelta(minutes=5)
        await asyncio.gather(*s._kick_price_actions(t["now"]))
        await s.async_flush()                                       # udane → pełny blok
        state["fail"] = True
        t["now"] = NOW + timedelta(hours=6, minutes=5)
        await asyncio.gather(*s._kick_price_actions(t["now"]))
        await s.async_flush()                                       # późniejszy błąd → znowu bez cen
        return cloud

    sent = asyncio.run(scenario()).sent
    assert ["prices" in r for r in sent] == [False, True, False]
    assert sent[1]["prices"]["intervals"][0]["sell"] == 0.3


def test_sell_not_configured_keeps_buy_only_with_null_sell():
    async def scenario():
        h, cloud = goodwe_hass(), Cloud()
        _services(h, _np)
        s = _sender(h, cloud, {"price_buy_action": NP})
        await _refresh(s)
        await s.async_flush()
        return cloud

    ivs = asyncio.run(scenario()).sent[0]["prices"]["intervals"]
    assert ivs and all(iv["sell"] is None for iv in ivs)


def test_both_actions_successful_send_full_block():
    async def scenario():
        h, cloud = goodwe_hass(), Cloud()
        _services(h, lambda d, sv, data: _np_sell(d, sv, data) if data.get("areas") == ["PL"] and
                  data.get("config_entry") == "sell" else _np(d, sv, data))
        sell = {**NP, "data": {**NP["data"], "config_entry": "sell"}}
        s = _sender(h, cloud, {"price_buy_action": NP, "price_sell_action": sell})
        await _refresh(s)
        await s.async_flush()
        return cloud

    ivs = asyncio.run(scenario()).sent[0]["prices"]["intervals"]
    assert len(ivs) == 192 and ivs[0]["buy"] == 0.5 and all(iv["sell"] is not None for iv in ivs)


def test_sell_action_with_nothing_paired_sends_no_prices():
    # Sprzedaż z akcji na przesuniętej siatce (godziny od :05 przy kupnie 15 min) — adapter jej nie
    # paruje; to nie „rynek bez sprzedaży”, więc kupno z samymi `sell: null` nie idzie.
    def _przesuniete(_d, _s, data):
        przesun = timedelta(minutes=5)
        wiersze = nordpool_response(date.fromisoformat(data["date"]), area="PL", minutes=60, base=300.0)["PL"]
        return {"PL": [{**w, "start": (datetime.fromisoformat(w["start"]) + przesun).isoformat(),
                        "end": (datetime.fromisoformat(w["end"]) + przesun).isoformat()} for w in wiersze]}

    async def scenario():
        h, cloud = goodwe_hass(), Cloud()
        _buy_entity(h)
        _services(h, _przesuniete)
        s = _sender(h, cloud, {"entity_price_buy": "sensor.buy", "price_sell_action": NP})
        await _refresh(s)
        await s.async_flush()
        return cloud

    assert "prices" not in asyncio.run(scenario()).sent[0]


def test_invalid_sell_action_sends_no_prices():
    async def scenario():
        h, cloud = goodwe_hass(), Cloud()
        _buy_entity(h)
        s = _sender(h, cloud, {"entity_price_buy": "sensor.buy", "price_sell_action": {"action": "x.y"}})
        await s._timer()
        return cloud

    assert "prices" not in asyncio.run(scenario()).sent[0]
