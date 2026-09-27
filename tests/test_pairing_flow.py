"""Kreator: parowanie przez external step, aktualizacja istniejącego wpisu, przerwania."""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import tests.test_config_flow_menu  # noqa: F401 — rejestruje atrapy ConfigFlow/OptionsFlow


def _external_step(self, *, url, step_id=None, **_):
    # Jak HA: bez `step_id` krok zewnętrzny dostaje id kroku, który HA właśnie wywołał.
    result = {"type": "external", "url": url}
    if step_id is not None:
        result["step_id"] = step_id
    return result


_ce = sys.modules["homeassistant.config_entries"]
for _name, _fn in {
    "async_external_step": _external_step,
    "async_external_step_done": lambda self, *, next_step_id: {"type": "external_done", "next_step_id": next_step_id},
    "show_advanced_options": False,
    "flow_id": "flow1",
}.items():
    if not hasattr(_ce.ConfigFlow, _name):
        setattr(_ce.ConfigFlow, _name, _fn)

from custom_components.volcast import config_flow as cf  # noqa: E402
from custom_components.volcast.cloud.client import (Backend, PairingDisabled, PairingError,  # noqa: E402
                                                    PairingSession, PollResult)
from custom_components.volcast.key_format import account_unique_id  # noqa: E402

BASE = "https://staging.example.test"
BACKEND = Backend.from_dict({"base_url": BASE, **{k: f"{BASE}/functions/v1/{k}" for k in (
    "forecast", "submit_production", "telemetry", "schedule", "history_import", "pairing")}})
KEY = "vk_" + "b" * 64
UID = account_unique_id(KEY)
SESSION = PairingSession("s1", "vps_tok", "https://volcast.app/connect?s=s1", "t")
CONFIRMED = PollResult("confirmed", api_key=KEY, user_id="u1", backend=BACKEND)
COMPONENT = Path(__file__).parent.parent / "custom_components" / "volcast"


class FakeClient:
    polls: list = []
    begin_error: Exception | None = None
    cancelled: list = []
    begins: list = []
    urls: list = []

    def __init__(self, session, url):
        self.url = url
        FakeClient.urls.append(url)

    async def async_begin(self, **kw):
        FakeClient.begins.append(kw)
        if FakeClient.begin_error:
            raise FakeClient.begin_error
        return SESSION

    async def async_poll(self, s):
        return FakeClient.polls.pop(0) if len(FakeClient.polls) > 1 else FakeClient.polls[0]

    async def async_cancel(self, s):
        FakeClient.cancelled.append(s.session_id)


def entry(entry_id, data, options=None, *, listeners=(), title="Volcast"):
    return SimpleNamespace(entry_id=entry_id, data=data, options=options or {}, title=title,
                           update_listeners=list(listeners))


def make_flow(monkeypatch, *, polls, entries=(), begin_error=None):
    FakeClient.polls, FakeClient.begin_error, FakeClient.cancelled = list(polls), begin_error, []
    FakeClient.begins, FakeClient.urls = [], []
    monkeypatch.setattr(cf, "PairingClient", FakeClient)
    monkeypatch.setattr(cf, "async_get_clientsession", lambda hass: None)
    monkeypatch.setattr(cf, "PAIR_POLL_INTERVAL_S", 0.0)
    flow = cf.VolcastConfigFlow()
    tasks = []

    def create_task(coro, *_a, **_k):
        t = asyncio.get_running_loop().create_task(coro)
        tasks.append(t)
        return t

    flow.hass = SimpleNamespace(
        config=SimpleNamespace(location_name="Dom", time_zone="Europe/Warsaw"),
        async_create_task=create_task, data={},
        config_entries=SimpleNamespace(flow=SimpleNamespace(async_configure=AsyncMock()),
                                       async_update_entry=MagicMock(return_value=True),
                                       async_schedule_reload=MagicMock(),
                                       async_reload=AsyncMock()))
    flow._async_current_entries = lambda include_ignore=None: list(entries)
    flow.unique_ids = []

    async def set_unique_id(uid, **kw):
        flow.unique_ids.append((uid, kw))
    flow.async_set_unique_id = set_unique_id
    flow._tasks = tasks
    return flow


