"""Encje wykrywania: sensor z podsumowaniem i przycisk „Run discovery"."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.const import EntityCategory

from custom_components.volcast import discovery_entities
from custom_components.volcast.const import DOMAIN
from custom_components.volcast.discovery_entities import (
    VolcastDiscoveryButton,
    VolcastDiscoverySensor,
)

REPORT = {"schema": 1, "generated_at": "t", "inverters": [], "price_entities": [],
          "energy_sensors": [], "network": {"udp_48899": None}, "errors": []}


def test_sensor_pending_then_summary():
    runner = SimpleNamespace(report=None)
    s = VolcastDiscoverySensor(runner, "e1")
    assert s.native_value == "pending"
    runner.report = dict(REPORT)
    assert s.native_value.startswith("No inverter integration found")
    assert len(json.dumps(s.extra_state_attributes)) <= 4096


def test_sensor_attributes_empty_while_pending():
    s = VolcastDiscoverySensor(SimpleNamespace(report=None), "e1")
    assert s.extra_state_attributes == {}


def test_sensor_identity_and_recorder_exclusions():
    s = VolcastDiscoverySensor(SimpleNamespace(report=None), "e1")
    assert s._attr_unique_id == "e1_discovery"
    assert s._attr_entity_category is EntityCategory.DIAGNOSTIC
    assert s._attr_translation_key == "discovery"
    assert s._attr_has_entity_name is True
    assert s._attr_should_poll is False
    assert s._unrecorded_attributes == frozenset({
        "inverters", "price_platforms", "max_history_days", "loggers", "errors",
        "schema", "generated_at"})
    # ta sama karta urządzenia co encje prognozy
    assert s._attr_device_info["identifiers"] == {(DOMAIN, "e1")}


def test_sensor_attributes_cover_every_compact_key():
    """Każdy klucz atrybutów jest wyłączony z rekordera (raport bywa duży)."""
    s = VolcastDiscoverySensor(SimpleNamespace(report=dict(REPORT)), "e1")
    assert set(s.extra_state_attributes) <= s._unrecorded_attributes


@pytest.mark.asyncio
async def test_sensor_subscribes_to_entry_signal_and_writes_state():
    s = VolcastDiscoverySensor(SimpleNamespace(report=None), "e1")
    s.hass = object()
    s.async_on_remove = MagicMock()
    s.async_write_ha_state = MagicMock()
    unsub = MagicMock()
    with patch.object(discovery_entities, "async_dispatcher_connect",
                      return_value=unsub) as connect:
        await s.async_added_to_hass()
    hass, signal, handler = connect.call_args.args
    assert hass is s.hass
    assert signal == "volcast_discovery_updated_e1"
    s.async_on_remove.assert_called_once_with(unsub)
    handler()
    s.async_write_ha_state.assert_called_once()


def test_button_identity():
    b = VolcastDiscoveryButton(SimpleNamespace(), "e1")
    assert b._attr_unique_id == "e1_run_discovery"
    assert b._attr_translation_key == "run_discovery"
    assert b._attr_has_entity_name is True
    assert b._attr_device_info["identifiers"] == {(DOMAIN, "e1")}


@pytest.mark.asyncio
async def test_button_press_runs_discovery():
    runner = SimpleNamespace(async_run=AsyncMock(return_value=dict(REPORT)))
    await VolcastDiscoveryButton(runner, "e1").async_press()
    runner.async_run.assert_awaited_once()


@pytest.mark.parametrize("path", ["strings.json", "translations/en.json"])
def test_entity_names_translated(path):
    from pathlib import Path

    base = Path(__file__).parent.parent / "custom_components" / "volcast"
    entity = json.loads((base / path).read_text(encoding="utf-8"))["entity"]
    assert entity["sensor"]["discovery"]["name"] == "Installation discovery"
    assert entity["button"]["run_discovery"]["name"] == "Run discovery"
