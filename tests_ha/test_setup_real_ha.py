"""Cykl życia wpisu na prawdziwym rdzeniu: start bez sieci, wyłączenie/włączenie, usunięcie,
karta i panel."""
from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
import homeassistant.util.dt as dt_util

from custom_components.volcast.const import DOMAIN
from custom_components.volcast.control import history_import as hi
from custom_components.volcast.frontend import CARD_PATH, PANEL_PATH

from .conftest import API_KEY, BACKEND, ENTITY_OPTIONS, OWNED, InverterServices, add_goodwe
from .conftest import control_of as _control
from .conftest import make_entry as _entry
from .conftest import setup_entry as _setup
from .conftest import store_state as _store


async def test_paired_entry_loads_with_network_down(hass: HomeAssistant, network_down, hass_storage):
    """Bez sieci przy starcie wpis sparowany się ładuje (bez ConfigEntryNotReady) i ma sterowanie."""
    _store(hass_storage, "paired01", {"consent": True, "local_switch": True})
    entry = _entry(hass)
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    rt = _control(hass, entry)
    assert rt is not None and rt.executor.consent is True and rt.executor.local_switch is True
    reg = er.async_get(hass)
    assert reg.async_get_entity_id("switch", DOMAIN, "paired01_control_switch")
    plan = reg.async_get_entity_id("sensor", DOMAIN, "paired01_control_plan")
    assert plan and hass.states.get(plan) is not None
    # Prognoza bez danych — niedostępna, ale nie blokuje wpisu.
    today = reg.async_get_entity_id("sensor", DOMAIN, "paired01_energy_today")
    assert today and hass.states.get(today).state == "unavailable"
    assert network_down.call_count >= 1
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED and DOMAIN not in hass.data or not hass.data[DOMAIN]


async def test_production_tracking_issue_created_with_real_severity(hass: HomeAssistant, network_down):
    entry = _entry(hass)
    await _setup(hass, entry)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "production_tracking_available")
    assert issue is not None and issue.severity is ir.IssueSeverity.WARNING


async def test_reload_does_not_restore_but_disable_does_and_enable_sets_up_again(
        hass: HomeAssistant, network_down, hass_storage):
    ents = add_goodwe(hass, mode="sell_power", export_limit_w="0")
    svc = InverterServices(hass)
    _store(hass_storage, "paired01", dict(OWNED))
    entry = _entry(hass, options=ENTITY_OPTIONS)
    await _setup(hass, entry)
    rt = _control(hass, entry)
    assert rt is not None and rt.mapped["mode"] == ents["mode"] and rt.executor.owned
    assert svc.calls == []          # profil w wersji próbnej: bez zapisów, pierwszy cykl nic nie oddaje

    # Zwykłe przeładowanie nie oddaje falownika.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert svc.calls == [] and _control(hass, entry).executor.owned

    # Wyłączenie wpisu przez właściciela oddaje falownik w tryb bazowy przed rozładunkiem.
    assert await hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    written = svc.written()
    assert written[ents["mode"]] == "auto" and float(written[ents["export_limit_w"]]) == 4000.0
    assert hass.states.get(ents["mode"]).state == "auto"
    saved = hass_storage["volcast.control.paired01"]["data"]
    assert saved["owned"] is False and saved["snapshot"] == {}

    # Ponowne włączenie składa wpis od nowa — bez własności nic nie przywraca.
    svc.calls.clear()
    assert await hass.config_entries.async_set_disabled_by(entry.entry_id, None)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    rt = _control(hass, entry)
    assert rt is not None and not rt.executor.owned and svc.calls == []


async def test_removal_restores_owned_inverter_and_wipes_store(hass: HomeAssistant, network_down, hass_storage):
    ents = add_goodwe(hass, mode="sell_power", export_limit_w="0")
    svc = InverterServices(hass)
    _store(hass_storage, "paired01", dict(OWNED))
    entry = _entry(hass, options=ENTITY_OPTIONS)
    await _setup(hass, entry)
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert svc.written()[ents["mode"]] == "auto"
    assert "volcast.control.paired01" not in hass_storage


