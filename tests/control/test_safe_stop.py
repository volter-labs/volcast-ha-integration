"""Bezpieczne zatrzymanie (oba tryby): zatrzymanie HA, wyładowanie/przeładowanie wpisu i usunięcie wpisu
nie zostawiają falownika w NASZYM trybie wymuszonym — zapis trybu neutralnego w budżecie
`STOP_WRITE_TIMEOUT_S`, własność zostaje (następny wykonawca przejmuje sterowanie)."""
from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.volcast.const import DOMAIN, STOP_WRITE_TIMEOUT_S
from custom_components.volcast.control import runtime as rt_mod
from custom_components.volcast.control.store import ControlStore
from tests.control.test_executor import E, goodwe_hass, make, ready
from tests.control.test_executor_direct import (_BACKEND, MODE, POWER, Harness, GW_V, _executor_job,
                                                _loopback_connection, _noop, _owned_sell, _salt, gw_raw_word,
                                                gw_target, issues, regs)  # noqa: F401 — `issues` to fikstura

LOGGER = "custom_components.volcast"


def _stale(h, age: float = 400.0) -> None:
    r = h.conn.reading
    h.conn.reading = SimpleNamespace(**{**r.__dict__, "at_mono": h.clock() - age})


def _rt(ex, conn=None):
    return rt_mod.ControlRuntime(ex, None, SimpleNamespace(async_stop=_noop), None, None, {}, 8000.0, direct=conn)


