"""Połączenie bezpośrednie: kolizja tylko z lokalnym hostem równym naszemu, ponowny start po odmowie."""
from __future__ import annotations

import asyncio

import pytest

from custom_components.volcast.const import DOMAIN

from tests.control.test_direct_connection import _conn, _entry, _target, hass, issues  # noqa: F401


async def _until(pred, timeout=2.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred():
        if loop.time() > end:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_cloud_integration_without_host_never_blocks(hass, goodwe_udp_sim, issues):
    h = hass(_entry("growatt_server", "g", data={"username": "u", "plant_id": "1"}))
    conn = _conn(h, _target(goodwe_udp_sim))
    try:
        await conn.async_start()
        assert conn.refused() is None and conn.identity == "confirmed" and conn.static_conflicts == ()
    finally:
        await conn.async_stop()


@pytest.mark.asyncio
async def test_refused_start_retries_with_back_off_until_the_conflict_is_gone(hass, goodwe_udp_sim, issues):
    other = _entry("goodwe", "g", data={"host": goodwe_udp_sim.host})
    h = hass(other)
    conn = _conn(h, _target(goodwe_udp_sim))
    conn.retry_min_s, conn.retry_max_s = 0.01, 0.04
    delays: list[float] = []

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 4:
            h.config_entries._entries.remove(other)          # właściciel wyłączył drugą integrację
        await asyncio.sleep(0)
    conn._sleep = sleep
    try:
        await conn.async_start()
        assert conn.refused() == "direct_conflict:goodwe"
        await _until(lambda: conn.refused() is None and conn.identity == "confirmed")
        assert delays == [0.01, 0.02, 0.04, 0.04]
        await _until(lambda: conn.reading is not None)             # po starcie od razu odczyt
    finally:
        await conn.async_stop()


@pytest.mark.asyncio
async def test_bad_target_is_not_retried(hass, issues):
    conn = _conn(hass(), {"profile_id": "goodwe-et", "transport": "goodwe_udp", "host": "8.8.8.8", "port": 8899,
                          "unit_id": 247})
    await conn.async_start()
    assert conn.refused() == "bad_target" and conn._retry_task is None
    await conn.async_stop()


@pytest.mark.asyncio
async def test_stop_cancels_a_pending_retry(hass, goodwe_udp_sim, issues):
    h = hass(_entry("goodwe", "g", data={"host": goodwe_udp_sim.host}))
    conn = _conn(h, _target(goodwe_udp_sim))
    await conn.async_start()
    task = conn._retry_task
    assert task is not None and not task.done()
    await conn.async_stop()
    assert task.done() and goodwe_udp_sim.requests == 0


# ── nieoczekiwany wyjątek przy starcie połączenia ─────────────────────────


def _failing_client(monkeypatch, fails: list[int]):
    from custom_components.volcast.control import direct as direct_mod
    real = direct_mod.RegisterClient

    def client(*a, **k):
        if fails[0] > 0:
            fails[0] -= 1
            raise RuntimeError("boom")
        return real(*a, **k)
    monkeypatch.setattr(direct_mod, "RegisterClient", client)


@pytest.mark.asyncio
async def test_start_exception_is_a_refusal_with_repair_issue_and_bounded_retry(hass, goodwe_udp_sim, issues,
                                                                                monkeypatch):
    from custom_components.volcast.control import direct as direct_mod
    fails = [10**6]
    _failing_client(monkeypatch, fails)
    h = hass()
    conn = _conn(h, _target(goodwe_udp_sim))
    conn.retry_min_s = conn.retry_max_s = 0.001
    try:
        await conn.async_start()                             # nie rzuca
        assert conn.refused() == "start_failed" and conn.client is None
        assert h.data[DOMAIN]["direct_hosts"] == {}          # host zwolniony
        assert ("direct_start_failed_self", "direct_start_failed") in issues.created
        await _until(lambda: conn._start_failures >= direct_mod.START_RETRIES)
        await asyncio.sleep(0.05)
        assert conn._start_failures == direct_mod.START_RETRIES                 # ograniczona liczba prób
        assert conn._retry_task is None or conn._retry_task.done()
        assert conn.refused() == "start_failed"
    finally:
        await conn.async_stop()
    assert "direct_start_failed_self" in issues.deleted


@pytest.mark.asyncio
async def test_start_exception_then_success_clears_the_issue(hass, goodwe_udp_sim, issues, monkeypatch):
    fails = [1]
    _failing_client(monkeypatch, fails)
    conn = _conn(hass(), _target(goodwe_udp_sim))
    conn.retry_min_s = conn.retry_max_s = 0.001
    try:
        await conn.async_start()
        assert conn.refused() == "start_failed"
        await _until(lambda: conn.refused() is None and conn.identity == "confirmed")
        assert "direct_start_failed_self" in issues.deleted
    finally:
        await conn.async_stop()


@pytest.mark.asyncio
async def test_restore_after_start_exception_is_not_silent_connecting(make_hass, goodwe_udp_sim, goodwe_bank,
                                                                     monkeypatch, caplog):
    from custom_components.volcast.control.store import ControlStore
    from tests.control.test_executor_direct import GW_V, Harness, _owned_sell
    from custom_components.volcast.control import direct as direct_mod
    from custom_components.volcast.control import executor as ex_mod
    created = []
    for mod in (ex_mod, direct_mod):
        monkeypatch.setattr(mod.ir, "async_create_issue",
                            lambda hass, domain, issue_id, **kw: created.append(kw.get("translation_key")))
        monkeypatch.setattr(mod.ir, "async_delete_issue", lambda *a, **k: None)
    store = ControlStore(make_hass(), "e1")
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank, store=store)
    await h.close()
    _failing_client(monkeypatch, [10**6])
    h2 = Harness(make_hass, GW_V, h.target, store=store)
    h2.conn.retry_min_s = h2.conn.retry_max_s = 60.0
    await h2.conn.async_start()
    await h2.ex.async_start()
    try:
        await h2.ex.async_set_consent(False)
        with caplog.at_level("WARNING"):
            await h2.ex.async_tick()
        assert h2.ex.last_decision.reason == "direct_refused" and h2.ex.owned
        assert "start_failed" in caplog.text and "direct_start_failed" in created
    finally:
        await h2.close()
