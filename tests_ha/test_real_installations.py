"""Sterowanie na zestawach encji z prawdziwych instalacji (raport wykrywania, nie symulator).

Symulator `add_goodwe` ma encje wybrane pod testy wykonawcy; prawdziwa integracja HA wystawia
inny zestaw (np. `select.*_inverter_operation_mode` zamiast trybu EMS). Każdy fixture w
`fixtures/` to raport wykrywania jednej instalacji z zamaskowanymi numerami seryjnymi.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .conftest import SN, control_of, make_entry, setup_entry

FIXTURES = Path(__file__).parent / "fixtures"
# Wpis sprzed 2.0 (tylko opcje prognozy), zaktualizowany parowaniem w miejscu.
LEGACY_FORECAST_OPTIONS = {"pv_energy_entity": "sensor.goodwe_today_s_pv_generation",
                           "pv_power_entity": "sensor.goodwe_pv_power",
                           "battery_soc_entity": "sensor.goodwe_battery_state_of_charge",
                           "battery_charge_power_entity": ""}


def add_installation(hass: HomeAssistant, name: str, domain: str) -> dict:
    """Wpis integracji falownika, urządzenie i encje (rejestr + stany) z fixture'a."""
    data = json.loads((FIXTURES / f"{name}.json").read_text())
    inv_entry = MockConfigEntry(domain=domain, title=data["device"]["manufacturer"])
    inv_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=inv_entry.entry_id, identifiers={(domain, SN)},
        manufacturer=data["device"]["manufacturer"], model=data["device"]["model"],
        sw_version=data["device"]["sw_version"])
    reg = er.async_get(hass)
    for e in data["entities"]:
        ent_domain, object_id = e["entity_id"].split(".", 1)
        ent = reg.async_get_or_create(
            ent_domain, domain, e["unique_id"].replace("<SN>", SN), suggested_object_id=object_id,
            config_entry=inv_entry, device_id=device.id, original_name=e["original_name"],
            original_device_class=e["device_class"], unit_of_measurement=e["unit"],
            disabled_by=er.RegistryEntryDisabler.USER if e["disabled"] else None)
        if e["disabled"]:
            continue
        attrs = {"unit_of_measurement": e["unit"], "device_class": e["device_class"],
                 "state_class": e["state_class"], "options": e["options"], "min": e["min"],
                 "max": e["max"], "step": e["step"], "friendly_name": e["original_name"]}
        hass.states.async_set(ent.entity_id, e["state"], {k: v for k, v in attrs.items() if v is not None})
    return data


@pytest.mark.parametrize("fixture", ["goodwe_core_et"])
async def test_paired_legacy_entry_sets_up_control_on_real_entity_set(
        hass: HomeAssistant, network_down, hass_storage, caplog, fixture):
    """Wpis 1.x po parowaniu, prawdziwe encje GoodWe: sterowanie się składa, tryb pracy jest śledzony."""
    add_installation(hass, fixture, "goodwe")
    entry = make_entry(hass, options=LEGACY_FORECAST_OPTIONS)
    await setup_entry(hass, entry)
    assert "could not be set up" not in caplog.text
    rt = control_of(hass, entry)
    assert rt is not None
    # Encja trybu pracy znaleziona i podpięta: zmiana stanu dociera do trackera.
    hass.states.async_set("select.goodwe_inverter_operation_mode", "general",
                          {"options": ["general", "off_grid", "backup", "eco", "peak_shaving"]})
    await hass.async_block_till_done()
    assert rt.work_mode.state == "general"
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    # `report_usage` HA: żadnych wycofywanych API na prawdziwym rejestrze urządzeń.
    assert "custom integration 'volcast'" not in caplog.text


async def test_failed_control_setup_logs_the_traceback(
        hass: HomeAssistant, network_down, hass_storage, caplog, monkeypatch):
    """Błąd złożenia sterowania trafia do logu z pełnym śladem — sam typ wyjątku nie wystarcza."""
    async def _boom(*_a, **_kw):
        raise AttributeError("boom")
    monkeypatch.setattr("custom_components.volcast.async_setup_control", _boom)
    entry = make_entry(hass)
    with caplog.at_level(logging.ERROR):
        await setup_entry(hass, entry)
    recs = [r for r in caplog.records if "could not be set up" in r.getMessage()]
    assert recs and recs[0].exc_info is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
