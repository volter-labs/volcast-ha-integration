"""Warstwa HA wykrywania instalacji: migawki rejestrów, rekorder, sygnał."""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.volcast import discovery_runner as dr_mod
from custom_components.volcast.discovery_runner import DiscoveryRunner
from custom_components.volcast.core.discovery.network import NetworkProbeResult
from custom_components.volcast.core.discovery.report import summarize

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
    assert summarize(rep) == "Discovery failed: timeout"


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


async def test_legacy_three_element_identifier_serial_masked(make_hass):
    entry = SimpleNamespace(entry_id="g1", domain="goodwe", title="GoodWe", data={})
    dev = _device(identifiers={("goodwe", "9010KETU000W0777", "extra")}, serial_number=None)
    hass = make_hass(devices=[dev], entries=[entry], components={"recorder"})
    with _ok_probe():
        rep = await DiscoveryRunner(hass, "v1", "2.0.0b1").async_run()
    # każdy element po domenie to osobna para; wartość identyfikatora maskowana polowo
    # niezależnie od cyfr, więc "extra" też staje się "<SN>"
    assert rep["inverters"][0]["devices"][0]["identifiers"] == [
        ["goodwe", "<SN>"], ["goodwe", "<SN>"]]
    assert "9010KETU000W0777" not in str(rep)
    assert rep["errors"] == []


async def test_legacy_identifier_serial_masked_in_entity_id(make_hass):
    # wartość = drugi element krotki (sam serial), więc maskuje się też w entity_id
    entry = SimpleNamespace(entry_id="g1", domain="goodwe", title="GoodWe", data={})
    dev = _device(identifiers={("goodwe", "9010KETU000W0777", "extra")}, serial_number=None)
    ent = _entity(entity_id="sensor.goodwe_9010ketu000w0777_power",
                  unique_id="power-1", original_device_class="power", unit_of_measurement="W")
    hass = make_hass(devices=[dev], entities=[ent], entries=[entry], components={"recorder"})
    with _ok_probe():
        rep = await DiscoveryRunner(hass, "v1", "2.0.0b1").async_run()
    assert rep["inverters"][0]["entities"][0]["entity_id"] == "sensor.goodwe_<SN>_power"
    assert "9010ketu000w0777" not in str(rep).lower()


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
    assert summarize(rep) == "Discovery failed — see diagnostics"


async def test_ignored_config_entry_is_not_an_inverter(make_hass):
    ignored = SimpleNamespace(entry_id="g9", domain="goodwe", title="GoodWe 9010KETU000W0777",
                              source="ignore", disabled_by=None, data={})
    hass = make_hass(entries=[ignored], components={"recorder"})
    with _ok_probe():
        rep = await DiscoveryRunner(hass, "v1", "2.0.0b1").async_run()
    assert rep["inverters"] == []
    assert "9010KETU000W0777" not in str(rep)


async def test_disabled_config_entry_is_not_an_inverter(make_hass):
    disabled = SimpleNamespace(entry_id="g8", domain="goodwe", title="GoodWe 9010KETU000W0888",
                               source="user", disabled_by="user", data={"host": "10.0.0.8"})
    active = SimpleNamespace(entry_id="g1", domain="goodwe", title="GoodWe",
                             source="user", disabled_by=None, data={"host": "10.0.0.1"})
    hass = make_hass(entries=[disabled, active], components={"recorder"})
    with _ok_probe():
        rep = await DiscoveryRunner(hass, "v1", "2.0.0b1").async_run()
    assert [i["host"] for i in rep["inverters"]] == ["10.0.0.1"]
    assert "9010KETU000W0888" not in str(rep) and "10.0.0.8" not in str(rep)


