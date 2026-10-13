"""Tryb pracy falownika w trybie bezpośrednim: jeden rejestr, tylko odczyt, ten sam `WorkModeTracker`."""
from __future__ import annotations

import pytest

from custom_components.volcast.control.work_mode import classify_direct
from custom_components.volcast.core.profile import profile_from_dict
from tests.control.test_executor_direct import GW_V, Harness, gw_target

WM_ADDR = 47000
WM_POLL_S = 300


def _thaw(o):
    if isinstance(o, dict) or hasattr(o, "items"):
        return {k: _thaw(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_thaw(v) for v in o]
    return o


def _wm_profile(poll_s: int = WM_POLL_S, neutral=(0,)):
    raw = _thaw(GW_V.raw)
    raw["work_mode"] = {"read": {"addr": WM_ADDR, "type": "u16"}, "neutral_values": list(neutral),
                        "ha": {"domain": "select", "unique_id_regex": "work_mode$", "neutral_options": ["general"]},
                        "poll_s": poll_s}
    return profile_from_dict(raw)


class Spy:
    """Zlicza odczyty na kliencie połączenia (bez drugiego gniazda)."""

    def __init__(self, conn) -> None:
        self.calls: list[tuple[int, int]] = []
        orig = conn.client.read_block

        async def spy(addr, count, fc=3, *, tries=None):
            self.calls.append((addr, count))
            return await orig(addr, count, fc, tries=tries)

        conn.client.read_block = spy

    @property
    def wm(self) -> int:
        return sum(1 for c in self.calls if c == (WM_ADDR, 1))


async def _start(make_hass, sim, profile=None):
    h = Harness(make_hass, profile or _wm_profile(), gw_target(sim))
    await h.conn.async_start()
    spy = Spy(h.conn)
    return h, spy


# ── classify_direct ──


def test_classify_direct_cases():
    assert classify_direct(0, (0,)) == "general"
    assert classify_direct(3, (0,)) == "other:3"
    assert classify_direct(5, (0, 5)) == "general"
    assert classify_direct(0, (5,)) == "other:0"
    for bad in (-1, None, True, False, "3", 3.0):
        assert classify_direct(bad, (0,)) is None


# ── odczyt w połączeniu ──


@pytest.mark.asyncio
async def test_first_poll_reads_general_then_other_when_due(make_hass, goodwe_udp_sim, goodwe_bank):
    h, spy = await _start(make_hass, goodwe_udp_sim)
    try:
        assert h.conn.work_mode.state == "unknown"
        await h.conn.async_poll()
        assert h.conn.work_mode.state == "general" and spy.wm == 1
        goodwe_bank.poke(WM_ADDR, 3)
        h.clock.advance(WM_POLL_S - 5)
        await h.conn.async_poll()
        assert spy.wm == 1 and h.conn.work_mode.state == "general"      # jeszcze nie na czasie
        h.clock.advance(10)
        await h.conn.async_poll()
        assert spy.wm == 2 and h.conn.work_mode.state == "other:3"
        assert goodwe_bank.writes == []                                   # zero zapisów
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_dropped_read_keeps_state(make_hass, goodwe_udp_sim, goodwe_bank):
    h, spy = await _start(make_hass, goodwe_udp_sim)
    try:
        await h.conn.async_poll()
        assert h.conn.work_mode.state == "general"
        goodwe_bank.poke(WM_ADDR, 3)
        h.clock.advance(WM_POLL_S + 1)
        goodwe_bank.unreadable.add(WM_ADDR)                               # rejestr przestaje odpowiadać
        await h.conn.async_poll()
        assert h.conn.work_mode.state == "general" and spy.wm == 2
        assert goodwe_bank.writes == []
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_no_read_while_conflict(make_hass, goodwe_udp_sim, goodwe_bank):
    h, spy = await _start(make_hass, goodwe_udp_sim)
    try:
        h.conn.static_conflicts = ("other",)
        h.conn._last_static = h.clock()                                   # bez ponownego sprawdzania statycznego
        assert h.conn.conflict
        await h.conn.async_poll()
        h.clock.advance(WM_POLL_S + 1)
        await h.conn.async_poll()
        assert spy.wm == 0 and h.conn.work_mode.state == "unknown"
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_profile_without_work_mode_adds_no_request(make_hass, goodwe_udp_sim, goodwe_bank):
    h, spy = await _start(make_hass, goodwe_udp_sim, profile=GW_V)
    try:
        assert GW_V.work_mode is None
        await h.conn.async_poll()
        h.clock.advance(1000)
        await h.conn.async_poll()
        assert spy.wm == 0 and h.conn.work_mode.state == "unknown"
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_replayed_previous_reply_is_never_taken_for_work_mode(make_hass):
    # Cykl kończy się odczytem 1 rejestru (soc_max, 47760 — tu czytelny); odczyt trybu pracy też ma 1 rejestr,
    # więc bez rozdzielenia moduł Wi-Fi odpowiedziałby poprzednią odpowiedzią (wartość soc_max) -> fałszywe „other”.
    from tests.sim.device import RegisterBank
    from tests.sim.fixtures import goodwe_words
    from tests.sim.flaky import flaky_module
    bank = RegisterBank(goodwe_words())
    bank.poke(47760, 5)
    bank.poke(WM_ADDR, 0)
    m = await flaky_module(bank)
    h = Harness(make_hass, _wm_profile(), gw_target(m, unreadable=[]))
    try:
        await h.conn.async_start()
        orig = h.conn.client.transport.read
        reads: list[tuple[int, int]] = []

        async def read(addr, count, **kw):
            reads.append((addr, count))
            if (addr, count) == (WM_ADDR, 1):
                m.plan = ["replay"]                   # następna odpowiedź = poprzednia odpowiedź modułu
            return await orig(addr, count, **kw)

        h.conn.client.transport.read = read
        await h.conn.async_poll()
        assert (47760, 1) in reads and (WM_ADDR, 1) in reads
        assert h.conn.work_mode.state != "other:5"
        assert h.conn.work_mode.state in ("general", "unknown")
        assert bank.writes == []
    finally:
        await h.close()
        await m.close()