async def _pair_until_result(flow):
    await flow.async_step_pair()
    await asyncio.gather(*flow._tasks)
    return await flow.async_step_pair_finish()


def test_user_menu_offers_pair_first():
    r = asyncio.run(cf.VolcastConfigFlow().async_step_user())
    assert r["menu_options"] == ["pair", "api_key", "discovery_only"]


def test_pair_happy_path_creates_entry_with_backend(monkeypatch):
    flow = make_flow(monkeypatch, polls=[PollResult("pending"), CONFIRMED])

    async def go():
        ext = await flow.async_step_pair()
        await asyncio.gather(*flow._tasks)
        # HA wraca do kroku, który pokazał external step (tu: `pair`) — bez danych.
        done = await flow.async_step_pair()
        return ext, done, await flow.async_step_pair_finish()
    ext, done, created = asyncio.run(go())
    # Bez przestarzałego `step_id`: HA przypisze id wywołanego kroku.
    assert ext == {"type": "external", "url": SESSION.connect_url}
    flow.hass.config_entries.flow.async_configure.assert_awaited_with(flow_id="flow1")
    assert done == {"type": "external_done", "next_step_id": "pair_finish"}
    d = created["data"]
    assert created["type"] == "create_entry" and created["title"] == "Volcast — Dom"
    assert (d["api_key"], d["api_url"], d["user_id"]) == (KEY, BACKEND.forecast, "u1")
    assert d["backend"] == BACKEND.as_dict() and d["pairing"]["poll_token"] == "vps_tok"
    assert d["pairing"]["session_id"] == "s1" and d["pairing"]["url"] == cf.BETA_PAIRING_URL
    assert d["paired_at"] < d["pairing"]["live_until"]
    assert "mode" not in d
    # unique_id to skrót klucza, nie klucz; krok końcowy nie ustępuje równoległemu kreatorowi.
    assert flow.unique_ids == [(UID, {"raise_on_progress": False})]


def test_advanced_reentry_after_form_does_not_show_form_again(monkeypatch):
    """Po formularzu adresu external step ma id `pair` — powrót HA nie może pokazać formularza."""
    flow = make_flow(monkeypatch, polls=[CONFIRMED])
    flow.context = {"source": "user", "show_advanced_options": True}

    async def go():
        ext = await flow.async_step_pair({"pairing_url": cf.BETA_PAIRING_URL})
        await asyncio.gather(*flow._tasks)
        return ext, await flow.async_step_pair()
    ext, done = asyncio.run(go())
    assert ext["type"] == "external"
    assert done == {"type": "external_done", "next_step_id": "pair_finish"}


def test_begin_describes_this_instance(monkeypatch):
    flow = make_flow(monkeypatch, polls=[PollResult("pending")])

    async def go():
        await flow.async_step_pair()
        flow.async_remove()
        await asyncio.gather(*flow._tasks, return_exceptions=True)
    asyncio.run(go())
    (kw,) = FakeClient.begins
    assert kw["instance_id"] == "abcdef1234567890" and kw["instance_name"] == "Dom"
    assert kw["ha_version"] == "2026.9.0" and isinstance(kw["client_version"], str)
    assert FakeClient.urls == [cf.BETA_PAIRING_URL]


def test_pair_reentry_while_pending_does_not_begin_again(monkeypatch):
    flow = make_flow(monkeypatch, polls=[PollResult("pending")])

    async def go():
        first = await flow.async_step_pair()
        again = await flow.async_step_pair()
        flow.async_remove()
        await asyncio.gather(*flow._tasks, return_exceptions=True)
        return first, again
    first, again = asyncio.run(go())
    assert first == again and again["type"] == "external"
    assert len(FakeClient.begins) == 1