# ── tryb bezpośredni ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_in_forced_mode_writes_neutral_and_keeps_ownership_direct(make_hass, goodwe_udp_sim,
                                                                             goodwe_bank, issues):
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        n = len(goodwe_bank.writes)
        assert await h.ex.async_neutral_at_stop() is True
        assert regs(goodwe_bank)[n:] == [MODE] and gw_raw_word(goodwe_bank, MODE) == 1
        assert h.ex.owned and h.ex._state.snapshot              # przejęcie po restarcie, migawka zostaje
        await h.cycle()                                          # po zatrzymaniu żadnego cyklu z planem
        assert gw_raw_word(goodwe_bank, MODE) == 1
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_stop_on_a_stale_reading_uses_only_the_minimum_frames_direct(make_hass, goodwe_udp_sim, goodwe_bank,
                                                                           issues):
    # Bez pełnego odpytania: odczyt rejestru trybu (pod wyłącznością łącza), zapis, odczyt zwrotny.
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        h.clock.advance(120.0)
        _stale(h)
        start = len(goodwe_udp_sim.log)
        t0 = time.monotonic()
        assert await h.ex.async_neutral_at_stop() is True
        assert time.monotonic() - t0 < STOP_WRITE_TIMEOUT_S
        frames = [(fc, a) for fc, a, _ in goodwe_udp_sim.log[start:]]
        # odczyt trybu przed zapisem (+ odczyt rozdzielający UDP), zapis, odczyt zwrotny — bez bloków stanu
        assert frames.count((0x06, MODE)) == 1 and len(frames) <= 4
        assert not {35105, 35140, 35180, 36008, 37003} & {a for _, a in frames}
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_stop_in_the_middle_of_a_write_sequence_waits_then_writes_neutral_direct(make_hass, goodwe_udp_sim,
                                                                                      goodwe_bank, issues):
    goodwe_bank.poke(MODE, 1)
    h = await Harness(make_hass, GW_V, gw_target(goodwe_udp_sim)).start()
    try:
        writer = h.io.writer
        real = writer.async_write
        power_done, release = asyncio.Event(), asyncio.Event()

        async def slow(w):
            out = await real(w)
            if w.key == "power_w":
                power_done.set()
                await release.wait()                            # HA zatrzymuje się między mocą a trybem
            return out
        writer.async_write = slow
        cycle = asyncio.create_task(h.ex.async_tick())
        await power_done.wait()
        stop = asyncio.create_task(h.ex.async_neutral_at_stop())
        await asyncio.sleep(0.05)
        assert not stop.done()                                  # czeka na koniec sekwencji (blokada)
        release.set()
        await cycle
        assert await stop is True
        assert [a for a in regs(goodwe_bank)] == [POWER, MODE, MODE]
        assert gw_raw_word(goodwe_bank, MODE) == 1 and h.ex.owned
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_stop_gives_up_within_the_budget_when_a_write_hangs(make_hass, goodwe_udp_sim, goodwe_bank, issues,
                                                                  caplog):
    caplog.set_level(logging.ERROR, logger=LOGGER)
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        h.ex._stop_timeout_s = 0.2
        await h.ex._lock.acquire()                              # zapis w toku, który nie kończy się
        try:
            t0 = time.monotonic()
            assert await h.ex.async_neutral_at_stop() is False
            assert time.monotonic() - t0 < 1.0
        finally:
            h.ex._lock.release()
        assert "neutral mode before stopping" in caplog.text and gw_raw_word(goodwe_bank, MODE) == 10
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_stop_leaves_a_mode_we_did_not_write_direct(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    goodwe_bank.poke(MODE, 10)                                   # tryb właściciela, bez własności
    h = await Harness(make_hass, GW_V, gw_target(goodwe_udp_sim)).start(consent=False)
    try:
        assert await h.ex.async_neutral_at_stop() is False
        assert goodwe_bank.writes == [] and gw_raw_word(goodwe_bank, MODE) == 10
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_ha_stop_event_writes_neutral_through_the_runtime_listener(make_hass, goodwe_udp_sim, goodwe_bank,
                                                                         issues):
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    listeners, tasks = [], []
    h.hass.bus = SimpleNamespace(async_listen_once=lambda ev, cb: listeners.append((ev, cb)) or (lambda: None))
    h.hass.async_create_task = lambda coro, *a, **k: tasks.append(asyncio.get_running_loop().create_task(coro))
    try:
        rt = _rt(h.ex, h.conn)
        rt_mod.track_ha_stop(h.hass, rt)
        assert [ev for ev, _ in listeners] == ["homeassistant_stop"]
        listeners[0][1](SimpleNamespace(event_type="homeassistant_stop"))
        await asyncio.gather(*tasks)
        assert gw_raw_word(goodwe_bank, MODE) == 1 and h.ex.owned
        for unsub in rt.unsubs:                                  # wyładowanie po zatrzymaniu: bez błędu
            unsub()
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_reload_writes_neutral_and_keeps_ownership_direct(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    n = len(goodwe_bank.writes)
    await rt_mod.async_unload_control(h.hass, _rt(h.ex, h.conn))
    assert regs(goodwe_bank)[n:] == [MODE] and gw_raw_word(goodwe_bank, MODE) == 1
    assert h.hass.data[DOMAIN]["direct_hosts"] == {} and h.ex.owned
    # Następny wykonawca (przeładowanie) przejmuje sterowanie z zachowanej własności.
    await h.restart()
    try:
        await h.cycle()
        assert gw_raw_word(goodwe_bank, MODE) == 10
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_remove_owned_direct_with_link_down_raises_a_repair_issue(make_hass, goodwe_udp_sim, goodwe_bank,
                                                                        monkeypatch, caplog, issues):
    store = ControlStore(make_hass(), "e1")
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank, store=store)
    await h.close()
    await goodwe_udp_sim.close()                                 # falownik poza zasięgiem
    monkeypatch.setattr(rt_mod, "ControlStore", lambda hass, eid: store)
    monkeypatch.setattr(rt_mod, "async_installation_salt", _salt)
    monkeypatch.setattr(rt_mod, "DirectConnection", _loopback_connection)
    entry = SimpleNamespace(domain=DOMAIN, entry_id="e1", options=h.options, disabled_by=None,
                            data={"api_key": "vk_x", "backend": _BACKEND})
    hass = make_hass(entries=[entry])
    hass.async_add_executor_job = _executor_job
    with caplog.at_level(logging.WARNING):
        await rt_mod.async_remove_control(hass, entry)
    assert "could not return the inverter" in caplog.text
    assert ("control_removal_failed_e1", "control_record_dropped") in [(i, k) for i, k, _ in issues.created]
    assert "control_removal_failed_e1" not in issues.deleted


@pytest.mark.asyncio
async def test_remove_with_unreadable_state_raises_a_repair_issue(make_hass, monkeypatch, issues):
    class Broken:
        async def async_load(self):
            raise ValueError("format")

        async def async_remove(self):
            return None
    monkeypatch.setattr(rt_mod, "ControlStore", lambda hass, eid: Broken())
    entry = SimpleNamespace(domain=DOMAIN, entry_id="e1", options={}, disabled_by=None,
                            data={"api_key": "vk_x", "backend": _BACKEND})
    await rt_mod.async_remove_control(make_hass(entries=[entry]), entry)
    assert "control_removal_failed_e1" in [i for i, _, _ in issues.created]


# ── tryb encji ────────────────────────────────────────────────────────────


def test_stop_in_forced_mode_writes_neutral_entities(monkeypatch):
    h, ex = make(monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_tick()
        assert h.states.get(E["mode"]).state == "sell_power"
        n = len(h.services.calls)
        done = await ex.async_neutral_at_stop()
        return done, h.services.calls[n:]
    done, calls = asyncio.run(go())
    assert done is True and [(c[0], c[1], c[2].get("option")) for c in calls] == [("select", "select_option", "auto")]
    assert ex.owned


def test_reload_writes_neutral_entities(monkeypatch):
    h, ex = make(monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_tick()
        await rt_mod.async_unload_control(h, _rt(ex))
    asyncio.run(go())
    assert h.states.get(E["mode"]).state == "auto" and ex.owned


def test_reload_without_our_mode_writes_nothing_entities(monkeypatch):
    h = goodwe_hass(mode="sell_power")                           # tryb właściciela, nic nie zapisaliśmy
    h, ex = make(h, monkeypatch=monkeypatch)

    async def go():
        await ready(ex, consent=False)
        await ex.async_tick()
        await rt_mod.async_unload_control(h, _rt(ex))
    asyncio.run(go())
    assert h.services.calls == [] and h.states.get(E["mode"]).state == "sell_power"


def test_stop_with_a_failing_inverter_integration_does_not_raise(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    h, ex = make(monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_tick()
        h.services.fail[E["mode"]] = HomeAssistantError("down")
        return await ex.async_neutral_at_stop()
    assert asyncio.run(go()) is False
    assert h.states.get(E["mode"]).state == "sell_power" and ex.owned
    assert "setting the inverter to its neutral mode failed" in caplog.text
