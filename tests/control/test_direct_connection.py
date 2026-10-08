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
async def test_unsupported_keys_are_not_polled(hass, goodwe_udp_sim, goodwe_bank):
    # Sonda: 47760 odpowiada wyjątkiem 2 — nie jest odpytywany (wyjątek byłby „poprzednią odpowiedzią”).
    goodwe_bank.unreadable.clear()
    goodwe_bank.unsupported.add(47760)
    conn = _conn(hass(), _target(goodwe_udp_sim), unreadable=set(), unsupported={"soc_max"})
    try:
        await conn.async_start()
        assert await conn.async_poll() is not None
        assert conn.reading.device["power_w"] == 8846.0
        assert all(a != 47760 for fc, a, _ in goodwe_udp_sim.log)
    finally:
        await conn.async_stop()


def test_goodwe_udp_transport_config_has_300_ms_gap(hass, goodwe_udp_sim):
    cfg = _conn(hass(), _target(goodwe_udp_sim))._config()
    assert (cfg.kind, cfg.gap_s, cfg.timeout_s) == ("goodwe_udp", 0.3, 2.0)


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
        assert conn.identity == "pending"                      # zapisy wstrzymane przed ponownym odczytem
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
    assert h.data[DOMAIN]["direct_hosts"][goodwe_udp_sim.host] is conn
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


# ── tożsamość w toku: zdarzenia łącza, godzinne sprawdzenie na UDP ─────────


@pytest.mark.asyncio
@pytest.mark.parametrize("counter", ["peer_resets", "reconnects"])
async def test_link_event_makes_identity_pending_until_recheck_passes(hass, goodwe_udp_sim, sim_faults, counter):
    clock = FakeClock()
    conn = _conn(hass(), _target(goodwe_udp_sim), clock=clock)
    try:
        await conn.async_start()
        first = await conn.async_poll()
        assert conn.identity == "confirmed"
        setattr(conn.stats, counter, getattr(conn.stats, counter) + 1)
        sim_faults.drop_next = 100                             # ponowne sprawdzenie jeszcze się nie udaje
        clock.advance(10.0)
        await conn.async_poll()
        assert conn.identity == "pending" and conn.identity_check_due()
        assert conn.reading is first                           # bez nowego odczytu z niepotwierdzonego urządzenia
        clock.advance(10.0)
        await conn.async_poll()
        assert conn.identity == "pending"                      # nadal: dopóki sprawdzenie nie przejdzie
        sim_faults.drop_next = 0
        clock.advance(10.0)
        await conn.async_poll()
        assert conn.identity == "confirmed" and conn.reading is not first
    finally:
        await conn.async_stop()


@pytest.mark.asyncio
async def test_link_event_seen_during_read_drops_that_reading(hass, goodwe_udp_sim, sim_faults):
    clock = FakeClock()
    conn = _conn(hass(), _target(goodwe_udp_sim), clock=clock)
    try:
        await conn.async_start()
        first = await conn.async_poll()
        real = conn.client.read_state

        async def read_then_reset():
            r = await real()
            conn.stats.peer_resets += 1
            sim_faults.drop_next = 100                         # ponowne sprawdzenie tożsamości nie przechodzi
            return r
        conn.client.read_state = read_then_reset
        clock.advance(10.0)
        await conn.async_poll()
        assert conn.identity == "pending" and conn.reading is first
    finally:
        await conn.async_stop()


@pytest.mark.asyncio
async def test_hourly_identity_recheck_inside_polling_on_udp(hass, goodwe_udp_sim):
    clock = FakeClock()
    conn = _conn(hass(), _target(goodwe_udp_sim), clock=clock)
    try:
        await conn.async_start()
        await conn.async_poll()
        seen = len(goodwe_udp_sim.log)
        clock.advance(10.0)
        await conn.async_poll()
        assert all(addr != 35000 for _, addr, _ in goodwe_udp_sim.log[seen:])
        seen = len(goodwe_udp_sim.log)
        clock.advance(3601.0)
        await conn.async_poll()
        assert (3, 35000, 33) in goodwe_udp_sim.log[seen:] and conn.identity == "confirmed"
    finally:
        await conn.async_stop()


@pytest.mark.asyncio
async def test_hourly_recheck_mismatch_drops_readings(hass, goodwe_udp_sim, goodwe_bank, issues):
    clock = FakeClock()
    conn = _conn(hass(), _target(goodwe_udp_sim), clock=clock)
    try:
        await conn.async_start()
        assert await conn.async_poll() is not None
        for a in range(35003, 35011):
            goodwe_bank.poke(a, 0x5A5A)                       # inne urządzenie pod tym samym adresem
        clock.advance(3601.0)
        assert await conn.async_poll() is None
        assert conn.identity == "mismatch" and conn.reading is None
        assert any(k == "direct_identity_changed" for _, k in issues.created)
    finally:
        await conn.async_stop()


