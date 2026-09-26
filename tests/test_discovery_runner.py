"""Warstwa HA wykrywania instalacji: migawki rejestrów, rekorder, sygnał."""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.volcast import discovery_runner as dr_mod
from custom_components.volcast.discovery_runner import DiscoveryRunner
from custom_components.volcast.core.discovery.network import NetworkProbeResult

pytestmark = pytest.mark.asyncio


def _device(**kw):
    base = dict(id="d1", manufacturer="GoodWe", model="GW10K-ET", name="GoodWe", sw_version="1",
                hw_version=None, serial_number="9010KETU000W0001", identifiers={("goodwe", "9010KETU000W0001")},
                config_entries={"g1"})
    base.update(kw); return SimpleNamespace(**base)


def _entity(**kw):
    base = dict(entity_id="sensor.battery_state_of_charge", platform="goodwe", unique_id="9010KETU000W0001-battery_soc",
                device_id="d1", config_entry_id="g1", device_class=None, original_device_class="battery",
                unit_of_measurement="%", translation_key=None, original_name="Battery SoC", disabled_by=None)
    base.update(kw); return SimpleNamespace(**base)


def _ok_probe():
    return patch.object(dr_mod, "probe_udp_48899", return_value=NetworkProbeResult(True, []))


async def test_goodwe_found_host_taken_but_password_never(make_hass):
    entry = SimpleNamespace(entry_id="g1", domain="goodwe", title="GoodWe",
                            data={"host": "192.168.1.20", "password": "secret"})
    hass = make_hass(devices=[_device()], entities=[_entity()], entries=[entry],
                     states={}, components={"recorder"})
    with patch.object(dr_mod, "probe_udp_48899", return_value=NetworkProbeResult(True, [])):
        rep = await DiscoveryRunner(hass, "v1", "2.0.0b1").async_run()
    assert rep["inverters"][0]["host"] == "192.168.1.20"
    assert "secret" not in str(rep)
    assert "9010KETU000W0001" not in str(rep)


async def test_registry_failure_recorded_not_raised(make_hass):
    hass = make_hass(devices=[], entities=[], entries=[], states={}, components=set())
    with patch.object(dr_mod.dr, "async_get", side_effect=RuntimeError("boom")), \
         patch.object(dr_mod, "probe_udp_48899", return_value=NetworkProbeResult(False, [], "x")):
        rep = await DiscoveryRunner(hass, "v1", "2.0.0b1").async_run()
    assert any("devices: RuntimeError: boom" in e for e in rep["errors"])
    assert any("history: recorder not loaded" in e for e in rep["errors"])


async def test_timeout_yields_report_with_timeout_error(make_hass, monkeypatch):
    hass = make_hass(devices=[], entities=[], entries=[], states={}, components=set())
    async def slow(*a, **k): await asyncio.sleep(5)
    monkeypatch.setattr(dr_mod, "DISCOVERY_TIMEOUT_S", 0.05)
    with patch.object(dr_mod, "probe_udp_48899", side_effect=slow):
        rep = await DiscoveryRunner(hass, "v1", "2.0.0b1").async_run()
    assert rep["errors"] == ["timeout"] and rep["inverters"] == []


async def test_signal_sent_after_run(make_hass):
    hass = make_hass(devices=[], entities=[], entries=[], states={}, components=set())
    with patch.object(dr_mod, "probe_udp_48899", return_value=NetworkProbeResult(True, [])), \
         patch.object(dr_mod, "async_dispatcher_send") as send:
        await DiscoveryRunner(hass, "v1", "2.0.0b1").async_run()
    send.assert_called_once_with(hass, "volcast_discovery_updated_v1")


# --- dodatkowe strażniki kontraktu ---