@pytest.mark.parametrize("status,reason", [("expired", "pairing_expired"), ("gone", "pairing_expired"),
                                            ("disabled", "pairing_disabled"),
                                            ("error", "pairing_connection_lost"),
                                            ("consumed", "pairing_failed")])
def test_poll_410_aborts_expired(monkeypatch, status, reason):
    flow = make_flow(monkeypatch, polls=[PollResult(status)])
    assert asyncio.run(_pair_until_result(flow)) == {"type": "abort", "reason": reason}
    flow.hass.config_entries.async_update_entry.assert_not_called()


def test_network_loss_until_deadline_is_connection_lost_not_expired(monkeypatch):
    flow = make_flow(monkeypatch, polls=[PollResult("pending"), PollResult("error")])
    monkeypatch.setattr(cf, "PAIR_DEADLINE_S", 0.05)
    assert asyncio.run(_pair_until_result(flow)) == {"type": "abort", "reason": "pairing_connection_lost"}


def test_confirmed_without_backend_is_failed(monkeypatch):
    flow = make_flow(monkeypatch, polls=[PollResult("confirmed", api_key=KEY, user_id="u1")])
    assert asyncio.run(_pair_until_result(flow)) == {"type": "abort", "reason": "pairing_failed"}


def test_poller_crash_still_finishes_the_flow(monkeypatch):
    flow = make_flow(monkeypatch, polls=[PollResult("pending")])

    async def boom(self, s):
        raise RuntimeError("boom")
    monkeypatch.setattr(FakeClient, "async_poll", boom)
    assert asyncio.run(_pair_until_result(flow)) == {"type": "abort", "reason": "pairing_failed"}
    flow.hass.config_entries.flow.async_configure.assert_awaited_with(flow_id="flow1")


@pytest.mark.parametrize("err,reason", [(PairingDisabled(), "pairing_disabled"), (PairingError("x"), "cannot_connect")])
def test_begin_failures_abort(monkeypatch, err, reason):
    flow = make_flow(monkeypatch, polls=[PollResult("pending")], begin_error=err)
    assert asyncio.run(flow.async_step_pair()) == {"type": "abort", "reason": reason}
    assert flow._tasks == []


def test_existing_forecast_entry_updated_in_place(monkeypatch):
    """Wpis konta ma listener aktualizacji — to on przeładowuje; drugiego przeładowania nie ma."""
    old = entry("e-old", {"api_key": "vk_" + "a" * 64, "api_url": "https://volcast.app/api/forecast",
                          "extra": 1}, {"pv_energy_entity": "sensor.pv"},
                listeners=[object()], title="Volcast — Kraków")
    flow = make_flow(monkeypatch, polls=[CONFIRMED], entries=[old])
    r = asyncio.run(_pair_until_result(flow))
    assert r == {"type": "abort", "reason": "paired_existing"}
    args, kwargs = flow.hass.config_entries.async_update_entry.call_args
    assert args[0] is old and kwargs["unique_id"] == UID
    assert kwargs["data"]["api_url"] == BACKEND.forecast and kwargs["data"]["backend"] == BACKEND.as_dict()
    assert kwargs["data"]["api_key"] == KEY and kwargs["data"]["extra"] == 1
    # Tytuł wpisu konta zostaje; opcje prognozy (encje produkcji) nie są ruszane.
    assert "title" not in kwargs
    assert "options" not in kwargs and old.options == {"pv_energy_entity": "sensor.pv"}
    flow.hass.config_entries.async_schedule_reload.assert_not_called()
    flow.hass.config_entries.async_reload.assert_not_awaited()


