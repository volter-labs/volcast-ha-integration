"""Porządki trybu bezpośredniego: budżet zapisany po powrocie, sól instalacji jedna i trwała, powody
blokad, kolejka datagramów z limitem, możliwości TOU w izolacji."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.volcast.control import store as store_mod
from custom_components.volcast.control.store import ControlStore, async_installation_salt
from custom_components.volcast.core.control.caps import direct_capabilities
from custom_components.volcast.core.profile import load_builtin
from custom_components.volcast.core.transports import goodwe_udp as udp_mod
from tests.control.test_executor import plan
from tests.control.test_executor_direct import (  # noqa: F401 — fixture `issues`
    GW_V, MODE, Harness, _owned_sell, gw_raw_word, gw_target, issues)
from custom_components.volcast.core.slot import parse_schedule


@pytest.mark.asyncio
async def test_restore_frames_are_persisted_in_the_budget(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        n = len(h.ex._state.nvm_log)
        await h.ex.async_set_consent(False)
        await h.cycle()
        assert not h.ex.owned and gw_raw_word(goodwe_bank, MODE) == 1
        assert len(h.ex._state.nvm_log) > n                     # ramki powrotu też w zapisanym budżecie
        assert len(h.ex._state.nvm_log) == len(h.ex._memory.budget.to_list())
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_budget_exhaustion_survives_restart(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        budget = h.ex._memory.budget
        for _ in range(budget.per_key):
            budget.note("power_w", h.utc().timestamp())
        await h.cycle()                                          # zapis budżetu razem ze stanem
        assert budget.exhausted({"power_w"}, h.utc().timestamp()) == {"power_w"}
        await h.restart()
        assert h.ex._memory.budget.exhausted({"power_w"}, h.utc().timestamp()) == {"power_w"}
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_budget_restore_blocked_by_pause_says_paused(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        budget = h.ex._memory.budget
        for key in ("mode", "power_w"):
            for _ in range(budget.per_key):
                budget.note(key, h.utc().timestamp())
        new = plan(power=3000, sid="p2")
        await h.ex.async_on_plan(new, parse_schedule(new))
        h.ex._memory.paused_until = h.clock() + 3600.0
        h.clock.advance(61.0)
        await h.conn.async_poll()
        await h.ex.async_tick()
        assert h.ex.last_decision.reason != "restore_not_owned"
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_restore_before_connection_started_is_quiet(make_hass, goodwe_udp_sim, goodwe_bank, issues, caplog):
    from custom_components.volcast.control.store import ControlStore as CS
    store = CS(make_hass(), "e1")
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank, store=store)
    await h.close()
    h2 = Harness(make_hass, GW_V, h.target, store=store)
    await h2.ex.async_start()
    try:
        await h2.ex.async_set_consent(False)
        with caplog.at_level("WARNING"):
            await h2.ex.async_tick()                              # połączenie jeszcze nie wystartowało
        assert h2.ex.last_decision.reason == "connecting"
        assert "not confirmed" not in caplog.text
    finally:
        await h2.close()


def test_installation_salt_persists_and_is_created_once(monkeypatch):
    backing = {}

    class Store:
        def __init__(self, hass, version, key):
            self.key = key

        async def async_load(self):
            await asyncio.sleep(0)
            return backing.get(self.key)

        async def async_save(self, data):
            await asyncio.sleep(0)
            backing[self.key] = data

    monkeypatch.setattr(store_mod, "Store", Store)

    async def go():
        hass = SimpleNamespace(data={})
        a, b = await asyncio.gather(async_installation_salt(hass), async_installation_salt(hass))
        assert a == b                                              # równoległe pierwsze wywołania — jedna sól
        again = await async_installation_salt(SimpleNamespace(data={}))  # „restart”: pusta pamięć, ten sam magazyn
        return a, again
    first, second = asyncio.run(go())
    assert first == second and len(first) == 16


@pytest.mark.asyncio
async def test_udp_datagram_queue_is_bounded():
    proto = udp_mod._Datagrams()
    for i in range(udp_mod.MAX_QUEUED_DATAGRAMS + 50):
        proto.datagram_received(bytes([i & 0xFF]), None)
    assert len(proto.queue) == udp_mod.MAX_QUEUED_DATAGRAMS


def test_time_window_capabilities_follow_the_program_block_only():
    deye = load_builtin("deye-sg")
    assert direct_capabilities(deye, {"tou": True, "soc_min": False}, ())["set_soc_floor"] is True
    assert not any(direct_capabilities(deye, {"tou": True}, ("tou",)).values())


@pytest.mark.asyncio
async def test_safety_off_cap_raises_repair_issue(make_hass, rtu_tcp_sim, deye_bank, issues):
    from custom_components.volcast.core.control.tou_cycle import SAFETY_OFF_CAP
    from tests.control.test_executor_direct import TOU_RAW, _deye
    h = await _deye(make_hass, rtu_tcp_sim).start(raw=TOU_RAW)
    try:
        await h.ex.async_tick()
        assert h.ex.owned
        h.ex._memory.tou_safety_offs.extend([h.clock()] * SAFETY_OFF_CAP)
        budget = h.ex._memory.budget
        for _ in range(budget.per_key):                              # przepisanie wstrzymane budżetem
            budget.note("tou_enable", h.utc().timestamp())
        raw = {**TOU_RAW, "schedule_id": "safer"}
        raw["slots"] = [{**s, "power_w": 1000} if s.get("mode") == "charge" else s for s in raw["slots"]]
        await h.ex.async_on_plan(raw, parse_schedule(raw))
        await h.cycle(400.0)
        assert "tou_safety_off_cap" in h.ex.last_decision.notes
        assert "tou_safety_off_cap" in issues.keys()
    finally:
        await h.close()
