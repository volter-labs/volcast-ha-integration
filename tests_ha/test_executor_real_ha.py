"""Wykonawca na prawdziwym rdzeniu: kontekst zmian (nasze / właściciela / automatyzacji /
integracji), `last_reported`, licznik z wywołaniem zwrotnym-korutyną, magazyn w złym
formacie i zgłoszenia naprawy."""
from __future__ import annotations

import inspect
from datetime import timedelta
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import async_fire_time_changed

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import storage
from homeassistant.helpers.translation import async_get_translations
import homeassistant.util.dt as dt_util

from custom_components.volcast.const import DOMAIN
from custom_components.volcast.control.store import ControlStore
from custom_components.volcast.core.entity_map import EntityWrite
from custom_components.volcast.core.write_sequence import OK

from .conftest import ENTITY_OPTIONS, control_of, make_entry, setup_entry, store_state
from .inverter import async_setup_inverter

GATES = {"consent": True, "local_switch": True}


async def _paired_with_inverter(hass, hass_storage, state=None):
    inv = await async_setup_inverter(hass)
    store_state(hass_storage, "paired01", dict(state or GATES))
    entry = make_entry(hass, options=ENTITY_OPTIONS)
    await setup_entry(hass, entry)
    rt = control_of(hass, entry)
    assert rt is not None and set(rt.mapped) >= {"mode", "power_w", "soc", "export_limit_enabled"}
    return entry, rt, inv


async def _our_mode_write(hass, rt, option: str) -> None:
    ex = rt.executor
    out = await ex._writer.async_write(
        EntityWrite("mode", rt.mapped["mode"], "select", "select_option", {"option": option}))
    await hass.async_block_till_done()
    assert out == OK and hass.states.get(rt.mapped["mode"]).state == option
    # Tak jak po zapisie cyklu: pamięć zna naszą ostatnią wartość.
    ex._memory.last_written["mode"] = option


async def test_own_write_through_real_entity_is_ours_user_change_pauses(
        hass: HomeAssistant, network_down, hass_storage, hass_admin_user):
    entry, rt, _ = await _paired_with_inverter(hass, hass_storage)
    ex = rt.executor
    await _our_mode_write(hass, rt, "sell_power")
    assert not ex.paused and ex.foreign_changes == []

    # Właściciel zmienia tryb z interfejsu (kontekst z user_id).
    await hass.services.async_call("select", "select_option", {"entity_id": rt.mapped["mode"], "option": "auto"},
                                   blocking=True, context=Context(user_id=hass_admin_user.id))
    await hass.async_block_till_done()
    assert ex.paused and ex.foreign_changes[-1]["key"] == "mode"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "foreign_control_paired01")
    assert issue is not None and issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_placeholders == {"entity_id": rt.mapped["mode"]}


async def test_automation_change_carries_parent_id_and_pauses(hass: HomeAssistant, network_down, hass_storage):
    entry, rt, _ = await _paired_with_inverter(hass, hass_storage)
    await _our_mode_write(hass, rt, "sell_power")
    trigger = Context()
    await hass.services.async_call("select", "select_option", {"entity_id": rt.mapped["mode"], "option": "auto"},
                                   blocking=True, context=Context(parent_id=trigger.id))
    await hass.async_block_till_done()
    assert rt.executor.paused


async def test_integration_refresh_is_ours_inside_context_window_and_neutral_after(
        hass: HomeAssistant, network_down, hass_storage, freezer):
    entry, rt, inv = await _paired_with_inverter(hass, hass_storage)
    await _our_mode_write(hass, rt, "sell_power")
    ent = inv["mode"]
    # Odświeżenie stanu przez integrację tuż po zapisie niesie nasz kontekst.
    ent._attr_current_option = "sell_power"
    ent._attr_extra_state_attributes = {"refresh": 1}
    ent.async_write_ha_state()
    assert rt.executor._writer.is_ours(hass.states.get(rt.mapped["mode"]).context.id)
    # Po oknie kontekstu (5 s) zmiana z samego urządzenia nie ma aktora — nie jest przejęciem.
    freezer.tick(timedelta(seconds=6))
    ent._attr_current_option = "auto"
    ent.async_write_ha_state()
    await hass.async_block_till_done()
    ctx = hass.states.get(rt.mapped["mode"]).context
    assert not rt.executor._writer.is_ours(ctx.id) and ctx.user_id is None and ctx.parent_id is None
    assert not rt.executor.paused


