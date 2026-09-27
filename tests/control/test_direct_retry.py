"""Połączenie bezpośrednie: kolizja tylko z lokalnym hostem równym naszemu, ponowny start po odmowie."""
from __future__ import annotations

import asyncio

import pytest

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
