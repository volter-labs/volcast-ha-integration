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
    await h.conn.async_start()                     # odmowa z ponowieniem zamiast wyjątku
    try:
        assert h.conn.refused() == "start_failed"
        assert h.hass.data[DOMAIN]["direct_hosts"] == {}
    finally:
        await h.conn.async_stop()


# ── wstrzymanie właściciela nie przeżywa zmiany planu i nie blokuje bezpieczeństwa ──


@pytest.mark.asyncio
@pytest.mark.parametrize("slot", [
    {"mode": "charge", "charge_source": "grid", "power_w": 2000, "soc_target": 90, "price_pln_kwh": 0.2},
    {"mode": "self_consume", "price_pln_kwh": 0.2}])
async def test_owner_power_takeover_then_plan_mode_change_releases_group(make_hass, goodwe_udp_sim, goodwe_bank,
                                                                         issues, slot):
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        goodwe_bank.poke(POWER, 4000)
        await h.cycle()
        goodwe_bank.poke(POWER, 4500)
        await h.cycle()
        assert h.ex.paused
        new = plan(sid="p2", slots=[{"from": "2026-09-23T10:00:00Z", "to": "2026-09-23T11:00:00Z", **slot}])
        await h.ex.async_on_plan(new, parse_schedule(new))
        for _ in range(35):
            await h.cycle()
        assert gw_raw_word(goodwe_bank, MODE) != 10 and not h.ex._owner_held
    finally:
        await h.close()


def _no_charge_raw(days=3):
    from datetime import timedelta

    from tests.core.control.tou_helpers import DAY0, SELF, iso
    slots = []
    for d in range(-1, days):
        for hr in range(24):
            s = DAY0 + timedelta(days=d, hours=hr)
            slots.append({"from": iso(s), "to": iso(s + timedelta(hours=1)), **SELF})
    return {"schedule_id": "nocharge", "slots": slots, "fallback": {"mode": "self_consume", "soc_reserve": 10},
            "control_enabled": True}


async def _deye_owner_took_soc(make_hass, sim, bank, reg=166):
    h = await _deye(make_hass, sim).start(raw=TOU_RAW)
    await h.ex.async_tick()
    bank.poke(reg, 55)
    await h.cycle(400.0)
    bank.poke(reg, 60)
    await h.cycle(400.0)
    assert h.ex.paused and h.ex._owner_held
    return h


@pytest.mark.asyncio
async def test_tou_owner_field_held_does_not_block_safety_rewrite(make_hass, rtu_tcp_sim, deye_bank, issues):
    h = await _deye_owner_took_soc(make_hass, rtu_tcp_sim, deye_bank)
    try:
        assert any(w & 1 for w in deye_bank.read(172, 6))      # nasz program ładowania z sieci
        raw = _no_charge_raw()
        await h.ex.async_on_plan(raw, parse_schedule(raw))
        for _ in range(20):
            await h.cycle(400.0)
        grid, en = deye_bank.read(172, 6), deye_bank.read(146, 1)[0]
        assert not (any(w & 1 for w in grid) and en & 1)
        assert deye_bank.read(166, 1)[0] == 60                  # pole właściciela zostaje
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_tou_held_start_field_falls_back_to_safety_off(make_hass, rtu_tcp_sim, deye_bank, issues):
    h = await _deye(make_hass, rtu_tcp_sim).start(raw=TOU_RAW)
    try:
        await h.ex.async_tick()
        starts = deye_bank.read(148, 6)
        reg = 148 + 1                                           # start programu 2
        deye_bank.poke(reg, starts[1] + 5)
        await h.cycle(400.0)
        deye_bank.poke(reg, starts[1] + 10)
        await h.cycle(400.0)
        assert h.ex.paused and any(k.endswith(".start") for k in h.ex._owner_held)
        # Te same godziny ładowania (start programu 2 bez zmian w planie), ale mniejsza moc — zmiana w stronę
        # bezpieczną, której nie da się zapisać bez przejętego startu.
        raw = tou_raw()
        for slot in raw["slots"]:
            if slot.get("mode") == "charge":
                slot["power_w"] = 1000
        await h.ex.async_on_plan(raw, parse_schedule(raw))
        for _ in range(20):
            await h.cycle(400.0)
        assert h.ex._owner_held                                 # plan nie zmienił startu — pole dalej właściciela
        assert deye_bank.read(146, 1)[0] & 1 == 0               # harmonogram OFF zamiast żywego, mocniejszego ładowania
        assert deye_bank.read(149, 1)[0] == starts[1] + 10
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_refused_while_owned_issue_not_recreated_every_tick(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    from custom_components.volcast.control.store import ControlStore
    store = ControlStore(make_hass(), "e1")
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank, store=store)
    await h.close()
    h2 = Harness(make_hass, GW_V, h.target, store=store)
    h2.hass.data.setdefault(DOMAIN, {})["direct_hosts"] = {goodwe_udp_sim.host: object()}   # inny wpis
    await h2.ex.async_start()
    await h2.conn.async_start()
    try:
        assert h2.conn.refused() == "direct_in_use"
        await h2.ex.async_set_consent(False)
        deleted = len(issues.deleted)
        for _ in range(3):
            await h2.cycle()
        conflict = [i for i, k, _ in issues.created if k == "direct_conflict"]
        assert len(conflict) == 1 and "direct_conflict_e1" not in issues.deleted[deleted:]
    finally:
        await h2.ex.async_stop()


@pytest.mark.asyncio
async def test_stopping_connection_does_not_cancel_the_restore_caller(make_hass, goodwe_udp_sim, goodwe_bank,
                                                                      sim_faults, issues):
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        sim_faults.delay_s = 0.3
        caller = asyncio.create_task(h.conn.async_read_fresh(h.ex._last_write_end))
        await asyncio.sleep(0.05)
        await h.conn.async_stop()
        await asyncio.wait({caller}, timeout=2.0)
        assert caller.done() and not caller.cancelled() and caller.result() is None
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_conflicted_connection_stops_after_restore(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    from custom_components.volcast.control.store import ControlStore
    store = ControlStore(make_hass(), "e1")
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank, store=store)
    await h.close()
    other = type(h.entry)(domain="goodwe", entry_id="g", data={"host": goodwe_udp_sim.host}, options={},
                          disabled_by=None)
    h2 = Harness(make_hass, GW_V, h.target, store=store)
    h2.hass.config_entries._entries.append(other)
    await h2.ex.async_start()
    h2.conn.allow_conflicted_restore = h2.ex.owned
    await h2.conn.async_start()
    try:
        await h2.ex.async_set_consent(False)
        await h2.cycle()
        assert not h2.ex.owned and gw_raw_word(goodwe_bank, MODE) == 1
        before = goodwe_udp_sim.requests
        await h2.cycle()
        assert goodwe_udp_sim.requests == before and h2.hass.data[DOMAIN]["direct_hosts"] == {}
    finally:
        await h2.close()