async def test_last_reported_keeps_an_unchanged_soc_fresh(hass: HomeAssistant, network_down, hass_storage, freezer):
    entry, rt, inv = await _paired_with_inverter(hass, hass_storage)
    freezer.tick(timedelta(seconds=400))
    inv["soc"].async_write_ha_state()           # ta sama wartość, nowy odczyt
    st = hass.states.get(rt.mapped["soc"])
    assert (dt_util.utcnow() - st.last_updated).total_seconds() >= 400
    assert rt.executor._age(st, dt_util.utcnow()) < 1.0


async def test_executor_timer_runs_coroutine_callback(hass: HomeAssistant, network_down, hass_storage):
    entry, rt, _ = await _paired_with_inverter(hass, hass_storage)
    rt.executor.last_decision = None
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()
    assert rt.executor.last_decision is not None


async def test_future_store_version_disables_control_with_issue(hass: HomeAssistant, network_down, hass_storage):
    await async_setup_inverter(hass)
    hass_storage["volcast.control.paired01"] = {"version": 99, "minor_version": 1,
                                                "key": "volcast.control.paired01", "data": {"owned": True}}
    entry = make_entry(hass, options=ENTITY_OPTIONS)
    await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    rt = control_of(hass, entry)
    assert rt.executor.last_decision.reason == "store_unreadable"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "control_error_paired01")
    assert issue is not None and issue.severity is ir.IssueSeverity.WARNING
    # Wyłączony wykonawca nie nadpisuje pliku z przyszłej wersji.
    assert hass_storage["volcast.control.paired01"]["version"] == 99
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "control_error_paired01") is None


def _real_store_load():
    """Oryginalne `Store._async_load` sprzed atrapy magazynu harnessu (czyta plik z dysku)."""
    return inspect.getclosurevars(storage.Store._async_load.side_effect).nonlocals["orig_load"]


async def test_corrupt_store_file_starts_empty_and_ha_raises_its_own_issue(hass: HomeAssistant, tmp_path,
                                                                           monkeypatch):
    monkeypatch.setattr(hass.config, "config_dir", str(tmp_path))     # plik w katalogu testu
    # Lokalnie (nie monkeypatch): atrapa magazynu harnessu musi wrócić przed jego sprzątaniem.
    with patch.object(storage.Store, "_async_load", _real_store_load()):
        await _corrupt_store_case(hass)


async def _corrupt_store_case(hass: HomeAssistant) -> None:
    store = ControlStore(hass, "corrupt01")
    path = store._store.path

    def _write():
        import os
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"version": 1, "data": {"owned": tru')
    await hass.async_add_executor_job(_write)
    state = await store.async_load()
    assert state.owned is False and state.consent is None and state.snapshot == {}
    issues = [i for (d, i) in ir.async_get(hass).issues if d == "homeassistant"]
    assert any(i.startswith("storage_corruption_volcast.control.corrupt01") for i in issues)


async def test_issue_translation_keys_exist(hass: HomeAssistant):
    """Każdy klucz zgłoszenia użyty w kodzie ma tytuł i opis w tłumaczeniu."""
    tr = await async_get_translations(hass, "en", "issues", {DOMAIN})
    for key in ("production_tracking_available", "foreign_control", "control_error"):
        assert f"component.{DOMAIN}.issues.{key}.title" in tr, key
        assert f"component.{DOMAIN}.issues.{key}.description" in tr, key
    assert "{entity_id}" in tr[f"component.{DOMAIN}.issues.foreign_control.description"]
