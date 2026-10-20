"""Jedno źródło produkcji na prawdziwym rdzeniu HA: telemetria sterowania z mocą PV wyłącza lokalny
tracker (zgłoszenie o nieużywanych czujnikach), telemetria bez mocy PV zostawia tracker; zgłoszenie
i timery znikają przy rozładunku i usunięciu wpisu."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from custom_components.volcast.const import DOMAIN

from .conftest import ENTITY_OPTIONS, add_goodwe, control_of, make_entry, setup_entry, store_state, volcast_timers

PV = {"pv_energy_entity": "sensor.house_pv_energy", "pv_power_entity": "sensor.house_pv_power"}
GATES = {"consent": True, "local_switch": True}


def _pv_sensors(hass) -> None:
    hass.states.async_set(PV["pv_energy_entity"], "1234.5", {"unit_of_measurement": "kWh"})
    hass.states.async_set(PV["pv_power_entity"], "800", {"unit_of_measurement": "W"})


def _issue(hass, issue_id: str):
    return ir.async_get(hass).async_get_issue(DOMAIN, issue_id)


def _mapping_issues(hass) -> list[str]:
    return [iid for (dom, iid) in ir.async_get(hass).issues if dom == DOMAIN
            and iid.startswith("production_mapping_unused_")]


def _production_timers(hass) -> list[str]:
    return [t for t in volcast_timers(hass) if ".production." in t or ".reconciler." in t]


def _assert_telemetry_source(hass, entry) -> None:
    """Telemetria niesie produkcję: bez trackera i reconcilera, zgłoszenie z nazwami czujników."""
    data = hass.data[DOMAIN][entry.entry_id]
    assert control_of(hass, entry).telemetry_reports_production is True
    assert data["tracker"] is None
    assert data.get("reconciler") is None
    assert _production_timers(hass) == []
    issue = _issue(hass, f"production_mapping_unused_{entry.entry_id}")
    assert issue is not None and issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_key == "production_mapping_unused"
    assert issue.translation_placeholders == {"entities": "sensor.house_pv_energy, sensor.house_pv_power"}
    assert _issue(hass, "production_tracking_available") is None


def _assert_tracker_source(hass, entry) -> None:
    """Telemetria bez mocy PV: tracker ze zmapowanych czujników działa, bez zgłoszeń źródła."""
    data = hass.data[DOMAIN][entry.entry_id]
    rt = control_of(hass, entry)
    assert rt is not None and rt.telemetry_reports_production is False
    tracker = data["tracker"]
    assert tracker is not None
    assert tracker._unsub_state is not None and tracker._unsub_timer is not None      # wystartował
    assert data.get("reconciler") is not None                                         # jest licznik energii
    assert any(t.endswith("VolcastProductionTracker._async_check_flush") for t in volcast_timers(hass))
    assert _mapping_issues(hass) == []
    assert _issue(hass, "production_tracking_available") is None


async def test_direct_mode_with_pv_power_and_mapped_sensors_skips_tracker(hass: HomeAssistant, network_down,
                                                                         hass_storage, goodwe_sim):
    from .test_direct_smoke_real_ha import _setup as _direct_setup
    from .test_direct_smoke_real_ha import _settle, _target, _verified_goodwe

    _pv_sensors(hass)
    options = {"control_mode": "direct", "direct_target": _target(goodwe_sim, _verified_goodwe()), **PV}
    entry, rt = await _direct_setup(hass, hass_storage, options)
    await _settle(hass, rt)
    _assert_telemetry_source(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert _mapping_issues(hass) == []


async def test_entities_mode_with_pv_entity_and_mapped_sensors_skips_tracker(hass: HomeAssistant, network_down,
                                                                            hass_storage):
    add_goodwe(hass)
    _pv_sensors(hass)
    store_state(hass_storage, "paired01", dict(GATES))
    entry = make_entry(hass, options={**ENTITY_OPTIONS, **PV})
    await setup_entry(hass, entry)
    assert "pv_power_w" in control_of(hass, entry).mapped
    _assert_telemetry_source(hass, entry)


async def test_paired_without_detected_inverter_keeps_tracker(hass: HomeAssistant, network_down, hass_storage):
    """Konto sparowane, w HA brak integracji falownika: telemetria bez mocy PV — tracker zostaje."""
    _pv_sensors(hass)
    store_state(hass_storage, "paired01", dict(GATES))
    entry = make_entry(hass, options=dict(PV))
    await setup_entry(hass, entry)
    assert "pv_power_w" not in control_of(hass, entry).mapped
    _assert_tracker_source(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert _production_timers(hass) == []


async def test_inverter_entities_without_pv_power_keep_tracker(hass: HomeAssistant, network_down, hass_storage):
    """Integracja falownika bez encji mocy PV (tryb encji): telemetria nie niesie produkcji."""
    ents = add_goodwe(hass)
    er.async_get(hass).async_remove(ents["pv_power_w"])
    hass.states.async_remove(ents["pv_power_w"])
    _pv_sensors(hass)
    store_state(hass_storage, "paired01", dict(GATES))
    entry = make_entry(hass, options={**ENTITY_OPTIONS, **PV})
    await setup_entry(hass, entry)
    rt = control_of(hass, entry)
    assert rt.mapped.get("mode") == ents["mode"] and "pv_power_w" not in rt.mapped
    _assert_tracker_source(hass, entry)


async def test_unload_then_remove_leaves_no_mapping_issue_or_timers(hass: HomeAssistant, network_down,
                                                                   hass_storage):
    add_goodwe(hass)
    _pv_sensors(hass)
    store_state(hass_storage, "paired01", dict(GATES))
    entry = make_entry(hass, options={**ENTITY_OPTIONS, **PV})
    await setup_entry(hass, entry)
    assert _mapping_issues(hass) == [f"production_mapping_unused_{entry.entry_id}"]
    assert volcast_timers(hass)                                   # wpis ma działające timery

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert _mapping_issues(hass) == []
    assert volcast_timers(hass) == []
    assert not hass.data.get(DOMAIN)

    assert (await hass.config_entries.async_remove(entry.entry_id))["require_restart"] is False
    await hass.async_block_till_done()
    assert hass.config_entries.async_get_entry(entry.entry_id) is None
    assert _mapping_issues(hass) == []
    assert volcast_timers(hass) == []


async def test_remove_of_loaded_entry_leaves_no_mapping_issue_or_timers(hass: HomeAssistant, network_down,
                                                                       hass_storage):
    add_goodwe(hass)
    _pv_sensors(hass)
    store_state(hass_storage, "paired01", dict(GATES))
    entry = make_entry(hass, options={**ENTITY_OPTIONS, **PV})
    await setup_entry(hass, entry)
    assert _mapping_issues(hass)
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert _mapping_issues(hass) == []
    assert volcast_timers(hass) == []
    assert not hass.data.get(DOMAIN)
