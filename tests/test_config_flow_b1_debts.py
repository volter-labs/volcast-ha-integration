import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import tests.test_config_flow_menu  # noqa: F401
from custom_components.volcast import config_flow as cf


def test_discovery_only_with_disabled_entry_gives_clear_reason():
    flow = cf.VolcastConfigFlow()
    flow._async_current_entries = lambda include_ignore=None: [
        SimpleNamespace(entry_id="a", data={"api_key": "vk_x"}, disabled_by="user")]
    assert asyncio.run(flow.async_step_discovery_only()) == {"type": "abort", "reason": "existing_entry_disabled"}


def test_api_key_entry_converts_existing_discovery_only_entry():
    flow = cf.VolcastConfigFlow()
    disc = SimpleNamespace(entry_id="d", data={"mode": "discovery_only"}, options={}, disabled_by=None)
    flow._async_current_entries = lambda include_ignore=None: [disc]
    flow.hass = SimpleNamespace(config_entries=SimpleNamespace(async_update_entry=MagicMock(),
                                                               async_reload=AsyncMock()))
    flow._api_data = {"api_key": "vk_" + "d" * 64, "api_url": "https://volcast.app/api/forecast", "title": "Volcast — X"}
    r = asyncio.run(flow.async_step_production({"pv_energy_entity": "sensor.pv"}))
    assert r == {"type": "abort", "reason": "converted_existing"}
    kw = flow.hass.config_entries.async_update_entry.call_args.kwargs
    assert "mode" not in kw["data"] and kw["options"]["pv_energy_entity"] == "sensor.pv"