async def test_disabled_devices_skipped(make_hass):
    # wyłączony wpis zostawia urządzenia w rejestrze (disabled_by=config_entry) —
    # nie mogą wrócić do raportu ścieżką producenta
    disabled = SimpleNamespace(entry_id="g8", domain="goodwe", title="GoodWe",
                               source="user", disabled_by="user", data={})
    dev = _device(id="d8", config_entries={"g8"}, disabled_by="config_entry")
    hass = make_hass(devices=[dev], entries=[disabled], components={"recorder"})
    with _ok_probe():
        rep = await DiscoveryRunner(hass, "v1", "2.0.0b1").async_run()
    assert rep["inverters"] == []


async def test_disabled_device_dropped_active_device_kept(make_hass):
    entry = SimpleNamespace(entry_id="g1", domain="goodwe", title="GoodWe", data={})
    active = _device(id="d1", disabled_by=None)
    off = _device(id="d2", model="GW5K-DT", disabled_by="user")
    hass = make_hass(devices=[active, off], entries=[entry], components={"recorder"})
    with _ok_probe():
        rep = await DiscoveryRunner(hass, "v1", "2.0.0b1").async_run()
    assert [d["model"] for d in rep["inverters"][0]["devices"]] == ["GW10K-ET"]


async def test_entities_of_disabled_device_dropped_serial_never_leaks(make_hass):
    # drugi falownik wyłączony przez użytkownika na AKTYWNYM wpisie: jego encje
    # (disabled_by="device") nie mogą dołączyć do znaleziska przez config_entry_id
    entry = SimpleNamespace(entry_id="g1", domain="goodwe", title="GoodWe", data={})
    active = _device(id="d1")
    off = _device(id="d2", serial_number="9010KETU000W0002",
                  identifiers={("goodwe", "9010KETU000W0002")}, disabled_by="user")
    ent_off = _entity(entity_id="sensor.goodwe_9010ketu000w0002_power",
                      unique_id="9010KETU000W0002-power", device_id="d2",
                      original_device_class="power", unit_of_measurement="W",
                      disabled_by="device")
    hass = make_hass(devices=[active, off], entities=[_entity(), ent_off], entries=[entry],
                     components={"recorder"})
    with _ok_probe():
        rep = await DiscoveryRunner(hass, "v1", "2.0.0b1").async_run()
    ids = [e["entity_id"] for e in rep["inverters"][0]["entities"]]
    assert ids == ["sensor.battery_state_of_charge"]
    assert "9010ketu000w0002" not in str(rep).lower()


async def test_entity_of_disabled_device_dropped_even_if_entity_itself_enabled(make_hass):
    entry = SimpleNamespace(entry_id="g1", domain="goodwe", title="GoodWe", data={})
    off = _device(id="d2", serial_number="9010KETU000W0002",
                  identifiers={("goodwe", "9010KETU000W0002")}, disabled_by="user")
    ent_off = _entity(entity_id="sensor.goodwe_9010ketu000w0002_energy",
                      unique_id="9010KETU000W0002-energy", device_id="d2",
                      original_device_class="energy", unit_of_measurement="kWh")
    hass = make_hass(devices=[off], entities=[ent_off], entries=[entry], components={"recorder"})
    with _ok_probe():
        rep = await DiscoveryRunner(hass, "v1", "2.0.0b1").async_run()
    assert rep["inverters"][0]["entities"] == [] and rep["energy_sensors"] == []
    assert "9010ketu000w0002" not in str(rep).lower()


