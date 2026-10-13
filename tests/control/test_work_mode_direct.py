"""Tryb pracy falownika w trybie bezpośrednim: jeden rejestr, tylko odczyt, ten sam `WorkModeTracker`."""
from __future__ import annotations

import pytest

from custom_components.volcast.control.work_mode import classify_direct
from custom_components.volcast.core.profile import profile_from_dict
from custom_components.volcast.core.transports.base import RequestTimeout
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


def _without_work_mode_profile():
    raw = _thaw(GW_V.raw)
    raw.pop("work_mode", None)
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


def _sim_reqs(sim) -> int:
    return sum(1 for e in sim.log if (e[1], e[2]) == (WM_ADDR, 1))


def _silent_timeouts_on_wm(conn, state: dict) -> None:
    """Gdy `state["timeout"]`: odczyt 47000 kończy się ciszą (RequestTimeout bez ramki), bez żądania
    do symulatora; liczy próby w `state["calls"]`."""
    orig_sep, orig_rb = conn.client._separated, conn.client.read_block

    async def sep(block, lost):
        if block[0] == WM_ADDR and state["timeout"]:
            state["calls"] += 1
            raise RequestTimeout(silent=True)
        return await orig_sep(block, lost)

    async def rb(addr, count, fc=3, *, tries=None):
        if addr == WM_ADDR and state["timeout"]:
            state["calls"] += 1
            raise RequestTimeout(silent=True)
        return await orig_rb(addr, count, fc, tries=tries)

    conn.client._separated = sep
    conn.client.read_block = rb


@pytest.mark.asyncio
async def test_work_mode_stops_after_one_exception_reply(make_hass, goodwe_udp_sim, goodwe_bank):
    h, _spy = await _start(make_hass, goodwe_udp_sim)
    try:
        await h.conn.async_poll()
        assert h.conn.work_mode.state == "general"
        goodwe_bank.unsupported.add(WM_ADDR)                              # wyjątek Modbus = rejestr nieobsługiwany
        h.clock.advance(WM_POLL_S + 1)
        await h.conn.async_poll()
        stopped_at = _sim_reqs(goodwe_udp_sim)
        goodwe_bank.unsupported.discard(WM_ADDR)                          # nawet gdyby znów odpowiadał
        for _ in range(3):
            h.clock.advance(WM_POLL_S + 1)
            await h.conn.async_poll()
        assert _sim_reqs(goodwe_udp_sim) == stopped_at
        assert h.conn.work_mode.state == "general"
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_work_mode_stops_after_one_garbage_reply_without_conflict(make_hass, goodwe_udp_sim, goodwe_bank):
    h, _spy = await _start(make_hass, goodwe_udp_sim)
    try:
        await h.conn.async_poll()
        stray0 = h.conn.stats.stray
        goodwe_bank.unreadable.add(WM_ADDR)                               # odpowiedź złej długości
        h.clock.advance(WM_POLL_S + 1)
        await h.conn.async_poll()
        stopped_at = _sim_reqs(goodwe_udp_sim)
        goodwe_bank.unreadable.discard(WM_ADDR)
        for _ in range(4):
            h.clock.advance(WM_POLL_S + 1)
            await h.conn.async_poll()
        assert _sim_reqs(goodwe_udp_sim) == stopped_at                    # zero kolejnych żądań 47000
        # jedna nieudana próba odczytu rozdzielanego = co najwyżej dwie ramki obce (po jednej na próbę
        # transportu); ograniczenie do jednej wymagałoby parametru `tries` w core/modbus/client.py
        assert h.conn.stats.stray - stray0 <= 2
        assert h.conn.monitor.state != "conflict"                         # brak fałszywego konfliktu
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_work_mode_three_silent_timeouts_stop_reading(make_hass, goodwe_udp_sim, goodwe_bank):
    h, _spy = await _start(make_hass, goodwe_udp_sim)
    state = {"timeout": False, "calls": 0}
    _silent_timeouts_on_wm(h.conn, state)
    try:
        await h.conn.async_poll()
        state["timeout"] = True
        for _ in range(3):
            h.clock.advance(WM_POLL_S + 1)
            await h.conn.async_poll()
        assert state["calls"] == 3
        for _ in range(2):
            h.clock.advance(WM_POLL_S + 1)
            await h.conn.async_poll()
        assert state["calls"] == 3                                        # po trzech ciszach: koniec odczytów
        assert h.conn.work_mode.state == "general"
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_work_mode_single_silent_timeout_does_not_stop(make_hass, goodwe_udp_sim, goodwe_bank):
    h, _spy = await _start(make_hass, goodwe_udp_sim)
    state = {"timeout": False, "calls": 0}
    _silent_timeouts_on_wm(h.conn, state)
    try:
        await h.conn.async_poll()
        state["timeout"] = True
        h.clock.advance(WM_POLL_S + 1)
        await h.conn.async_poll()
        assert state["calls"] == 1
        state["timeout"] = False
        goodwe_bank.poke(WM_ADDR, 3)
        h.clock.advance(WM_POLL_S + 1)
        await h.conn.async_poll()
        assert h.conn.work_mode.state == "other:3"                        # czytanie trwa dalej
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_work_mode_timeout_counter_resets_on_success(make_hass, goodwe_udp_sim, goodwe_bank):
    h, _spy = await _start(make_hass, goodwe_udp_sim)
    state = {"timeout": False, "calls": 0}
    _silent_timeouts_on_wm(h.conn, state)
    try:
        await h.conn.async_poll()
        state["timeout"] = True
        for _ in range(2):
            h.clock.advance(WM_POLL_S + 1)
            await h.conn.async_poll()
        state["timeout"] = False
        h.clock.advance(WM_POLL_S + 1)                                    # sukces zeruje licznik
        await h.conn.async_poll()
        state["timeout"] = True
        for _ in range(2):
            h.clock.advance(WM_POLL_S + 1)
            await h.conn.async_poll()
        assert state["calls"] == 4
        state["timeout"] = False
        goodwe_bank.poke(WM_ADDR, 3)
        h.clock.advance(WM_POLL_S + 1)
        await h.conn.async_poll()
        assert h.conn.work_mode.state == "other:3"                        # nadal czytamy
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
    no_wm = _without_work_mode_profile()
    h, spy = await _start(make_hass, goodwe_udp_sim, profile=no_wm)
    try:
        assert no_wm.work_mode is None
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
