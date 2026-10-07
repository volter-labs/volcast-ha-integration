"""Pisarz: ponowiony ODCZYT (przed zapisem i zwrotny) przy chwilowej ciszy — ramka zapisu nigdy dwa razy."""
import asyncio

import pytest

from custom_components.volcast.core.modbus import writer as writer_mod
from custom_components.volcast.core.modbus.client import RegisterClient
from custom_components.volcast.core.modbus.writer import RegisterWriter
from custom_components.volcast.core.registers import RegisterWrite
from custom_components.volcast.core.transports.base import LinkDown, ModbusException, RequestTimeout
from custom_components.volcast.core.write_sequence import DENIED, ERROR, OK, OK_ADJUSTED, UNSUPPORTED

PRE = object()          # w skrypcie odczytu: wartość sprzed zapisu niezależnie od rejestru


def _lost():
    return RequestTimeout(silent=True)


class _Scripted:
    """Transport z bankiem rejestrów; kolejne odczyty wg skryptu (wyjątek = porażka, None = bank)."""
    kind = "goodwe_udp"

    def __init__(self, regs, reads=(), *, write_exc=None, apply=lambda addr, v: v):
        self.regs = dict(regs)
        self.pre = dict(regs)
        self.script = list(reads)
        self.write_exc = write_exc
        self.apply = apply
        self.log: list[tuple[str, int]] = []
        self.read_tries: list = []
        self.writes = 0

    async def read(self, addr, count, *, tries=None):
        self.read_tries.append(tries)
        self.log.append(("read", addr))
        await asyncio.sleep(0)                      # punkt przełączenia — zamek musi trzymać
        step = self.script.pop(0) if self.script else None
        if isinstance(step, Exception):
            raise step
        if step is PRE:
            return [self.pre[addr]]
        return [self.regs[addr]]

    async def write(self, addr, values, *, function, on_send=None):
        self.writes += 1
        self.log.append(("write", addr))
        if on_send:
            on_send()
        self.regs[addr] = self.apply(addr, values[0])
        if self.write_exc is not None:
            raise self.write_exc

    @property
    def reads(self):
        return len(self.read_tries)


def _writer(t, profile):
    return RegisterWriter(RegisterClient(t, profile), profile)


# ── odczyt zwrotny ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("lost", [1, 2])
async def test_readback_timeout_then_requested_is_ok_with_one_write(goodwe_profile, writer_sleeps, lost):
    t = _Scripted({47512: 0}, [None] + [_lost() for _ in range(lost)])
    assert await _writer(t, goodwe_profile).async_write(RegisterWrite("power_w", 47512, 1500)) == OK
    assert t.writes == 1 and t.reads == 2 + lost
    assert writer_sleeps == [writer_mod.READ_RETRY_BACKOFF_S] * lost


@pytest.mark.asyncio
async def test_readback_always_lost_is_error_bounded_reads_one_write(goodwe_profile, writer_sleeps):
    t = _Scripted({47512: 0}, [None] + [_lost() for _ in range(50)])
    assert await _writer(t, goodwe_profile).async_write(RegisterWrite("power_w", 47512, 1500)) == ERROR
    assert t.writes == 1
    assert t.reads == 2 + writer_mod.READ_RETRIES               # przed + zwrotny + ponowienia
    # pierwszy odczyt zwrotny z pełną liczbą prób transportu, ponowienia po jednej próbie
    assert t.read_tries == [None, None] + [1] * writer_mod.READ_RETRIES
    assert writer_sleeps == [writer_mod.READ_RETRY_BACKOFF_S] * writer_mod.READ_RETRIES
    assert writer_mod.READ_RETRIES == 2 and 0.5 <= writer_mod.READ_RETRY_BACKOFF_S <= 1.0


@pytest.mark.asyncio
async def test_readback_link_errors_are_retried(goodwe_profile):
    t = _Scripted({47511: 1}, [None, LinkDown("down"), ModbusException(6)])
    assert await _writer(t, goodwe_profile).async_write(RegisterWrite("mode", 47511, 10)) == OK
    assert t.writes == 1 and t.reads == 4


@pytest.mark.asyncio
async def test_readback_pre_write_value_after_timeout_is_denied_when_echo_confirmed(goodwe_profile):
    t = _Scripted({47512: 0}, [None, _lost()], apply=lambda a, v: 0)
    assert await _writer(t, goodwe_profile).async_write(RegisterWrite("power_w", 47512, 1500)) == DENIED
    assert t.writes == 1


