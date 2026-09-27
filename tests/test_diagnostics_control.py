import asyncio
import json
from types import SimpleNamespace

from custom_components.volcast.diagnostics import async_get_config_entry_diagnostics

KEY = "vk_" + "c" * 64
BASE = "https://s.example.test"
BACKEND = {"base_url": BASE, **{k: f"{BASE}/functions/v1/{k}" for k in (
    "forecast", "submit_production", "telemetry", "schedule", "history_import", "pairing")}}


def test_paired_diagnostics_redacts_secrets():
    # Serial małymi literami w entity_id — tak wygląda w prawdziwych slugach HA;
    # maskowanie musi być niewrażliwe na wielkość liter (pattern IGNORECASE).
    ex = SimpleNamespace(exec_summary=lambda: {"decision": {"status": "dry_run"}}, tou_preview=None,
                         foreign_changes=[{"key": "mode", "entity_id": "select.gw_9010abcd1234_mode"}])
    rt = SimpleNamespace(executor=ex, choice=SimpleNamespace(profile=SimpleNamespace(id="goodwe-et"),
                                                             integration_domain="goodwe"),
                         mapped={"mode": "select.gw_9010abcd1234_mode"})
    entry = SimpleNamespace(entry_id="e1", options={"control_mode": "entities"},
                            data={"api_key": KEY, "user_id": "11111111-2222-3333-4444-555555555555",
                                  "backend": BACKEND, "pairing": {"poll_token": "vps_secret"}})
    hass = SimpleNamespace(data={"volcast": {"e1": {"control": rt, "discovery": None}},
                                 "device_registry": SimpleNamespace(devices={"d": SimpleNamespace(
                                     serial_number="9010ABCD1234", identifiers={("goodwe", "9010ABCD1234")},
                                     connections=set(), config_entries={"g"})})})
    out = asyncio.run(async_get_config_entry_diagnostics(hass, entry))
    blob = json.dumps(out)
    for secret in (KEY, "vps_secret", "11111111-2222", "9010ABCD1234", "9010abcd1234", "/functions/v1"):
        assert secret not in blob
    assert out["entry"]["paired"] is True and out["entry"]["backend_host"] == "s.example.test"
    assert out["control"]["profile"] == "goodwe-et" and out["control"]["exec"]["decision"]["status"] == "dry_run"


def test_control_section_masks_macs_in_every_slug_form():
    # Trzy formy, w jakich MAC trafia do entity_id: z dwukropkiem (łapie go już
    # ogólny wzorzec), z podkreśleniem (typowy slug HA dla MAC-a w unique_id/
    # entity_id) i zupełnie bez separatora.
    mac = "AA:BB:CC:DD:EE:FF"
    ex = SimpleNamespace(
        exec_summary=lambda: {"decision": {"status": "dry_run"}}, tou_preview=None,
        foreign_changes=[{"key": "mode", "entity_id": "select.shelly_aa_bb_cc_dd_ee_ff_mode"}])
    rt = SimpleNamespace(executor=ex, choice=SimpleNamespace(profile=SimpleNamespace(id="shelly"),
                                                             integration_domain="shelly"),
                         mapped={"mode": "select.shelly_aa_bb_cc_dd_ee_ff_mode",
                                "power_w": "number.dev_aabbccddeeff_soc"})
    entry = SimpleNamespace(entry_id="e1", options={},
                            data={"api_key": KEY, "backend": BACKEND})
    hass = SimpleNamespace(data={"volcast": {"e1": {"control": rt, "discovery": None}},
                                 "device_registry": SimpleNamespace(devices={"d": SimpleNamespace(
                                     serial_number=None, identifiers=set(),
                                     connections={("mac", mac)}, config_entries={"g"})})})
    out = asyncio.run(async_get_config_entry_diagnostics(hass, entry))
    blob = json.dumps(out).lower()
    assert "aabbccddeeff" not in blob and "aa_bb_cc_dd_ee_ff" not in blob and "aa:bb:cc:dd:ee:ff" not in blob


def test_wordlike_registry_identifier_does_not_over_mask_ordinary_text():
    # Identyfikator rejestru bez cyfry (słowo-klucz integracji, nie serial) nie
    # powinien zamieniać zwykłego słowa z entity_id na `<SN>`.
    ex = SimpleNamespace(exec_summary=lambda: {}, tou_preview=None, foreign_changes=[])
    rt = SimpleNamespace(executor=ex, choice=SimpleNamespace(profile=SimpleNamespace(id="p"),
                                                             integration_domain="d"),
                         mapped={"mode": "select.inverter_mode"})
    entry = SimpleNamespace(entry_id="e1", options={}, data={"api_key": KEY, "backend": BACKEND})
    hass = SimpleNamespace(data={"volcast": {"e1": {"control": rt, "discovery": None}},
                                 "device_registry": SimpleNamespace(devices={"d": SimpleNamespace(
                                     serial_number=None, identifiers={("hassio", "supervisor")},
                                     connections=set(), config_entries={"g"})})})
    out = asyncio.run(async_get_config_entry_diagnostics(hass, entry))
    assert out["control"]["mapped"]["mode"] == "select.inverter_mode"