async def test_only_str_host_keys_read_from_entry_data(make_hass):
    entry = SimpleNamespace(entry_id="g1", domain="goodwe", title="GoodWe",
                            data={"host": 12345, "ip_address": "10.0.0.7",
                                  "username": "admin", "api_key": "k-secret", "port": 50123})
    hass = make_hass(devices=[_device()], entities=[_entity()], entries=[entry],
                     components={"recorder"})
    with _ok_probe():
        rep = await DiscoveryRunner(hass, "v1", "2.0.0b1").async_run()
    assert rep["inverters"][0]["host"] == "10.0.0.7"
    for leaked in ("admin", "k-secret", "50123", "12345"):
        assert leaked not in str(rep)


async def test_entity_snapshot_fields_and_meta(make_hass):
    ent = _entity(entity_id="sensor.pv_energy_total", unique_id="pv-total", device_class=None,
                  original_device_class="energy", unit_of_measurement="kWh",
                  disabled_by="user")
    entry = SimpleNamespace(entry_id="g1", domain="goodwe", title="GoodWe", data={})
    hass = make_hass(devices=[_device()], entities=[ent], entries=[entry],
                     components={"recorder"})
    with _ok_probe():
        rep = await DiscoveryRunner(hass, "v1", "2.0.0b1").async_run()
    e = rep["inverters"][0]["entities"][0]
    assert e["device_class"] == "energy" and e["unit"] == "kWh" and e["disabled"] is True
    assert rep["ha_version"] == "2026.9.0"
    assert rep["integration_version"] == "2.0.0b1"
    assert rep["generated_at"].startswith("2026-09-23T10:00:00")


async def test_states_read_only_for_classified_entities(make_hass):
    entry = SimpleNamespace(entry_id="g1", domain="goodwe", title="GoodWe", data={})
    other = _entity(entity_id="light.kitchen", platform="hue", unique_id="hue-1",
                    device_id="x", config_entry_id="h1", original_device_class=None,
                    unit_of_measurement=None)
    hass = make_hass(devices=[_device()], entities=[_entity(), other], entries=[entry],
                     states={"sensor.battery_state_of_charge": ("57", {"state_class": "measurement"}),
                             "light.kitchen": ("on", {})},
                     components={"recorder"})
    with _ok_probe():
        rep = await DiscoveryRunner(hass, "v1", "2.0.0b1").async_run()
    assert hass.states.requested == ["sensor.battery_state_of_charge"]
    assert rep["inverters"][0]["entities"][0]["state"] == "57"


async def test_history_queries_recorder_and_counts_days(make_hass):
    ent = _entity(entity_id="sensor.pv_energy_total", unique_id="pv-total",
                  original_device_class="energy", unit_of_measurement="kWh")
    entry = SimpleNamespace(entry_id="g1", domain="goodwe", title="GoodWe", data={})
    hass = make_hass(devices=[_device()], entities=[ent], entries=[entry],
                     components={"recorder"})
    day = 86400.0
    rows = {"sensor.pv_energy_total": [{"start": 1_790_000_000.0 + i * day} for i in range(3)]}
    stats = MagicMock(return_value=rows)
    with _ok_probe(), patch.object(dr_mod, "statistics_during_period", stats):
        rep = await DiscoveryRunner(hass, "v1", "2.0.0b1").async_run()
    args = stats.call_args.args
    assert args[0] is hass
    assert (dr_mod.dt_util.utcnow() - args[1]).days == 90
    assert args[2] is None and args[3] == {"sensor.pv_energy_total"}
    assert args[4] == "day" and args[5] is None and args[6] == {"sum", "state"}
    assert rep["energy_sensors"][0]["days_of_statistics"] == 3
    assert rep["errors"] == []


async def test_history_per_entity_error_recorded_not_raised(make_hass):
    ent = _entity(entity_id="sensor.pv_energy_total", unique_id="pv-total",
                  original_device_class="energy", unit_of_measurement="kWh")
    entry = SimpleNamespace(entry_id="g1", domain="goodwe", title="GoodWe", data={})
    hass = make_hass(devices=[_device()], entities=[ent], entries=[entry],
                     components={"recorder"}, time_zone="Not/AZone")
    rows = {"sensor.pv_energy_total": [{"start": 1_790_000_000.0}]}
    with _ok_probe(), patch.object(dr_mod, "statistics_during_period", MagicMock(return_value=rows)):
        rep = await DiscoveryRunner(hass, "v1", "2.0.0b1").async_run()
    assert any(e.startswith("history: ") and "sensor.pv_energy_total" in e for e in rep["errors"])
    assert rep["energy_sensors"][0]["days_of_statistics"] is None