async def test_legacy_identifier_every_extra_element_is_masking_candidate(make_hass):
    # (domena, host/model, serial) — serial na 3. pozycji też musi być maskowany
    entry = SimpleNamespace(entry_id="g1", domain="goodwe", title="GoodWe", data={})
    dev = _device(identifiers={("goodwe", "gw-host", "9010KETU000W0555")}, serial_number=None)
    ent = _entity(entity_id="sensor.goodwe_9010ketu000w0555_power", unique_id="p-1",
                  original_device_class="power", unit_of_measurement="W")
    hass = make_hass(devices=[dev], entities=[ent], entries=[entry], components={"recorder"})
    with _ok_probe():
        rep = await DiscoveryRunner(hass, "v1", "2.0.0b1").async_run()
    # wartość identyfikatora maskowana polowo niezależnie od cyfr,
    # więc "gw-host" też staje się "<SN>"
    assert sorted(rep["inverters"][0]["devices"][0]["identifiers"]) == [
        ["goodwe", "<SN>"], ["goodwe", "<SN>"]]
    assert rep["inverters"][0]["entities"][0]["entity_id"] == "sensor.goodwe_<SN>_power"
    assert "9010ketu000w0555" not in str(rep).lower()


async def test_serial_of_non_inverter_energy_device_masked(make_hass):
    # Bramka spoza listy falowników (Enphase Envoy): serial w entity_id czujnika energii.
    sn = "122012345678"
    entry = SimpleNamespace(entry_id="en1", domain="enphase_envoy", title="Envoy", data={})
    dev = _device(id="env", manufacturer="Enphase", model="Envoy", name="Envoy",
                  serial_number=sn, identifiers={("enphase_envoy", sn)}, config_entries={"en1"})
    ent = _entity(entity_id=f"sensor.envoy_{sn}_lifetime_energy_production",
                  platform="enphase_envoy", unique_id=f"{sn}_lifetime_energy_production",
                  device_id="env", config_entry_id="en1", original_device_class="energy",
                  unit_of_measurement="kWh")
    hass = make_hass(devices=[dev], entities=[ent], entries=[entry],
                     states={ent.entity_id: ("1234", {"state_class": "total_increasing"})})
    with _ok_probe():
        rep = await DiscoveryRunner(hass, "v1", "2.0.0b1").async_run()
    assert rep["inverters"] == []
    assert [s["entity_id"] for s in rep["energy_sensors"]] == [
        "sensor.envoy_<SN>_lifetime_energy_production"]
    assert sn not in str(rep)


async def test_entity_capabilities_reach_snapshot_and_charger_is_found(make_hass):
    # rejestr encji niesie options/min/max/step; runner klasyfikuje bez stanów
    dev = _device(id="c1", manufacturer="Tuya", model=None, name="EV charger",
                  serial_number=None, identifiers={("tuya_local", "DEV1")},
                  config_entries={"t1"})
    common = dict(platform="tuya_local", device_id="c1", config_entry_id="t1",
                  original_device_class=None, translation_key=None)
    status = _entity(entity_id="sensor.ev_charger_status", unique_id="u-status",
                     device_class="enum", unit_of_measurement=None, original_name="Status",
                     capabilities={"options": ["available", "plugged_in", "charging"]}, **common)
    current = _entity(entity_id="number.ev_charger_charge_current", unique_id="u-current",
                      device_class="current", unit_of_measurement="A",
                      original_name="Charge current",
                      capabilities={"min": 6.0, "max": 16.0, "step": 1.0, "cap_marker": "zz9"},
                      **common)
    entry = SimpleNamespace(entry_id="t1", domain="tuya_local", title="EV charger", data={})
    hass = make_hass(devices=[dev], entities=[status, current], entries=[entry],
                     components={"recorder"})
    with _ok_probe(), patch.object(dr_mod, "build_report", wraps=dr_mod.build_report) as br:
        rep = await DiscoveryRunner(hass, "v1", "2.0.0b1").async_run()
    chargers = br.call_args.kwargs["classification"].chargers
    assert [f.device_id for f in chargers] == ["c1"]
    sp = chargers[0].roles["setpoint"]
    assert (sp.min, sp.max, sp.step) == (6.0, 16.0, 1.0)
    assert "plugged_in" in chargers[0].roles["status"].options
    # raport do chmury serializuje pola jawnie: bez capabilities i bez sekcji ładowarek
    assert "cap_marker" not in str(rep) and "capabilities" not in str(rep)
    assert "chargers" not in rep
