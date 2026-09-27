"""Tryb bezpośredni: powrót do trybu bazowego tylko na świeżym odczycie, przejęcie przez właściciela
kończy się po pauzie, migawka TOU nigdy przy własności, kolizja statyczna nie blokuje powrotu."""
from __future__ import annotations

import asyncio

import pytest

from custom_components.volcast.const import DOMAIN
from custom_components.volcast.control import direct as direct_mod
from custom_components.volcast.core.slot import parse_schedule
from tests.control.test_executor import plan
from tests.control.test_executor_direct import (  # noqa: F401 — fixture `issues`
    EXPORT_EN, GW_V, MODE, POWER, TOU_RAW, Harness, _deye, _owned_sell, gw_raw_word, gw_target, issues, tou_raw)

FAST = 0.2


async def _hold_poll(conn, seconds: float) -> None:
    await conn._poll_lock.acquire()
    try:
        await asyncio.sleep(seconds)
    finally:
        conn._poll_lock.release()


# ── powrót na świeżym odczycie ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_restore_stale_reading_owner_in_auto(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    goodwe_bank.poke(MODE, 1)                                  # właściciel w trybie bazowym (auto)
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    h.conn.fresh_wait_s = FAST
    try:
        await h.conn._poll_lock.acquire()                     # odpytywanie w toku przez cały tik
        try:
            await h.ex.async_set_consent(False)
            await h.ex.async_tick()
        finally:
            h.conn._poll_lock.release()
        assert h.ex.owned and h.ex.last_decision.reason == "no_fresh_reading"
        assert gw_raw_word(goodwe_bank, MODE) == 10              # nic nie zapisane na nieświeżym odczycie
        await h.ex.async_tick()                                  # następny tik: świeży odczyt → powrót
        assert gw_raw_word(goodwe_bank, MODE) == 1 and not h.ex.owned
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_restore_waits_for_in_flight_poll(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    goodwe_bank.poke(MODE, 1)
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        holder = asyncio.create_task(_hold_poll(h.conn, 0.1))
        await asyncio.sleep(0)
        await h.ex.async_set_consent(False)
        await h.ex.async_tick()
        await holder
        assert gw_raw_word(goodwe_bank, MODE) == 1 and not h.ex.owned
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_restore_releases_ownership_only_when_read_back_confirms(make_hass, goodwe_udp_sim, goodwe_bank,
                                                                       issues):
    goodwe_bank.poke(MODE, 1)
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        goodwe_bank.ignore_writes.add(MODE)                   # urządzenie nie przyjmuje trybu bazowego
        await h.ex.async_set_consent(False)
        await h.cycle()
        assert h.ex.owned and h.ex.last_decision.reason == "restore_failed"
        goodwe_bank.ignore_writes.discard(MODE)
        await h.cycle()
        assert gw_raw_word(goodwe_bank, MODE) == 1 and not h.ex.owned
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_restore_skips_frames_for_keys_already_at_baseline(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        goodwe_bank.poke(MODE, 1)                              # właściciel już przestawił tryb
        goodwe_bank.poke(EXPORT_EN, 1)
        n = len(goodwe_bank.writes)
        await h.ex.async_set_consent(False)
        await h.ex.async_tick()                                # odczyt z cyklu nieświeży — decyduje odczyt pisarza
        assert goodwe_bank.writes[n:] == [] and not h.ex.owned
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_tou_revoke_stale_reading(make_hass, rtu_tcp_sim, deye_bank, issues):
    before = deye_bank.read(146, 32)
    h = await _deye(make_hass, rtu_tcp_sim).start(raw=TOU_RAW)
    h.conn.fresh_wait_s = FAST
    try:
        await h.ex.async_tick()
        ours = deye_bank.read(146, 32)
        assert ours != before
        await h.conn._poll_lock.acquire()
        try:
            await h.ex.async_set_consent(False)
            await h.ex.async_tick()
        finally:
            h.conn._poll_lock.release()
        assert h.ex.owned and h.ex._state.tou_snapshot is not None       # nic nie oddane na ślepo
        await h.ex.async_tick()
        assert deye_bank.read(146, 32) == before and not h.ex.owned
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_interrupted_rewrite_restore_waits_for_fresh_reading(make_hass, rtu_tcp_sim, deye_bank, issues):
    before = deye_bank.read(146, 32)
    h = await _deye(make_hass, rtu_tcp_sim).start(raw=TOU_RAW)
    h.conn.fresh_wait_s = FAST
    try:
        deye_bank.ignore_writes.add(167)
        await h.conn._poll_lock.acquire()
        try:
            await h.ex.async_tick()
        finally:
            h.conn._poll_lock.release()
        assert h.ex._tou_restore_pending                        # powrót czeka na świeży odczyt
        await h.cycle(5.0)
        assert deye_bank.read(146, 32) == before and not h.ex._tou_restore_pending
    finally:
        await h.close()


# ── migawka TOU nigdy przy własności ──────────────────────────────────────


@pytest.mark.asyncio
async def test_tou_resnapshot_while_owned_captures_our_programs(make_hass, rtu_tcp_sim, deye_bank, issues):
    h = await _deye(make_hass, rtu_tcp_sim).start(raw=TOU_RAW)
    try:
        await h.ex.async_tick()
        assert h.ex.owned
        h.ex._state.tou_snapshot = None                        # np. loader odrzucił zapisany kształt
        raw2 = {**tou_raw(), "schedule_id": "tou2"}
        for s in raw2["slots"]:
            if s.get("mode") == "charge":
                s["power_w"] = 5000
        await h.ex.async_on_plan(raw2, parse_schedule(raw2))
        await h.cycle(4000.0)
        assert h.ex._state.tou_snapshot is None                # nie bierzemy NASZYCH programów za właściciela
        assert "tou_snapshot_lost" in issues.keys()
        await h.ex.async_set_consent(False)
        await h.cycle()
        after = deye_bank.read(146, 32)
        assert not (after[0] & 1) and not h.ex.owned           # programy bazowe + harmonogram OFF
    finally:
        await h.close()


# ── przejęcie przez właściciela ───────────────────────────────────────────


async def _taken_over(make_hass, sim, bank):
    h = await _owned_sell(make_hass, sim, bank)
    bank.poke(POWER, 4000)
    await h.cycle()
    bank.poke(POWER, 4000)
    await h.cycle()
    assert h.ex.paused
    return h


@pytest.mark.asyncio
async def test_takeover_pause_ends_while_owner_value_persists(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await _taken_over(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        n0 = len(h.ex.foreign_changes)
        for _ in range(40):                                    # ~40 min, właściciel nic więcej nie robi
            await h.cycle()
        assert not h.ex.paused and len(h.ex.foreign_changes) == n0
        assert gw_raw_word(goodwe_bank, POWER) == 4000         # wartość właściciela zostaje (plan bez zmian)
        goodwe_bank.poke(POWER, 3000)                          # NOWA zmiana właściciela — liczy się od nowa
        await h.cycle()
        assert not h.ex.paused
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_plan_back_to_self_consume_after_takeover_is_applied(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await _taken_over(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        sc = plan(sid="p2", slots=[{"from": "2026-09-23T10:00:00Z", "to": "2026-09-23T11:00:00Z",
                                    "mode": "self_consume", "price_pln_kwh": 0.2}])
        await h.ex.async_on_plan(sc, parse_schedule(sc))
        for _ in range(35):
            await h.cycle()
        assert not h.ex.paused and gw_raw_word(goodwe_bank, MODE) == 1
    finally:
        await h.close()


# ── kolizja statyczna przy starcie nie blokuje powrotu ────────────────────


@pytest.mark.asyncio
async def test_static_conflict_at_start_never_blocks_restore_when_owned(make_hass, goodwe_udp_sim, goodwe_bank,
                                                                        issues, caplog):
    from custom_components.volcast.control.store import ControlStore
    store = ControlStore(make_hass(), "e1")
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank, store=store)
    await h.close()
    other = type(h.entry)(domain="goodwe", entry_id="g", data={"host": goodwe_udp_sim.host}, options={},
                          disabled_by=None)
    h2 = Harness(make_hass, GW_V, h.target, store=store)
    h2.hass.config_entries._entries.append(other)          # właściciel włączył integrację falownika
    await h2.ex.async_start()
    h2.conn.allow_conflicted_restore = h2.ex.owned
    await h2.conn.async_start()
    try:
        assert h2.conn.refused() is None and h2.conn.conflict
        await h2.ex.async_set_consent(False)
        await h2.cycle()
        assert gw_raw_word(goodwe_bank, MODE) == 1 and not h2.ex.owned
        assert "direct_conflict" in issues.keys()
    finally:
        await h2.close()


@pytest.mark.asyncio
async def test_static_conflict_at_start_still_refuses_when_not_owned(make_hass, goodwe_udp_sim, goodwe_bank):
    h = Harness(make_hass, GW_V, gw_target(goodwe_udp_sim))
    other = type(h.entry)(domain="goodwe", entry_id="g", data={"host": goodwe_udp_sim.host}, options={},
                          disabled_by=None)
    h.hass.config_entries._entries.append(other)
    await h.ex.async_start()
    h.conn.allow_conflicted_restore = h.ex.owned
    await h.conn.async_start()
    try:
        assert h.conn.refused() == "direct_conflict:goodwe" and goodwe_udp_sim.requests == 0
    finally:
        await h.close()


# ── rejestracja hosta zwalniana przy nieoczekiwanym wyjątku ───────────────


@pytest.mark.asyncio
async def test_host_released_when_start_raises_after_registration(make_hass, goodwe_udp_sim, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("client")
    monkeypatch.setattr(direct_mod, "RegisterClient", boom)
    h = Harness(make_hass, GW_V, gw_target(goodwe_udp_sim))
    with pytest.raises(RuntimeError):
        await h.conn.async_start()
    assert h.hass.data[DOMAIN]["direct_hosts"] == {}
