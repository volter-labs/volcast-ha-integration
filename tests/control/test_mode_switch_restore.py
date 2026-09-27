"""Zmiana sposobu sterowania przy własności, gdy powrót do trybu bazowego się nie udał.

Opcje odmawiają zapisu (nic nie zapisane), a setup — gdy magazyn i tak ma własność z innego sposobu
sterowania — najpierw oddaje falownik przez TAMTEN sposób i trzyma rekord, dopóki powrót nie dojdzie.
Wszystko na symulatorach z pętli zwrotnej.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import tests.test_options_direct as od
from custom_components.volcast.const import DOMAIN
from custom_components.volcast.control import executor as ex_mod
from custom_components.volcast.control import runtime as rt_mod
from custom_components.volcast.control.device_io import DirectIO, EntityIO
from custom_components.volcast.control.store import ControlStore
from tests.control.ha_fakes import GOODWE_ENTITIES as E
from tests.control.test_executor_direct import (MODE, _loopback_connection, _noop, _owned_sell, _salt,  # noqa: F401
                                                gw_raw_word, issues)
from tests.control.test_runtime import BACKEND, FakeCloud

OFF = {"control_mode": None}
ENTITIES = {"control_mode": "entities", "profile_id": "goodwe-et", "inverter_domain": "goodwe"}


def _runtime(h):
    return rt_mod.ControlRuntime(h.ex, None, SimpleNamespace(async_stop=_noop), None, h.ex._choice, {}, 8000.0,
                                 direct=h.conn)


def _flow(h, *, options=None):
    return od.flow(options=options if options is not None else dict(h.options), runtime=_runtime(h))


# ── opcje: odmowa, gdy powrót przed przeładowaniem się nie udał ───────────


@pytest.mark.asyncio
async def test_direct_to_off_refused_when_restore_fails(make_hass, goodwe_udp_sim, goodwe_bank, sim_faults, issues):
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        sim_faults.drop_next = 10**6                         # falownik poza zasięgiem przy zapisie opcji
        r = await _flow(h).async_step_control_off()
        assert r == {"type": "abort", "reason": "restore_failed"}
        assert h.ex.owned and gw_raw_word(goodwe_bank, MODE) == 10
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_direct_to_entities_refused_when_restore_fails(make_hass, goodwe_udp_sim, goodwe_bank, sim_faults,
                                                             issues):
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        sim_faults.drop_next = 10**6
        r = await _flow(h)._finish({**h.options, **ENTITIES})
        assert r == {"type": "abort", "reason": "restore_failed"}
        assert h.ex.owned and gw_raw_word(goodwe_bank, MODE) == 10
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_direct_to_off_saved_when_restore_succeeds(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        r = await _flow(h).async_step_control_off()
        assert r["type"] == "create_entry" and r["data"].get("control_mode") is None
        assert not h.ex.owned and gw_raw_word(goodwe_bank, MODE) == 1
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_new_target_refused_with_form_error_when_restore_fails(make_hass, goodwe_udp_sim, goodwe_bank,
                                                                    sim_faults, issues):
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        f = _flow(h)
        f._reports = [od.report()]                            # inny adres niż obecny cel
        f._profiles_cache = [od.GW_V]
        sim_faults.drop_next = 10**6
        r = await f.async_step_direct_pick({"candidate": "0"})
        assert (r["type"], r["step_id"], r["errors"]) == ("form", "direct_pick", {"base": "restore_failed"})
        assert h.ex.owned and gw_raw_word(goodwe_bank, MODE) == 10
    finally:
        await h.close()


def test_entities_to_direct_refused_when_restore_fails(monkeypatch):
    from tests.test_options_flow import _owned_executor
    h, ex = _owned_executor(monkeypatch)

    async def bad_write(_w):
        raise RuntimeError("down")
    ex._writer.async_write = bad_write
    old = dict(ENTITIES)
    runtime = SimpleNamespace(executor=ex, choice=None, mapped={})
    f = od.flow(options=old, runtime=runtime)
    new = {**old, "control_mode": "direct", "direct_target": {"profile_id": "goodwe-et", "host": "192.168.1.9"}}
    r = asyncio.run(f._finish(new))
    assert r == {"type": "abort", "reason": "restore_failed"} and ex.owned


@pytest.mark.parametrize("old,new,allowed", [
    (ENTITIES, OFF | {"profile_id": "goodwe-et", "inverter_domain": "goodwe"}, True),   # ten sam wykonawca ponawia
    (ENTITIES, {**ENTITIES, "inverter_domain": "other"}, False),
    ({"control_mode": "direct", "direct_target": {"host": "192.168.1.9"}},
     {"control_mode": None, "direct_target": {"host": "192.168.1.9"}}, False),
])
def test_control_change_allowed_only_when_the_next_executor_keeps_the_record(old, new, allowed):
    class Owned:
        owned = True

        async def async_restore_now(self):
            return None
    assert asyncio.run(rt_mod.async_control_change_allowed(SimpleNamespace(executor=Owned()), old, new)) is allowed
    # bez własności albo bez zmiany sterowania — zawsze wolno
    assert asyncio.run(rt_mod.async_control_change_allowed(SimpleNamespace(executor=None), old, new)) is True
    assert asyncio.run(rt_mod.async_control_change_allowed(SimpleNamespace(executor=Owned()), old, old)) is True


# ── setup: własność z innego sposobu sterowania wraca przez tamten sposób ──


def _setup(monkeypatch, store, options, *, entry_id="e1"):
    from tests.control.test_runtime import _setup_hass
    from tests.setup_harness import FakeEntry
    monkeypatch.setattr(rt_mod, "VolcastCloud", FakeCloud)
    monkeypatch.setattr(rt_mod, "async_get_clientsession", lambda hass: None)
    monkeypatch.setattr(rt_mod, "ControlStore", lambda hass, eid: store)
    monkeypatch.setattr(rt_mod, "async_installation_salt", _salt)
    monkeypatch.setattr(rt_mod, "DirectConnection", _loopback_connection)
    hass = _setup_hass()
    reloads: list[str] = []
    hass.config_entries.async_schedule_reload = reloads.append
    entry = FakeEntry(data={"api_key": "vk_" + "a" * 64, "backend": BACKEND}, options=options, entry_id=entry_id)
    return hass, entry, reloads


@pytest.mark.asyncio
@pytest.mark.parametrize("new", [OFF, ENTITIES], ids=["off", "entities"])
async def test_setup_returns_direct_ownership_through_direct_first(make_hass, goodwe_udp_sim, goodwe_bank,
                                                                   sim_faults, issues, monkeypatch, new):
    from tests.setup_harness import drain
    store = ControlStore(make_hass(), "e1")
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank, store=store)
    await h.close()
    sim_faults.drop_next = 10**6                             # powrót przed przeładowaniem się nie udał
    hass, entry, reloads = _setup(monkeypatch, store, {**h.options, **new})
    rt = await rt_mod.async_setup_control(hass, entry, report=lambda: None)
    try:
        await drain(hass)
        await rt.executor.async_tick()
        assert isinstance(rt.executor.io, DirectIO) and rt.executor.owned
        assert (await store.async_load()).owner.get("mode") == "direct"
        assert gw_raw_word(goodwe_bank, MODE) == 10 and reloads == []
        sim_faults.drop_next = 0                             # falownik znowu osiągalny
        await rt.executor.async_tick()
        assert not rt.executor.owned and gw_raw_word(goodwe_bank, MODE) == 1
        assert reloads == ["e1"]                             # dopiero teraz nowy sposób sterowania
    finally:
        await rt_mod.async_unload_control(hass, rt)


@pytest.mark.asyncio
async def test_setup_returns_entity_ownership_through_entities_first(monkeypatch, goodwe_udp_sim, issues):
    from tests.control.test_executor import make, ready
    from tests.control.test_executor_direct import gw_target
    from tests.setup_harness import drain
    gh, ex = make(monkeypatch=monkeypatch)

    async def own():
        await ready(ex)
        await ex.async_tick()
    await own()
    assert ex.owned and gh.states.get(E["mode"]).state == "sell_power"
    await ex.async_stop()
    store = ex._store
    monkeypatch.setattr(rt_mod, "map_entities", lambda hass, c: dict(E) if c and c.integration_domain else {})
    hass, entry, reloads = _setup(monkeypatch, store, {**ENTITIES, "control_mode": "direct",
                                                       "direct_target": gw_target(goodwe_udp_sim)})
    hass.states, hass.services = gh.states, gh.services
    gh.services.fail[E["mode"]] = RuntimeError("unavailable")
    rt = await rt_mod.async_setup_control(hass, entry, report=lambda: None)
    try:
        await drain(hass)
        await rt.executor.async_tick()
        assert isinstance(rt.executor.io, EntityIO) and rt.executor.owned and rt.direct is None
        assert gh.states.get(E["mode"]).state == "sell_power" and reloads == []
        gh.services.fail.clear()
        await rt.executor.async_tick()
        assert not rt.executor.owned and gh.states.get(E["mode"]).state != "sell_power"
        assert reloads == ["e1"]
    finally:
        await rt_mod.async_unload_control(hass, rt)


@pytest.mark.asyncio
async def test_setup_without_a_way_back_drops_the_record_with_a_repair_issue(make_hass, goodwe_udp_sim, goodwe_bank,
                                                                            issues, monkeypatch):
    from tests.setup_harness import drain
    store = ControlStore(make_hass(), "e1")
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank, store=store)
    await h.close()
    options = {k: v for k, v in h.options.items() if k != "direct_target"} | OFF      # celu już nie ma
    hass, entry, reloads = _setup(monkeypatch, store, options)
    rt = await rt_mod.async_setup_control(hass, entry, report=lambda: None)
    try:
        await drain(hass)
        assert not rt.executor.owned and "control_record_dropped" in issues.keys()
    finally:
        await rt_mod.async_unload_control(hass, rt)


def test_dropped_record_issue_key_has_strings():
    import json
    from pathlib import Path
    root = Path(ex_mod.__file__).parents[1]
    for name in ("strings.json", "translations/en.json"):
        data = json.loads((root / name).read_text())
        assert "control_record_dropped" in data["issues"]
        assert "restore_failed" in data["options"]["abort"] and "restore_failed" in data["options"]["error"]
    assert DOMAIN == "volcast"
