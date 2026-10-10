"""Diagnostyka wpisu: raport wykrywania tak, klucz API nigdy."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.volcast.const import DOMAIN
from custom_components.volcast.diagnostics import async_get_config_entry_diagnostics

pytestmark = pytest.mark.asyncio

API_KEY = "vk_" + "b" * 64


@pytest.fixture
def hass_with_runner():
    from custom_components.volcast.discovery_runner import DiscoveryRunner

    def _factory(*, report, data=None):
        entry = SimpleNamespace(entry_id="e1", version=1,
                                data=data if data is not None else {"api_key": API_KEY},
                                options={"pv_energy_entity": "sensor.pv"})
        hass = SimpleNamespace(data={})
        runner = DiscoveryRunner(hass, "e1", "1.7.2")
        runner.report = report
        hass.data[DOMAIN] = {"e1": {"discovery": runner}}
        return hass, entry, runner

    return _factory


async def test_diagnostics_contains_report_but_never_api_key(hass_with_runner):
    hass, entry, runner = hass_with_runner(report={"schema": 1, "inverters": []})
    out = await async_get_config_entry_diagnostics(hass, entry)
    assert out["discovery"]["schema"] == 1
    assert "vk_" not in str(out) and "api_key" not in str(out)


async def test_diagnostics_entry_block(hass_with_runner):
    hass, entry, _ = hass_with_runner(report=None)
    out = await async_get_config_entry_diagnostics(hass, entry)
    assert out == {"entry": {"mode": "forecast", "version": "1.7.2", "paired": False,
                             "backend_host": None, "options_keys": ["pv_energy_entity"]},
                   "discovery": {"status": "pending"}, "control": None}


async def test_diagnostics_discovery_only_mode(hass_with_runner):
    hass, entry, _ = hass_with_runner(report=None, data={"mode": "discovery_only"})
    out = await async_get_config_entry_diagnostics(hass, entry)
    assert out["entry"]["mode"] == "discovery_only"


async def test_diagnostics_without_loaded_entry_is_pending():
    entry = SimpleNamespace(entry_id="e1", version=1, data={"api_key": API_KEY}, options={})
    out = await async_get_config_entry_diagnostics(SimpleNamespace(data={}), entry)
    assert out == {"entry": {"mode": "forecast", "version": "unknown", "paired": False,
                             "backend_host": None, "options_keys": []},
                   "discovery": {"status": "pending"}, "control": None}
    assert API_KEY not in str(out)


def _control_hass(telemetry):
    ex = SimpleNamespace(exec_summary=lambda: {}, tou_preview=None, foreign_changes=[])
    rt = SimpleNamespace(executor=ex, choice=SimpleNamespace(profile=SimpleNamespace(id="goodwe-et"),
                                                             integration_domain="goodwe"),
                         mapped={}, telemetry=telemetry)
    entry = SimpleNamespace(entry_id="e1", version=1, data={"api_key": API_KEY}, options={})
    hass = SimpleNamespace(data={DOMAIN: {"e1": {"control": rt, "discovery": None}},
                                 "device_registry": SimpleNamespace(devices={})})
    return hass, entry


async def test_diagnostics_control_carries_readings_source():
    report = {"readings_source": {"soc": "entity", "pv_power_w": "none"}, "readings_dropped": ["pv_power_w"]}
    hass, entry = _control_hass(SimpleNamespace(readings_report=lambda: report))
    out = await async_get_config_entry_diagnostics(hass, entry)
    assert out["control"]["readings_source"] == {"soc": "entity", "pv_power_w": "none"}
    assert out["control"]["readings_dropped"] == ["pv_power_w"]


async def test_diagnostics_readings_report_error_keeps_control_section():
    def broken():
        raise RuntimeError("boom")
    hass, entry = _control_hass(SimpleNamespace(readings_report=broken))
    out = await async_get_config_entry_diagnostics(hass, entry)
    assert out["control"]["profile"] == "goodwe-et"
    assert out["control"]["readings_source"] == {"error": "RuntimeError"}