def test_entry_without_update_listener_reloaded_once(monkeypatch):
    """Wpis nieuruchomiony (np. czeka na ponowienie) nie ma listenera — przeładowanie planujemy sami."""
    old = entry("e-old", {"api_key": "vk_" + "a" * 64})
    flow = make_flow(monkeypatch, polls=[CONFIRMED], entries=[old])
    assert asyncio.run(_pair_until_result(flow)) == {"type": "abort", "reason": "paired_existing"}
    flow.hass.config_entries.async_schedule_reload.assert_called_once_with("e-old")
    flow.hass.config_entries.async_reload.assert_not_awaited()


def test_reload_falls_back_when_schedule_reload_missing(monkeypatch):
    old = entry("e-old", {"api_key": "vk_" + "a" * 64})
    flow = make_flow(monkeypatch, polls=[CONFIRMED], entries=[old])
    del flow.hass.config_entries.async_schedule_reload

    async def go():
        r = await _pair_until_result(flow)
        await asyncio.gather(*flow._tasks[1:])
        return r
    assert asyncio.run(go()) == {"type": "abort", "reason": "paired_existing"}
    flow.hass.config_entries.async_reload.assert_awaited_once_with("e-old")


def test_discovery_only_entry_converted(monkeypatch):
    disc = entry("e-disc", {"mode": "discovery_only"}, title="Volcast — discovery")
    flow = make_flow(monkeypatch, polls=[CONFIRMED], entries=[disc])
    assert asyncio.run(_pair_until_result(flow)) == {"type": "abort", "reason": "paired_existing"}
    args, kwargs = flow.hass.config_entries.async_update_entry.call_args
    assert args[0] is disc and "mode" not in kwargs["data"] and kwargs["data"]["api_key"] == KEY
    assert kwargs["title"] == "Volcast — Dom" and kwargs["unique_id"] == UID
    flow.hass.config_entries.async_schedule_reload.assert_called_once_with("e-disc")


def test_account_entry_preferred_over_discovery_entry(monkeypatch):
    disc = entry("e-disc", {"mode": "discovery_only"})
    acct = entry("e-acct", {"api_key": "vk_" + "a" * 64})
    flow = make_flow(monkeypatch, polls=[CONFIRMED], entries=[disc, acct])
    asyncio.run(_pair_until_result(flow))
    assert flow.hass.config_entries.async_update_entry.call_args.args[0] is acct


def test_multiple_account_entries_abort(monkeypatch):
    a = entry("a", {"api_key": "vk_1" * 5})
    b = entry("b", {"api_key": "vk_2" * 5})
    flow = make_flow(monkeypatch, polls=[PollResult("pending")], entries=[a, b])
    assert asyncio.run(flow.async_step_pair()) == {"type": "abort", "reason": "multiple_accounts"}
    assert FakeClient.begins == []


def test_account_key_never_logged(monkeypatch, caplog):
    old = entry("e-old", {"api_key": "vk_" + "a" * 64})
    flow = make_flow(monkeypatch, polls=[PollResult("error"), CONFIRMED], entries=[old])
    caplog.set_level(logging.DEBUG)
    asyncio.run(_pair_until_result(flow))
    assert KEY not in caplog.text and "vps_tok" not in caplog.text
    assert all(KEY not in repr(c) for c in flow.hass.config_entries.async_update_entry.call_args.kwargs.values()
               if not isinstance(c, dict))


def test_flow_removed_cancels_session(monkeypatch):
    flow = make_flow(monkeypatch, polls=[PollResult("pending")])

    async def go():
        await flow.async_step_pair()
        flow.async_remove()
        await asyncio.sleep(0)
        await asyncio.gather(*flow._tasks, return_exceptions=True)
        return flow._tasks[0]
    waiter = asyncio.run(go())
    assert FakeClient.cancelled == ["s1"] and waiter.cancelled()


