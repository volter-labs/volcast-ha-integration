"""Tryb bezpośredni na prawdziwym rdzeniu HA z symulatorem GoodWe (UDP, pętla zwrotna):
złożenie wpisu, odpytywanie na pętli, próba bez zapisów, zapis z profilem `verified`, powrót po
cofnięciu zgody, przeładowanie, rozładowanie i odmowa przy integracji falownika na tym samym hoście."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import timedelta

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

import homeassistant.util.dt as dt_util
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.volcast.const import DOMAIN
from custom_components.volcast.control import direct_search as ds
from custom_components.volcast.control import runtime as rt_mod
from custom_components.volcast.core.modbus.identity import device_fingerprint
from custom_components.volcast.core.profile import load_builtin, profile_from_dict
from custom_components.volcast.core.registers import RegisterImage

from .conftest import SALT, control_of, make_entry, make_poll_due, seed_salt, setup_entry, store_state

MODE, POWER = 47511, 47512


def _thaw(v):
    if isinstance(v, Mapping):
        return {k: _thaw(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_thaw(x) for x in v]
    return v


def _verified_goodwe():
    raw = _thaw(load_builtin("goodwe-et").raw)
    raw["status"] = raw["modbus"]["status"] = "verified"
    return profile_from_dict(raw)


def _target(sim, profile) -> dict:
    from tests.sim.fixtures import goodwe_words
    fp = device_fingerprint(SALT, profile, RegisterImage(goodwe_words()))
    return {"profile_id": "goodwe-et", "transport": "goodwe_udp", "host": "127.0.0.1", "port": sim.port,
            "unit_id": 247, "device_fp": fp, "unreadable": ["soc_max"],
            # moc znamionowa z identyfikacji (35001), jak zapisuje ją sonda — pułap nastawy sprzedaży
            "rated_power_w": 8000.0,
            "capabilities": {k: True for k in profile.modbus.probe_keys}}


def _plan(mode="discharge", **slot) -> dict:
    now = dt_util.utcnow()
    iso = lambda t: t.isoformat().replace("+00:00", "Z")  # noqa: E731
    body = {"mode": mode, "price_pln_kwh": 0.8, **slot}
    if mode == "discharge":
        body.update(discharge_purpose="sell", power_w=2000)
    return {"schedule_id": "smoke", "control_enabled": True, "fallback": {"mode": "self_consume", "soc_reserve": 10},
            "slots": [{"from": iso(now - timedelta(minutes=30)), "to": iso(now + timedelta(hours=2)), **body}]}


async def _settle(hass, rt) -> None:
    """Start połączenia w tle (kolizje, tożsamość), pierwszy odczyt i cykl."""
    for _ in range(100):
        await asyncio.sleep(0.02)                          # start w tle czeka na odpowiedzi symulatora
        await hass.async_block_till_done()
        if rt.direct.reading is not None and rt.executor.last_decision is not None \
                and rt.executor.last_decision.reason != "identity":
            return
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    d = rt.executor.last_decision
    raise AssertionError(f"direct connection did not start: refused={rt.direct.refused()} "
                         f"identity={rt.direct.identity} reading={rt.direct.reading is not None} "
                         f"decision={d and (d.status, d.reason)}")


async def _setup(hass, hass_storage, options, *, state=None, profiles=None, monkeypatch=None):
    seed_salt(hass_storage)
    store_state(hass_storage, "paired01", state or {"consent": True, "local_switch": True, "plan_raw": _plan()})
    if profiles is not None:
        monkeypatch.setattr(rt_mod.ds, "load_profiles", lambda: list(profiles))
    entry = make_entry(hass, options=options)
    await setup_entry(hass, entry)
    rt = control_of(hass, entry)
    assert rt is not None and rt.direct is not None
    return entry, rt


async def test_real_ha_trial_sensors_polling_and_dry_run(hass: HomeAssistant, network_down, hass_storage,
                                                        goodwe_sim):
    target = _target(goodwe_sim, load_builtin("goodwe-et"))
    entry, rt = await _setup(hass, hass_storage, {"direct_trial": True, "direct_target": target})
    await _settle(hass, rt)
    reg = er.async_get(hass)
    soc = reg.async_get_entity_id("sensor", DOMAIN, "paired01_control_direct_soc")
    assert soc and float(hass.states.get(soc).state) == 83.0
    assert rt.executor.last_decision.status == "dry_run" and rt.executor.last_decision.summary()["would_write"]
    before = goodwe_sim.requests
    make_poll_due(rt.direct)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
    for _ in range(20):
        await asyncio.sleep(0.02)
        await hass.async_block_till_done()
    assert goodwe_sim.requests > before                     # odpytywanie z zegara HA na pętli zdarzeń
    assert goodwe_sim.bank.writes == []                     # próba: zero zapisów
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_real_ha_direct_write_restore_reload_unload(hass: HomeAssistant, network_down, hass_storage,
                                                         goodwe_sim, monkeypatch):
    gw = _verified_goodwe()
    options = {"control_mode": "direct", "direct_target": _target(goodwe_sim, gw)}
    entry, rt = await _setup(hass, hass_storage, options, profiles=[gw], monkeypatch=monkeypatch)
    await _settle(hass, rt)
    bank = goodwe_sim.bank
    assert rt.executor.last_decision.status == "write", rt.executor.last_decision.summary()
    # sprzedaż: nastawa eksportu = moc baterii 2000 W + PV 828 W − dom 364 W (odczyt symulatora)
    assert bank.read(MODE, 1) == [10] and bank.read(POWER, 1) == [2000 + 828 - 364] and rt.executor.owned

    # przeładowanie: bez powrotu do migawki, ale bez trybu wymuszonego na czas przerwy (sam tryb
    # neutralny); własność zostaje, nowy wykonawca przejmuje sterowanie; jedno połączenie na host
    n = len(bank.writes)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    rt = control_of(hass, entry)
    await _settle(hass, rt)
    assert bank.writes[n:] == [(MODE, 1), (MODE, 10)]
    assert bank.read(MODE, 1) == [10] and rt.executor.owned
    assert list(hass.data[DOMAIN]["direct_hosts"]) == ["127.0.0.1"]

    # cofnięcie zgody → powrót do trybu bazowego tym samym połączeniem
    # (bez przesuwania zegara HA: `async_fire_time_changed` odpala też terminy oczekiwania transportu)
    await rt.executor.async_set_consent(False)
    await rt.executor.async_tick()
    await hass.async_block_till_done()
    assert bank.read(MODE, 1) == [1] and not rt.executor.owned

    # rozładowanie: host zwolniony, symulator nie dostaje już ramek
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED and hass.data[DOMAIN].get("direct_hosts") == {}
    before = goodwe_sim.requests
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=5))
    await hass.async_block_till_done()
    assert goodwe_sim.requests == before


async def test_real_ha_stop_event_sets_neutral_mode_and_keeps_ownership(hass: HomeAssistant, network_down,
                                                                        hass_storage, goodwe_sim, monkeypatch):
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP
    gw = _verified_goodwe()
    options = {"control_mode": "direct", "direct_target": _target(goodwe_sim, gw)}
    entry, rt = await _setup(hass, hass_storage, options, profiles=[gw], monkeypatch=monkeypatch)
    await _settle(hass, rt)
    bank = goodwe_sim.bank
    assert bank.read(MODE, 1) == [10] and rt.executor.owned
    # Etap 1 zatrzymania HA: nasłuch zapisuje tryb neutralny, a HA czeka na to zadanie.
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert bank.read(MODE, 1) == [1] and rt.executor.owned
    n = len(bank.writes)
    await rt.executor.async_tick()                       # po zatrzymaniu żaden cykl nie wraca do planu
    assert len(bank.writes) == n
    assert await hass.config_entries.async_unload(entry.entry_id)   # zdjęcie nasłuchu po wystrzale: bez błędu
    await hass.async_block_till_done()


async def test_real_ha_conflicting_goodwe_entry_refuses(hass: HomeAssistant, network_down, hass_storage,
                                                        goodwe_sim):
    MockConfigEntry(domain="goodwe", data={"host": "127.0.0.1"}, entry_id="gw").add_to_hass(hass)
    target = _target(goodwe_sim, load_builtin("goodwe-et"))
    entry, rt = await _setup(hass, hass_storage, {"direct_trial": True, "direct_target": target})
    for _ in range(10):
        await asyncio.sleep(0.02)
        await hass.async_block_till_done()
    assert rt.direct.refused() == "direct_conflict:goodwe" and goodwe_sim.requests == 0
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_real_ha_options_search_manual_then_trial(hass: HomeAssistant, network_down, hass_storage,
                                                        goodwe_sim, monkeypatch):
    from custom_components.volcast.core.discovery.network import NetworkProbeResult

    async def no_replies(*a, **k):
        return NetworkProbeResult(sent=True)
    monkeypatch.setattr(ds, "probe_udp_48899", no_replies)
    seed_salt(hass_storage)
    store_state(hass_storage, "paired01", {"consent": True, "local_switch": True})
    entry = make_entry(hass)
    await setup_entry(hass, entry)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"next_step_id": "details"})
    assert flow["step_id"] == "details"
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"direct_search": True})
    assert flow["type"] == "progress"
    await hass.async_block_till_done()
    flow = await hass.config_entries.options.async_configure(flow["flow_id"])
    assert flow["type"] == "form" and flow["step_id"] == "direct_pick"
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"candidate": "manual"})
    assert flow["step_id"] == "direct_manual"
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"host": "127.0.0.1", "transport": "goodwe_udp", "port": goodwe_sim.port, "unit_id": 247})
    assert flow["type"] == "create_entry", flow.get("errors")
    await hass.async_block_till_done()
    target = entry.options["direct_target"]
    assert target["device_fp"] and target["port"] == goodwe_sim.port and "control_mode" not in entry.options
    # Szczegóły → połączenie próbne (profil `draft`)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"next_step_id": "details"})
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"direct_trial": True})
    assert flow["type"] == "create_entry"
    await hass.async_block_till_done()
    rt = control_of(hass, entry)
    await _settle(hass, rt)
    soc = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, "paired01_control_direct_soc")
    assert soc and float(hass.states.get(soc).state) == 83.0 and goodwe_sim.bank.writes == []
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