async def test_card_static_path_panel_and_storage_resource(hass: HomeAssistant, network_down, hass_client):
    assert await async_setup_component(hass, "lovelace", {})
    entry = _entry(hass)
    await _setup(hass, entry)
    client = await hass_client()
    resp = await client.get(CARD_PATH)
    assert resp.status == 200 and "customElements" in await resp.text()
    from homeassistant.components.frontend import DATA_PANELS
    panel = hass.data[DATA_PANELS][PANEL_PATH]
    plan = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, "paired01_control_plan")
    # Panel czyta `panel.config.entity` (karta w panelu), moduł to adres karty z wersją.
    assert panel.config["entity"] == plan and panel.config["_panel_custom"]["name"] == "volcast-panel"
    assert panel.config["_panel_custom"]["module_url"].split("?")[0] == CARD_PATH
    resources = hass.data["lovelace"].resources
    await resources.async_get_info()
    urls = [r["url"] for r in resources.async_items()]
    assert sum(u.split("?")[0] == CARD_PATH for u in urls) == 1
    # Przeładowanie nie dubluje zasobu ani ścieżki (flaga), panel wraca.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert sum(r["url"].split("?")[0] == CARD_PATH for r in resources.async_items()) == 1
    assert PANEL_PATH in hass.data[DATA_PANELS]
    # Rozładunek zdejmuje panel.
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert PANEL_PATH not in hass.data[DATA_PANELS]


async def test_yaml_mode_dashboard_still_gets_panel(hass: HomeAssistant, network_down):
    assert await async_setup_component(hass, "lovelace", {"lovelace": {"mode": "yaml"}})
    entry = _entry(hass)
    await _setup(hass, entry)
    from homeassistant.components.frontend import DATA_PANELS
    assert entry.state is ConfigEntryState.LOADED and PANEL_PATH in hass.data[DATA_PANELS]


async def test_without_recorder_import_is_skipped_quietly(hass: HomeAssistant):
    cloud, ex = _Cloud(), _Exec()
    assert await hi.async_import_history_once(hass, cloud, ex, load_entity="sensor.house_energy",
                                              now_utc=dt_util.utcnow()) is None
    assert cloud.parts == []


class _Cloud:
    parts: list = []

    async def async_import_history(self, hours):
        raise AssertionError("no recorder — nothing to send")


class _Exec:
    history_imported_at = None


async def test_setup_runs_discovery_and_reports_no_deprecated_ha_usage(
        hass: HomeAssistant, network_down, hass_storage, no_udp_probe, caplog):
    """Wykrywanie rusza przy starcie (sonda UDP podmieniona) i nic nie woła wycofywanych API HA."""
    add_goodwe(hass)
    _store(hass_storage, "paired01", {"consent": True, "local_switch": True})
    entry = _entry(hass, options=ENTITY_OPTIONS)
    await _setup(hass, entry)
    assert no_udp_probe.await_count >= 1
    reg = er.async_get(hass)
    disc = hass.states.get(reg.async_get_entity_id("sensor", DOMAIN, "paired01_discovery"))
    assert disc is not None and disc.state not in ("unknown", "unavailable")
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    # `report_usage` HA opisuje integrację własną jako "custom integration 'volcast'".
    assert "custom integration 'volcast'" not in caplog.text


async def test_telemetry_and_schedule_timers_fire_without_network(hass: HomeAssistant, network_down, hass_storage):
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    _store(hass_storage, "paired01", {"consent": True, "local_switch": True})
    entry = _entry(hass)
    await _setup(hass, entry)
    before = {str(c[1]) for c in network_down.mock_calls}
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=301))
    await hass.async_block_till_done()
    urls = [str(c[1]) for c in network_down.mock_calls]
    # Pobranie planu (co 5 min) poszło mimo braku sieci; wpis dalej działa.
    assert any(u.startswith(BACKEND["schedule"]) for u in urls)
    assert entry.state is ConfigEntryState.LOADED and _control(hass, entry) is not None
    assert len(urls) > len(before)


async def test_diagnostics_on_real_registries_hide_secrets(hass: HomeAssistant, network_down, hass_storage):
    from custom_components.volcast.diagnostics import async_get_config_entry_diagnostics

    add_goodwe(hass)
    _store(hass_storage, "paired01", {"consent": True, "local_switch": True})
    entry = _entry(hass, options=ENTITY_OPTIONS)
    await _setup(hass, entry)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = str(diag)
    assert API_KEY not in text and "TESTSN0001" not in text
    assert diag.get("control") is not None


async def test_card_resource_from_older_version_is_updated_not_duplicated(hass: HomeAssistant, network_down):
    assert await async_setup_component(hass, "lovelace", {})
    resources = hass.data["lovelace"].resources
    await resources.async_get_info()
    await resources.async_create_item({"res_type": "module", "url": f"{CARD_PATH}?v=0.0.1"})
    entry = _entry(hass)
    await _setup(hass, entry)
    urls = [r["url"] for r in resources.async_items() if r["url"].split("?")[0] == CARD_PATH]
    assert len(urls) == 1 and urls[0] != f"{CARD_PATH}?v=0.0.1"
