"""Połączenie bezpośrednie w HA: cykl życia, odpytywanie, kolizje, tożsamość urządzenia."""
import asyncio
import logging
import types
from dataclasses import replace

import pytest

from custom_components.volcast.const import DIRECT_SLOW_POLL_S, DOMAIN
from custom_components.volcast.control import direct as direct_mod
from custom_components.volcast.control.direct import (
    DirectConnection, async_entry_snaps, target_fingerprint)
from custom_components.volcast.core.modbus.identity import device_fingerprint
from custom_components.volcast.core.profile import load_builtin
from custom_components.volcast.core.registers import RegisterImage
from custom_components.volcast.core.transports.factory import make_transport
from tests.core.transports.helpers import FakeClock
from tests.sim.fixtures import goodwe_words

SALT = bytes(range(16))
GOODWE = load_builtin("goodwe-et")


def _entry(domain, entry_id, *, data=None, options=None, disabled_by=None):
    return types.SimpleNamespace(domain=domain, entry_id=entry_id, data=data or {}, options=options or {},
                                 disabled_by=disabled_by)


def _target(sim, kind="goodwe_udp", **over):
    fp = device_fingerprint(SALT, GOODWE, RegisterImage(goodwe_words()))
    return {"profile_id": "goodwe-et", "transport": kind, "host": sim.host, "port": sim.port,
            "unit_id": 247, "device_fp": fp, **over}


def _factory(timeout_s=0.1):
    def make(cfg, **kw):
        kw.pop("clock", None)
        return make_transport(replace(cfg, timeout_s=timeout_s, gap_s=0.0, read_tries=2, backoff_min_s=0.01,
                                      backoff_max_s=0.02), **kw)
    return make


@pytest.fixture
def hass(make_hass):
    def build(*entries):
        h = make_hass(entries=[_entry(DOMAIN, "self"), *entries])
        h.async_create_task = lambda coro, *a, **k: asyncio.get_event_loop().create_task(coro)
        return h
    return build


def _conn(hass, target, *, clock=None, resolve=None, **kw):
    entry = _entry(DOMAIN, "self")
    return DirectConnection(hass, entry, GOODWE, target, trial=False, salt=SALT, transport_factory=_factory(),
                            allow_loopback=True, clock=clock or FakeClock(), resolve=resolve,
                            unreadable=kw.pop("unreadable", {"soc_max"}), **kw)


@pytest.fixture
def issues(monkeypatch):
    created, deleted = [], []
    monkeypatch.setattr(direct_mod.ir, "async_create_issue",
                        lambda hass, domain, issue_id, **kw: created.append((issue_id, kw.get("translation_key"))))
    monkeypatch.setattr(direct_mod.ir, "async_delete_issue", lambda hass, domain, issue_id: deleted.append(issue_id))
    return types.SimpleNamespace(created=created, deleted=deleted)


# ── start, odpytywanie ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_polls_and_notifies_listeners(hass, goodwe_udp_sim, issues):
    conn = _conn(hass(), _target(goodwe_udp_sim))
    calls = []
    conn.add_listener(lambda: calls.append(1))
    try:
        await conn.async_start()
        assert conn.refused() is None and conn.identity == "confirmed"
        r = await conn.async_poll()
        assert r is not None and conn.reading is r and r.values["soc"] is not None and calls
        assert all(addr != 47760 for _, addr, _ in goodwe_udp_sim.log)        # rejestr bez odczytu pomijany
    finally:
        await conn.async_stop()


@pytest.mark.asyncio
async def test_refuses_when_goodwe_entry_uses_same_host(hass, goodwe_udp_sim):
    h = hass(_entry("goodwe", "g", data={"host": goodwe_udp_sim.host}))
    conn = _conn(h, _target(goodwe_udp_sim))
    await conn.async_start()
    assert conn.refused() == "direct_conflict:goodwe" and goodwe_udp_sim.requests == 0
    assert await conn.async_poll() is None
    await conn.async_stop()


@pytest.mark.asyncio
async def test_refuses_second_volcast_entry_same_host(hass, goodwe_udp_sim):
    other = _entry(DOMAIN, "other", options={"control_mode": "direct",
                                              "direct_target": {"host": goodwe_udp_sim.host}})
    conn = _conn(hass(other), _target(goodwe_udp_sim))
    await conn.async_start()
    assert conn.refused() == "direct_in_use" and goodwe_udp_sim.requests == 0