@pytest.mark.parametrize("result", [CONFIRMED, PollResult("consumed")])
def test_flow_removed_after_credentials_does_not_cancel(monkeypatch, result):
    """Klucz już wydany — anulowanie niczego nie cofnie (chmura odpowie 409)."""
    flow = make_flow(monkeypatch, polls=[result])

    async def go():
        await _pair_until_result(flow)
        flow.async_remove()
        await asyncio.gather(*flow._tasks, return_exceptions=True)
    asyncio.run(go())
    assert FakeClient.cancelled == []


def test_flow_removed_before_begin_is_noop(monkeypatch):
    flow = make_flow(monkeypatch, polls=[PollResult("pending")])
    flow.async_remove()
    assert FakeClient.cancelled == [] and flow._tasks == []


def test_cancel_failure_is_swallowed(monkeypatch):
    flow = make_flow(monkeypatch, polls=[PollResult("pending")])

    async def bad_cancel(self, s):
        raise RuntimeError("offline")
    monkeypatch.setattr(FakeClient, "async_cancel", bad_cancel)

    async def go():
        await flow.async_step_pair()
        flow.async_remove()
        return await asyncio.gather(*flow._tasks, return_exceptions=True)
    results = asyncio.run(go())
    assert not any(isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError) for r in results)


def test_address_form_only_on_explicit_advanced_context(monkeypatch):
    """Bieżące HA: `show_advanced_options` zwraca True dla każdego — to nie może pokazać
    formularza adresu; decyduje tylko jawna flaga w kontekście kreatora."""
    flow = make_flow(monkeypatch, polls=[PollResult("pending")])
    flow.show_advanced_options = True
    flow.context = {"source": "user"}

    async def go():
        ext = await flow.async_step_pair()
        flow.async_remove()
        await asyncio.gather(*flow._tasks, return_exceptions=True)
        return ext
    assert asyncio.run(go())["type"] == "external"


def test_advanced_url_must_be_https(monkeypatch):
    flow = make_flow(monkeypatch, polls=[PollResult("pending")])
    flow.context = {"source": "user", "show_advanced_options": True}
    form = asyncio.run(flow.async_step_pair())
    assert form["type"] == "form" and form["step_id"] == "pair"
    for bad_url in ("http://x", "https://x.example/", "https://", "https://a b.example", "https://u@x.example/p"):
        bad = asyncio.run(flow.async_step_pair({"pairing_url": bad_url}))
        assert bad["errors"] == {"base": "invalid_url"}, bad_url
    assert FakeClient.begins == []


def test_advanced_url_is_used_and_stored(monkeypatch):
    url = "https://pair.example.test/functions/v1/pairing-session"
    flow = make_flow(monkeypatch, polls=[CONFIRMED])
    flow.context = {"source": "user", "show_advanced_options": True}

    async def go():
        await flow.async_step_pair({"pairing_url": f"  {url} "})
        await asyncio.gather(*flow._tasks)
        return await flow.async_step_pair_finish()
    created = asyncio.run(go())
    assert FakeClient.urls == [url] and created["data"]["pairing"]["url"] == url


@pytest.mark.parametrize("path", [COMPONENT / "strings.json", COMPONENT / "translations" / "en.json"])
def test_pairing_texts_present(path):
    cfg = json.loads(path.read_text(encoding="utf-8"))["config"]
    assert cfg["step"]["user"]["menu_options"]["pair"]
    # Krok `pair` opisuje zarówno formularz adresu (tryb zaawansowany), jak i external step.
    assert cfg["step"]["pair"]["description"] and cfg["step"]["pair"]["data"]["pairing_url"]
    assert "pair_wait" not in cfg["step"]
    for reason in ("pairing_expired", "pairing_disabled", "pairing_failed", "pairing_connection_lost",
                   "paired_existing", "multiple_accounts", "cannot_connect", "already_in_progress"):
        assert cfg["abort"][reason], reason
    assert cfg["error"]["invalid_url"]


def test_english_translation_matches_strings():
    strings = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
    en = json.loads((COMPONENT / "translations" / "en.json").read_text(encoding="utf-8"))
    assert en == strings