@pytest.mark.asyncio
async def test_identity_not_read_without_expected_fingerprint(hass, goodwe_udp_sim):
    for fp in (None, "zz" * 8, "ą" * 16):
        conn = _conn(hass(), _target(goodwe_udp_sim, device_fp=fp))
        try:
            await conn.async_start()
            assert conn.refused() is None and conn.identity == "unknown"
            await conn.async_poll()
            assert all(addr != 35000 for _, addr, _ in goodwe_udp_sim.log)
        finally:
            await conn.async_stop()


# ── zatrzymanie w trakcie startu, rejestr hostów ──────────────────────────


@pytest.fixture
def timers(monkeypatch):
    made, cancelled = [], []

    def track(hass, action, interval):
        made.append(action)
        return lambda: cancelled.append(action)
    monkeypatch.setattr(direct_mod, "async_track_time_interval", track)
    return types.SimpleNamespace(made=made, cancelled=cancelled)


@pytest.mark.asyncio
async def test_stop_during_slow_resolve_leaves_nothing_behind(hass, goodwe_udp_sim, timers):
    gate = asyncio.Event()

    async def resolve(name):
        await gate.wait()
        return ("192.168.77.1",)                               # inny adres: bez kolizji
    h = hass(_entry("goodwe", "g", data={"host": "inverter.local"}))
    conn = _conn(h, _target(goodwe_udp_sim), resolve=resolve)
    start = asyncio.create_task(conn.async_start())
    await asyncio.sleep(0.02)
    await conn.async_stop()
    gate.set()
    await asyncio.wait_for(start, 2.0)
    await asyncio.sleep(0.05)
    assert h.data.get(DOMAIN, {}).get("direct_hosts", {}) == {}
    assert conn.client is None and timers.made == [] and goodwe_udp_sim.requests == 0


@pytest.mark.asyncio
async def test_stop_during_identity_read_registers_no_timer(hass, goodwe_udp_sim, sim_faults, timers):
    h = hass()
    conn = _conn(h, _target(goodwe_udp_sim))
    sim_faults.delay_s = 0.3
    start = asyncio.create_task(conn.async_start())
    await asyncio.sleep(0.05)
    await conn.async_stop()
    at_stop = goodwe_udp_sim.requests
    await asyncio.wait_for(start, 3.0)
    await asyncio.sleep(0.4)
    assert timers.made == [] and h.data[DOMAIN]["direct_hosts"] == {}
    assert goodwe_udp_sim.requests == at_stop


@pytest.mark.asyncio
async def test_second_connection_of_same_entry_is_refused(hass, goodwe_udp_sim, caplog):
    h = hass()
    first = _conn(h, _target(goodwe_udp_sim))
    second = _conn(h, _target(goodwe_udp_sim))
    try:
        await first.async_start()
        with caplog.at_level(logging.WARNING):
            await second.async_start()
        assert second.refused() == "direct_in_use" and "in use" in caplog.text
        assert goodwe_udp_sim.host not in caplog.text
        await second.async_stop()
        assert h.data[DOMAIN]["direct_hosts"][goodwe_udp_sim.host] is first
    finally:
        await first.async_stop()
    assert h.data[DOMAIN]["direct_hosts"] == {}


# ── kolizja statyczna w pracy, powrót tożsamości ──────────────────────────


@pytest.mark.asyncio
async def test_static_conflict_rechecked_every_ten_minutes(hass, goodwe_udp_sim):
    clock = FakeClock()
    h = hass()
    conn = _conn(h, _target(goodwe_udp_sim), clock=clock)
    try:
        await conn.async_start()
        h.config_entries._entries.append(_entry("goodwe", "g", data={"host": goodwe_udp_sim.host}))
        clock.advance(10.0)
        await conn.async_poll()
        assert not conn.conflict
        clock.advance(600.0)
        await conn.async_poll()
        assert conn.static_conflicts == ("goodwe",) and conn.conflict
    finally:
        await conn.async_stop()


@pytest.mark.asyncio
async def test_mismatch_retries_every_ten_minutes_and_recovers(hass, goodwe_udp_sim, goodwe_bank, issues):
    clock = FakeClock()
    conn = _conn(hass(), _target(goodwe_udp_sim), clock=clock)
    try:
        await conn.async_start()
        saved = goodwe_bank.read(35003, 8)
        for a in range(35003, 35011):
            goodwe_bank.poke(a, 0x5A5A)
        clock.advance(3601.0)
        await conn.async_poll()
        assert conn.identity == "mismatch"
        for a, w in zip(range(35003, 35011), saved):
            goodwe_bank.poke(a, w)
        clock.advance(10.0)
        await conn.async_poll()
        assert conn.identity == "mismatch"                     # tylko co 10 min
        clock.advance(600.0)
        await conn.async_poll()
        assert conn.identity == "confirmed"
        assert "direct_identity_changed_self" in issues.deleted
    finally:
        await conn.async_stop()


@pytest.mark.asyncio
async def test_stop_with_forget_clears_identity_issue(hass, goodwe_udp_sim, issues):
    conn = _conn(hass(), _target(goodwe_udp_sim))
    await conn.async_start()
    await conn.async_stop(forget=True)
    assert "direct_identity_changed_self" in issues.deleted
