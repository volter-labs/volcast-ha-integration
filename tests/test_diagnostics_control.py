import asyncio
import json
from types import SimpleNamespace

from custom_components.volcast.diagnostics import async_get_config_entry_diagnostics

KEY = "vk_" + "c" * 64
BASE = "https://s.example.test"


def test_paired_diagnostics_redacts_secrets():
    ex = SimpleNamespace(exec_summary=lambda: {"decision": {"status": "dry_run"}}, tou_preview=None,
                         foreign_changes=[{"key": "mode", "entity_id": "select.gw_9010ABCD1234_mode"}])
    rt = SimpleNamespace(executor=ex, choice=SimpleNamespace(profile=SimpleNamespace(id="goodwe-et"),
                                                             integration_domain="goodwe"),
                         mapped={"mode": "select.gw_9010ABCD1234_mode"})
    entry = SimpleNamespace(entry_id="e1", options={"control_mode": "entities"},
                            data={"api_key": KEY, "user_id": "11111111-2222-3333-4444-555555555555",
                                  "backend": {"base_url": BASE}, "pairing": {"poll_token": "vps_secret"}})
    hass = SimpleNamespace(data={"volcast": {"e1": {"control": rt, "discovery": None}},
                                 "device_registry": SimpleNamespace(devices={"d": SimpleNamespace(
                                     serial_number="9010ABCD1234", identifiers={("goodwe", "9010ABCD1234")},
                                     config_entries={"g"})})})
    out = asyncio.run(async_get_config_entry_diagnostics(hass, entry))
    blob = json.dumps(out)
    for secret in (KEY, "vps_secret", "11111111-2222", "9010ABCD1234", "/functions/v1"):
        assert secret not in blob
    assert out["entry"]["paired"] is True and out["entry"]["backend_host"] == "s.example.test"
    assert out["control"]["profile"] == "goodwe-et" and out["control"]["exec"]["decision"]["status"] == "dry_run"