async def test_recorder_query_failure_recorded(make_hass):
    ent = _entity(entity_id="sensor.pv_energy_total", unique_id="pv-total",
                  original_device_class="energy", unit_of_measurement="kWh")
    hass = make_hass(entities=[ent], components={"recorder"})
    with _ok_probe(), patch.object(dr_mod, "statistics_during_period",
                                   MagicMock(side_effect=OSError("db locked"))):
        rep = await DiscoveryRunner(hass, "v1", "2.0.0b1").async_run()
    assert any("history: OSError: db locked" in e for e in rep["errors"])


async def test_no_candidates_skips_recorder_query(make_hass):
    hass = make_hass(components={"recorder"})
    stats = MagicMock(return_value={})
    with _ok_probe(), patch.object(dr_mod, "statistics_during_period", stats):
        rep = await DiscoveryRunner(hass, "v1", "2.0.0b1").async_run()
    stats.assert_not_called()
    assert rep["errors"] == []


async def test_legacy_three_element_identifier_does_not_break_report(make_hass):
    entry = SimpleNamespace(entry_id="g1", domain="goodwe", title="GoodWe", data={})
    dev = _device(identifiers={("goodwe", "abc", "extra")}, serial_number=None)
    hass = make_hass(devices=[dev], entries=[entry], components={"recorder"})
    with _ok_probe():
        rep = await DiscoveryRunner(hass, "v1", "2.0.0b1").async_run()
    # wartość sklejona z nadmiarowych części (i tak maskowana jako potencjalny serial)
    assert rep["inverters"][0]["devices"][0]["identifiers"] == [["goodwe", "<SN>"]]
    assert rep["errors"] == []


async def test_concurrent_run_does_not_start_second_pass(make_hass):
    hass = make_hass(components={"recorder"})
    calls = 0
    gate = asyncio.Event()

    async def probe(*a, **k):
        nonlocal calls
        calls += 1
        await gate.wait()
        return NetworkProbeResult(True, [])

    runner = DiscoveryRunner(hass, "v1", "2.0.0b1")
    with patch.object(dr_mod, "probe_udp_48899", side_effect=probe):
        first = asyncio.create_task(runner.async_run())
        await asyncio.sleep(0)
        assert runner.running is True
        second = asyncio.create_task(runner.async_run())
        await asyncio.sleep(0)
        gate.set()
        rep1, rep2 = await asyncio.gather(first, second)
    assert calls == 1
    assert rep1 is rep2 and runner.report is rep1 and runner.running is False


async def test_concurrent_run_returns_previous_report_immediately(make_hass):
    hass = make_hass(components={"recorder"})
    runner = DiscoveryRunner(hass, "v1", "2.0.0b1")
    with _ok_probe():
        old = await runner.async_run()
    gate = asyncio.Event()

    async def probe(*a, **k):
        await gate.wait()
        return NetworkProbeResult(True, [])

    with patch.object(dr_mod, "probe_udp_48899", side_effect=probe):
        first = asyncio.create_task(runner.async_run())
        await asyncio.sleep(0)
        assert await runner.async_run() is old
        gate.set()
        new = await first
    assert new is not old and runner.report is new


async def test_unexpected_failure_still_returns_report(make_hass):
    hass = make_hass(components={"recorder"})
    real = dr_mod.build_report
    calls = {"n": 0}

    def fail_once(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("bad")
        return real(*a, **k)

    with _ok_probe(), patch.object(dr_mod, "build_report", side_effect=fail_once):
        rep = await DiscoveryRunner(hass, "v1", "2.0.0b1").async_run()
    assert any("runner: RuntimeError: bad" in e for e in rep["errors"])
    assert rep["inverters"] == []
