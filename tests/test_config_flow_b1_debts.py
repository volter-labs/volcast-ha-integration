import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import tests.test_config_flow_menu  # noqa: F401
from custom_components.volcast import config_flow as cf
from custom_components.volcast.key_format import account_unique_id


def test_discovery_only_with_disabled_entry_gives_clear_reason():
    flow = cf.VolcastConfigFlow()
    flow._async_current_entries = lambda include_ignore=None: [
        SimpleNamespace(entry_id="a", data={"api_key": "vk_x"}, disabled_by="user")]
    assert asyncio.run(flow.async_step_discovery_only()) == {"type": "abort", "reason": "existing_entry_disabled"}


def test_api_key_entry_converts_existing_discovery_only_entry():
    flow = cf.VolcastConfigFlow()
    disc = SimpleNamespace(entry_id="d", data={"mode": "discovery_only"}, options={}, disabled_by=None)
    flow._async_current_entries = lambda include_ignore=None: [disc]
    flow.hass = SimpleNamespace(config_entries=SimpleNamespace(async_update_entry=MagicMock(return_value=True),
                                                               async_schedule_reload=MagicMock(),
                                                               async_reload=AsyncMock()))
    key = "vk_" + "d" * 64
    flow._api_data = {"api_key": key, "api_url": "https://volcast.app/api/forecast", "title": "Volcast — X"}
    r = asyncio.run(flow.async_step_production({"pv_energy_entity": "sensor.pv"}))
    assert r == {"type": "abort", "reason": "converted_existing"}
    # Ta sama ścieżka co przy parowaniu: przeładowanie zaplanowane raz, kreator na nie nie czeka.
    flow.hass.config_entries.async_schedule_reload.assert_called_once_with("d")
    flow.hass.config_entries.async_reload.assert_not_awaited()
    (entry,), kw = flow.hass.config_entries.async_update_entry.call_args
    assert entry is disc
    assert "mode" not in kw["data"] and kw["options"]["pv_energy_entity"] == "sensor.pv"
    # HA loguje unique_id w tekście jawnym — nigdy surowy klucz (`key_format.py`).
    assert kw["unique_id"] == account_unique_id(key) and key not in str(kw["unique_id"])


def _flow_with(entries):
    flow = cf.VolcastConfigFlow()
    flow._async_current_entries = lambda include_ignore=None: list(entries)
    flow.hass = SimpleNamespace(config_entries=SimpleNamespace(async_update_entry=MagicMock(return_value=True),
                                                               async_schedule_reload=MagicMock(),
                                                               async_reload=AsyncMock()))
    return flow


DISABLED_DISC = SimpleNamespace(entry_id="d", data={"mode": "discovery_only"}, options={}, disabled_by="user")


def test_disabled_discovery_entry_is_not_converted_by_api_key(monkeypatch):
    """Wyłączony wpis „tylko rozpoznanie": najpierw go włącz — nic nie przerabiamy po cichu."""
    flow = _flow_with([DISABLED_DISC])
    validate = AsyncMock(side_effect=AssertionError("no network call for a disabled target"))
    monkeypatch.setattr(cf, "_validate_api_key", validate)
    r = asyncio.run(flow.async_step_api_key({"api_key": "vk_" + "e" * 64}))
    assert r == {"type": "abort", "reason": "existing_entry_disabled"}
    # Także gdy wpis wyłączono między krokami kreatora.
    flow._api_data = {"api_key": "vk_" + "e" * 64, "api_url": "https://volcast.app/api/forecast", "title": "T"}
    r = asyncio.run(flow.async_step_production({}))
    assert r == {"type": "abort", "reason": "existing_entry_disabled"}
    flow.hass.config_entries.async_update_entry.assert_not_called()
    flow.hass.config_entries.async_schedule_reload.assert_not_called()


def test_disabled_discovery_entry_is_not_converted_by_pairing():
    flow = _flow_with([DISABLED_DISC])
    assert asyncio.run(flow.async_step_pair()) == {"type": "abort", "reason": "existing_entry_disabled"}
    # Krok końcowy też odmawia (wpis wyłączony w trakcie oczekiwania na potwierdzenie).
    flow._result = cf.PollResult("confirmed", api_key="vk_" + "e" * 64, user_id="u1",
                                 backend=cf.Backend.from_dict({"base_url": "https://s.example.test", **{
                                     k: f"https://s.example.test/functions/v1/{k}" for k in (
                                         "forecast", "submit_production", "telemetry", "schedule",
                                         "history_import", "pairing")}}))
    flow._session = SimpleNamespace(session_id="s", poll_token="t")
    assert asyncio.run(flow.async_step_pair_finish()) == {"type": "abort", "reason": "existing_entry_disabled"}
    flow.hass.config_entries.async_update_entry.assert_not_called()


def test_disabled_account_entry_is_not_paired_into_either():
    """Wyłączony wpis konta ma tę samą regułę co wpis „tylko rozpoznanie": nie przerabiamy go po cichu."""
    acc = SimpleNamespace(entry_id="a", data={"api_key": "vk_x"}, options={}, disabled_by="user")
    flow = _flow_with([acc])
    assert flow._account_target_disabled() is True
    assert asyncio.run(flow.async_step_pair()) == {"type": "abort", "reason": "existing_entry_disabled"}
    flow._result = cf.PollResult("confirmed", api_key="vk_" + "e" * 64, user_id="u1",
                                 backend=cf.Backend.from_dict({"base_url": "https://s.example.test", **{
                                     k: f"https://s.example.test/functions/v1/{k}" for k in (
                                         "forecast", "submit_production", "telemetry", "schedule",
                                         "history_import", "pairing")}}))
    flow._session = SimpleNamespace(session_id="s", poll_token="t")
    assert asyncio.run(flow.async_step_pair_finish()) == {"type": "abort", "reason": "existing_entry_disabled"}
    flow.hass.config_entries.async_update_entry.assert_not_called()