@pytest.mark.asyncio
async def test_host_registered_by_other_connection_is_in_use(hass, goodwe_udp_sim):
    h = hass()
    h.data.setdefault(DOMAIN, {})["direct_hosts"] = {goodwe_udp_sim.host: "other"}
    conn = _conn(h, _target(goodwe_udp_sim))
    await conn.async_start()
    assert conn.refused() == "direct_in_use" and goodwe_udp_sim.requests == 0


@pytest.mark.asyncio
async def test_disabled_other_entry_allows(hass, goodwe_udp_sim):
    h = hass(_entry("goodwe", "g", data={"host": goodwe_udp_sim.host}, disabled_by="user"))
    conn = _conn(h, _target(goodwe_udp_sim))
    try:
        await conn.async_start()
        assert conn.refused() is None
    finally:
        await conn.async_stop()


@pytest.mark.asyncio
async def test_hostname_vendor_entry_conflicts(hass, goodwe_udp_sim):
    async def resolve(name):
        return {"goodwe.local": (goodwe_udp_sim.host,)}.get(name)       # None = nierozwiązywalny
    for host in ("goodwe.local", "nowhere.local"):
        h = hass(_entry("goodwe", "g", data={"host": host}))
        conn = _conn(h, _target(goodwe_udp_sim), resolve=resolve)
        await conn.async_start()
        assert conn.refused() == "direct_conflict:goodwe"
    assert goodwe_udp_sim.requests == 0


@pytest.mark.asyncio
async def test_host_in_options_conflicts(hass, goodwe_udp_sim):
    h = hass(_entry("solarman", "s", data={"name": "x"}, options={"host": goodwe_udp_sim.host}))
    conn = _conn(h, _target(goodwe_udp_sim))
    await conn.async_start()
    assert conn.refused() == "direct_conflict:solarman"


@pytest.mark.asyncio
async def test_entry_snaps_resolution_timeout_is_unknown(hass):
    async def slow(name):
        await asyncio.sleep(10)
    h = hass(_entry("goodwe", "g", data={"host": "slow.local"}), _entry("met", "m", data={"host": "x.local"}))
    snaps = await async_entry_snaps(h, "self", resolve=slow, timeout_s=0.05)
    by_domain = {s.domain: s for s in snaps}
    assert by_domain["goodwe"].addresses is None and "met" not in by_domain
    assert by_domain[DOMAIN].is_self


@pytest.mark.asyncio
async def test_bad_target_refused_without_socket(hass, goodwe_udp_sim):
    conn = _conn(hass(), {**_target(goodwe_udp_sim), "host": "8.8.8.8"})
    await conn.async_start()
    assert conn.refused() == "bad_target" and conn.client is None
    conn = _conn(hass(), {**_target(goodwe_udp_sim), "transport": "carrier_pigeon"})
    await conn.async_start()
    assert conn.refused() == "bad_target"


# ── tożsamość ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_identity_mismatch_after_reconnect_blocks_writes(hass, modbus_tcp_sim, goodwe_bank, sim_faults, issues):
    clock = FakeClock()
    conn = _conn(hass(), _target(modbus_tcp_sim, kind="modbus_tcp"), clock=clock)
    try:
        await conn.async_start()
        assert conn.identity == "confirmed"
        for a in range(35003, 35011):
            goodwe_bank.poke(a, 0x5A5A)                       # inne urządzenie pod tym adresem
        sim_faults.reset_after = 1                             # zerwanie → ponowne połączenie
        clock.advance(10.0)
        await conn.async_poll()
        sim_faults.reset_after = 0
        await asyncio.sleep(0.05)                              # odwrót ponownego łączenia
        for _ in range(2):
            clock.advance(10.0)
            await conn.async_poll()
        assert conn.identity == "mismatch" and conn.reading is None
        assert any(k == "direct_identity_changed" for _, k in issues.created)
        assert goodwe_bank.writes == []
        before = modbus_tcp_sim.requests
        clock.advance(10.0)
        await conn.async_poll()                                # odczyty wstrzymane (poza co 10 min)
        assert modbus_tcp_sim.requests == before
    finally:
        await conn.async_stop()