@pytest.mark.asyncio
async def test_readback_pre_write_value_after_timeout_without_echo_is_error(goodwe_profile):
    t = _Scripted({47512: 0}, [None, _lost(), PRE], write_exc=_lost())
    assert await _writer(t, goodwe_profile).async_write(RegisterWrite("power_w", 47512, 1500)) == ERROR
    assert t.writes == 1


@pytest.mark.asyncio
async def test_readback_safe_deviation_after_timeout_is_adjusted(goodwe_profile):
    t = _Scripted({47512: 0}, [None, _lost()], apply=lambda a, v: min(v, 1000))
    out = await _writer(t, goodwe_profile).async_write(RegisterWrite("power_w", 47512, 1500))
    assert out == OK_ADJUSTED and out.actual == 1000.0 and t.writes == 1


# ── odczyt przed zapisem ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pre_read_lost_once_then_write_proceeds(goodwe_profile, writer_sleeps):
    t = _Scripted({47512: 0}, [_lost()])
    assert await _writer(t, goodwe_profile).async_write(RegisterWrite("power_w", 47512, 1500)) == OK
    assert t.writes == 1 and t.reads == 3 and t.regs[47512] == 1500
    assert writer_sleeps == [writer_mod.READ_RETRY_BACKOFF_S]


@pytest.mark.asyncio
async def test_pre_read_always_lost_is_error_nothing_sent(goodwe_profile):
    t = _Scripted({47512: 0}, [_lost() for _ in range(50)])
    assert await _writer(t, goodwe_profile).async_write(RegisterWrite("power_w", 47512, 1500)) == ERROR
    assert t.writes == 0 and t.reads == 1 + writer_mod.READ_RETRIES


@pytest.mark.asyncio
async def test_pre_read_exception_2_not_retried(goodwe_profile, writer_sleeps):
    t = _Scripted({47510: 0}, [ModbusException(2)])
    assert await _writer(t, goodwe_profile).async_write(RegisterWrite("export_limit_w", 47510, 100)) == UNSUPPORTED
    assert t.writes == 0 and t.reads == 1 and writer_sleeps == []


# ── powrót do trybu bazowego ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_restore_pre_read_lost_once_register_already_equal_sends_nothing(goodwe_profile):
    t = _Scripted({47511: 1}, [_lost()])
    assert await _writer(t, goodwe_profile).async_write_restore(RegisterWrite("mode", 47511, 1)) == OK
    assert t.writes == 0 and t.reads == 2


@pytest.mark.asyncio
async def test_restore_readback_lost_once_is_ok_with_one_write(goodwe_profile):
    t = _Scripted({47511: 10}, [None, _lost()])
    assert await _writer(t, goodwe_profile).async_write_restore(RegisterWrite("mode", 47511, 1)) == OK
    assert t.writes == 1 and t.reads == 3


# ── zamek: ponowienia nie wpuszczają innego zapisu ─────────────────────────


@pytest.mark.asyncio
async def test_retries_hold_the_writer_lock(goodwe_profile):
    t = _Scripted({47511: 1, 47512: 0}, [None, _lost(), _lost()])
    w = _writer(t, goodwe_profile)
    a, b = await asyncio.gather(w.async_write(RegisterWrite("mode", 47511, 10)),
                                w.async_write(RegisterWrite("power_w", 47512, 1500)))
    assert (a, b) == (OK, OK)
    assert t.log == [("read", 47511), ("write", 47511), ("read", 47511), ("read", 47511), ("read", 47511),
                     ("read", 47512), ("write", 47512), ("read", 47512)]


# ── symulator UDP: odczyt zwrotny ginie w całości pierwszej próby ─────────


@pytest.mark.asyncio
async def test_sim_readback_lost_then_answered_is_ok_single_write_frame(goodwe_writer, goodwe_bank,
                                                                         goodwe_udp_sim, sim_faults):
    sim_faults.drop_after_write = 2               # = read_tries transportu: pierwszy odczyt zwrotny ginie
    assert await goodwe_writer.async_write(RegisterWrite("power_w", 47512, 1500)) == OK
    assert [e[0] for e in goodwe_udp_sim.log].count(0x06) == 1
    assert goodwe_bank.read(47512, 1) == [1500]