@pytest.mark.asyncio
async def test_identity_rechecked_after_timeout_streak(hass, goodwe_udp_sim, sim_faults):
    clock = FakeClock()
    conn = _conn(hass(), _target(goodwe_udp_sim), clock=clock)
    try:
        await conn.async_start()
        seen = len(goodwe_udp_sim.log)
        sim_faults.drop_next = 4                               # 2 odczyty × 2 próby bez odpowiedzi
        for _ in range(2):
            clock.advance(10.0)
            await conn.async_poll()
        assert conn.stats.consecutive_timeouts >= 3 and conn.identity_check_due()
        clock.advance(10.0)
        await conn.async_poll()
        assert (3, 35000, 33) in goodwe_udp_sim.log[seen + 4:] and conn.identity == "confirmed"
        assert not conn.identity_check_due()
    finally:
        await conn.async_stop()


@pytest.mark.asyncio
async def test_identity_check_due_hourly_on_udp(hass, goodwe_udp_sim):
    clock = FakeClock()
    conn = _conn(hass(), _target(goodwe_udp_sim), clock=clock)
    try:
        await conn.async_start()
        assert not conn.identity_check_due()
        clock.advance(3601.0)
        assert conn.identity_check_due()
        assert await conn.async_confirm_identity() == "confirmed" and not conn.identity_check_due()
    finally:
        await conn.async_stop()


# ── łącze, kolizja w pracy, zatrzymanie ───────────────────────────────────


@pytest.mark.asyncio
async def test_link_down_keeps_last_reading_and_age_grows(hass, goodwe_udp_sim, sim_faults):
    clock = FakeClock()
    conn = _conn(hass(), _target(goodwe_udp_sim), clock=clock)
    try:
        await conn.async_start()
        first = await conn.async_poll()
        assert conn.age_s() == 0.0
        sim_faults.drop_next = 100
        clock.advance(30.0)
        assert await conn.async_poll() is first and conn.reading is first
        assert conn.age_s() == 30.0 and conn.stats.timeouts > 0
    finally:
        await conn.async_stop()


@pytest.mark.asyncio
async def test_conflict_slows_polling(hass, goodwe_udp_sim):
    clock = FakeClock()
    conn = _conn(hass(), _target(goodwe_udp_sim), clock=clock, poll_s=10.0)
    try:
        await conn.async_start()
        await conn.async_poll()
        clock.advance(10.0)
        assert conn.poll_due()
        conn.stats.stray += 3                                  # obce ramki: inny klient
        await conn.async_poll()
        assert conn.conflict and conn.monitor.reason == "stray_frames"
        clock.advance(10.0)
        assert not conn.poll_due()
        clock.advance(DIRECT_SLOW_POLL_S)
        assert conn.poll_due()
    finally:
        await conn.async_stop()


@pytest.mark.asyncio
async def test_stop_is_idempotent_and_unregisters_host(hass, goodwe_udp_sim):
    h = hass()
    conn = _conn(h, _target(goodwe_udp_sim))
    await conn.async_start()
    assert h.data[DOMAIN]["direct_hosts"] == {goodwe_udp_sim.host: "self"}
    await conn.async_stop()
    await conn.async_stop()
    assert h.data[DOMAIN]["direct_hosts"] == {} and await conn.async_poll() is None


@pytest.mark.asyncio
async def test_stop_during_poll_cancels_cleanly(hass, goodwe_udp_sim, sim_faults):
    conn = _conn(hass(), _target(goodwe_udp_sim))
    await conn.async_start()
    sim_faults.delay_s = 5.0
    task = asyncio.create_task(conn.async_poll())
    await asyncio.sleep(0.05)
    await asyncio.wait_for(conn.async_stop(), 2.0)
    assert task.done()
    assert task.cancelled() or task.result() is None


@pytest.mark.asyncio
async def test_logs_never_contain_host(hass, goodwe_udp_sim, sim_faults, caplog):
    conn = _conn(hass(), _target(goodwe_udp_sim, host="127.0.0.1"))
    with caplog.at_level(logging.DEBUG):
        await conn.async_start()
        sim_faults.drop_next = 100
        await conn.async_poll()
        await conn.async_stop()
    assert "127.0.0.1" not in caplog.text and str(goodwe_udp_sim.port) not in caplog.text


def test_target_fingerprint_salted_and_hides_host():
    t = {"transport": "modbus_tcp", "host": "192.168.1.50", "port": 502, "unit_id": 1}
    fp = target_fingerprint(t, SALT)
    assert fp == target_fingerprint(dict(t), SALT) and fp != target_fingerprint(t, bytes(range(1, 17)))
    assert len(fp) == 16 and "192" not in fp
    assert fp != target_fingerprint({**t, "port": 503}, SALT)
