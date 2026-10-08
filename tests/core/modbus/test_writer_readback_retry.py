"""Pisarz: ponowiony ODCZYT (przed zapisem i zwrotny) przy chwilowej ciszy — pisarz nigdy nie ponawia zapisu."""
import asyncio

import pytest

from custom_components.volcast.core.modbus import writer as writer_mod
from custom_components.volcast.core.modbus.client import RegisterClient
from custom_components.volcast.core.modbus.writer import RegisterWriter
from custom_components.volcast.core.registers import RegisterWrite
from custom_components.volcast.core.transports.base import LinkDown, ModbusException, RequestTimeout
from custom_components.volcast.core.write_sequence import DENIED, ERROR, OK, OK_ADJUSTED, UNSUPPORTED

PRE = object()          # w skrypcie odczytu: wartość sprzed zapisu niezależnie od rejestru
# GoodWe UDP (łącze bez korelacji odpowiedzi): dwa odczyty przed zapisem o różnej długości (`views.py`)
PRE_READS = 2


def _lost():
    return RequestTimeout(silent=True)


class _Scripted:
    """Transport z bankiem rejestrów; kolejne odczyty wg skryptu (wyjątek = porażka, None = bank).
    Odczyt bloku zwraca słowa banku (rejestr spoza banku = 0)."""
    kind = "goodwe_udp"

    def __init__(self, regs, reads=(), *, write_exc=None, apply=lambda addr, v: v):
        self.regs = dict(regs)
        self.pre = dict(regs)
        self.script = list(reads)
        self.write_exc = write_exc
        self.apply = apply
        self.log: list[tuple] = []
        self.read_tries: list = []
        self.writes = 0

    async def read(self, addr, count, *, tries=None):
        self.read_tries.append(tries)
        self.log.append(("read", addr, count))
        await asyncio.sleep(0)                      # punkt przełączenia — zamek musi trzymać
        step = self.script.pop(0) if self.script else None
        if isinstance(step, Exception):
            raise step
        bank = self.pre if step is PRE else self.regs
        return [bank.get(a, 0) for a in range(addr, addr + count)]

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
    t = _Scripted({47512: 0}, [None] * PRE_READS + [_lost() for _ in range(lost)])
    assert await _writer(t, goodwe_profile).async_write(RegisterWrite("power_w", 47512, 1500)) == OK
    assert t.writes == 1 and t.reads == PRE_READS + 1 + lost
    assert writer_sleeps == [writer_mod.READ_RETRY_BACKOFF_S] * lost


@pytest.mark.asyncio
async def test_readback_always_lost_is_error_bounded_reads_one_write(goodwe_profile, writer_sleeps):
    t = _Scripted({47512: 0}, [None] * PRE_READS + [_lost() for _ in range(50)])
    assert await _writer(t, goodwe_profile).async_write(RegisterWrite("power_w", 47512, 1500)) == ERROR
    assert t.writes == 1
    assert t.reads == PRE_READS + 1 + writer_mod.READ_RETRIES   # przed + zwrotny + ponowienia
    # pierwszy odczyt zwrotny z pełną liczbą prób transportu, ponowienia po jednej próbie
    assert t.read_tries == [None] * (PRE_READS + 1) + [1] * writer_mod.READ_RETRIES
    assert writer_sleeps == [writer_mod.READ_RETRY_BACKOFF_S] * writer_mod.READ_RETRIES
    assert writer_mod.READ_RETRIES == 2 and 0.5 <= writer_mod.READ_RETRY_BACKOFF_S <= 1.0


@pytest.mark.asyncio
async def test_readback_link_errors_are_retried(goodwe_profile):
    t = _Scripted({47511: 1}, [None] * PRE_READS + [LinkDown("down"), ModbusException(6)])
    assert await _writer(t, goodwe_profile).async_write(RegisterWrite("mode", 47511, 10)) == OK
    assert t.writes == 1 and t.reads == PRE_READS + 3


@pytest.mark.asyncio
async def test_readback_pre_write_value_after_timeout_is_denied_when_echo_confirmed(goodwe_profile):
    t = _Scripted({47512: 0}, [None] * PRE_READS + [_lost()], apply=lambda a, v: 0)
    assert await _writer(t, goodwe_profile).async_write(RegisterWrite("power_w", 47512, 1500)) == DENIED
    assert t.writes == 1


@pytest.mark.asyncio
async def test_readback_pre_write_value_after_timeout_without_echo_is_error(goodwe_profile):
    t = _Scripted({47512: 0}, [None] * PRE_READS + [_lost(), PRE], write_exc=_lost())
    assert await _writer(t, goodwe_profile).async_write(RegisterWrite("power_w", 47512, 1500)) == ERROR
    assert t.writes == 1


@pytest.mark.asyncio
async def test_readback_safe_deviation_after_timeout_is_adjusted(goodwe_profile):
    t = _Scripted({47512: 0}, [None] * PRE_READS + [_lost()], apply=lambda a, v: min(v, 1000))
    out = await _writer(t, goodwe_profile).async_write(RegisterWrite("power_w", 47512, 1500))
    assert out == OK_ADJUSTED and out.actual == 1000.0 and t.writes == 1


# ── odczyt przed zapisem ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pre_read_lost_once_then_write_proceeds(goodwe_profile, writer_sleeps):
    t = _Scripted({47512: 0}, [_lost()])
    assert await _writer(t, goodwe_profile).async_write(RegisterWrite("power_w", 47512, 1500)) == OK
    assert t.writes == 1 and t.reads == 1 + PRE_READS + 1 and t.regs[47512] == 1500
    assert writer_sleeps == [writer_mod.READ_RETRY_BACKOFF_S]


@pytest.mark.asyncio
async def test_pre_read_always_lost_is_error_nothing_sent(goodwe_profile):
    t = _Scripted({47512: 0}, [_lost() for _ in range(50)])
    assert await _writer(t, goodwe_profile).async_write(RegisterWrite("power_w", 47512, 1500)) == ERROR
    assert t.writes == 0 and t.reads == 1 + writer_mod.READ_RETRIES


@pytest.mark.asyncio
async def test_pre_read_exception_2_confirmed_without_retry_delays(goodwe_profile, writer_sleeps):
    # blok EMS: wyjątek 2 → sam rejestr: wyjątek 2 → blok rozdzielający → sam rejestr: wyjątek 2
    t = _Scripted({47510: 0}, [ModbusException(2), ModbusException(2), None, ModbusException(2)])
    assert await _writer(t, goodwe_profile).async_write(RegisterWrite("export_limit_w", 47510, 100)) == UNSUPPORTED
    assert t.writes == 0 and t.reads == 4 and writer_sleeps == []
    assert [e[1:] for e in t.log] == [(47509, 4), (47510, 1), (35000, 33), (47510, 1)]


@pytest.mark.asyncio
async def test_separator_with_exception_falls_back_to_next_candidate(goodwe_profile, writer_sleeps):
    # soc_max tylko pojedynczo: rozdziela blok EMS; gdy ten odpowie wyjątkiem — następny znany blok
    t = _Scripted({}, [ModbusException(2), ModbusException(2), None, ModbusException(2)])
    assert await _writer(t, goodwe_profile).async_write(RegisterWrite("soc_max", 47760, 95)) == UNSUPPORTED
    assert [e[1:] for e in t.log] == [(47760, 1), (47509, 4), (45353, 4), (47760, 1)] and t.writes == 0


@pytest.mark.asyncio
async def test_pre_read_exception_2_not_repeated_is_error_nothing_sent(goodwe_profile, writer_sleeps):
    t = _Scripted({47510: 0}, [ModbusException(2), ModbusException(2), None, None])
    assert await _writer(t, goodwe_profile).async_write(RegisterWrite("export_limit_w", 47510, 100)) == ERROR
    assert t.writes == 0 and writer_sleeps == []


# ── powrót do trybu bazowego ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_restore_pre_read_lost_once_register_already_equal_sends_nothing(goodwe_profile):
    t = _Scripted({47511: 1}, [_lost()])
    assert await _writer(t, goodwe_profile).async_write_restore(RegisterWrite("mode", 47511, 1)) == OK
    assert t.writes == 0 and t.reads == 1 + PRE_READS


@pytest.mark.asyncio
async def test_restore_readback_lost_once_is_ok_with_one_write(goodwe_profile):
    t = _Scripted({47511: 10}, [None] * PRE_READS + [_lost()])
    assert await _writer(t, goodwe_profile).async_write_restore(RegisterWrite("mode", 47511, 1)) == OK
    assert t.writes == 1 and t.reads == PRE_READS + 2


# ── zamek: ponowienia nie wpuszczają innego zapisu ─────────────────────────


@pytest.mark.asyncio
async def test_retries_hold_the_writer_lock(goodwe_profile):
    t = _Scripted({47511: 1, 47512: 0}, [None] * PRE_READS + [_lost(), _lost()])
    w = _writer(t, goodwe_profile)
    a, b = await asyncio.gather(w.async_write(RegisterWrite("mode", 47511, 10)),
                                w.async_write(RegisterWrite("power_w", 47512, 1500)))
    assert (a, b) == (OK, OK)
    # kolejne odczyty na przemian blokiem EMS i samym rejestrem (różna długość odpowiedzi)
    assert t.log == [("read", 47509, 4), ("read", 47511, 1), ("write", 47511),
                     ("read", 47509, 4), ("read", 47511, 1), ("read", 47509, 4),
                     ("read", 47512, 1), ("read", 47509, 4), ("write", 47512), ("read", 47512, 1)]


# ── symulator UDP: odczyt zwrotny ginie w całości pierwszej próby ─────────


@pytest.mark.asyncio
async def test_sim_readback_lost_then_answered_is_ok_single_write_frame(goodwe_writer, goodwe_bank,
                                                                         goodwe_udp_sim, sim_faults):
    sim_faults.drop_after_write = 2               # = read_tries transportu: pierwszy odczyt zwrotny ginie
    assert await goodwe_writer.async_write(RegisterWrite("power_w", 47512, 1500)) == OK
    assert [e[0] for e in goodwe_udp_sim.log].count(0x06) == 1
    assert goodwe_bank.read(47512, 1) == [1500]


# ── przerwa przed ponowieniem: nie krótsza niż czekanie transportu na ponowne połączenie ──


def _stream_writer(profile, **cfg):
    from custom_components.volcast.core.transports.base import TransportConfig
    from custom_components.volcast.core.transports.factory import make_transport
    t = make_transport(TransportConfig(kind="modbus_tcp", host="127.0.0.1", port=1502, unit=1, **cfg),
                       allow_loopback=True)
    return RegisterWriter(RegisterClient(t, profile), profile)


def test_stream_transport_retry_waits_out_doubling_reconnect_backoff(deye_profile):
    # Transport strumieniowy po nieudanym połączeniu czeka backoff_min_s, potem dwa razy dłużej.
    assert _stream_writer(deye_profile).read_retry_delays() == pytest.approx((1.1, 2.1))
    assert _stream_writer(deye_profile, backoff_min_s=1.5).read_retry_delays() == pytest.approx((1.6, 3.1))


def test_udp_and_unknown_transport_retry_delays(goodwe_profile):
    assert _writer(_Scripted({}), goodwe_profile).read_retry_delays() == (writer_mod.READ_RETRY_BACKOFF_S,) * 2
    other = _Scripted({})
    other.kind = "something_else"                 # bez wiedzy o czekaniu transportu — stała ≥ 1,1 s
    assert all(d >= 1.1 for d in _writer(other, goodwe_profile).read_retry_delays())


@pytest.mark.asyncio
async def test_retries_sleep_the_derived_delays(goodwe_profile, writer_sleeps, monkeypatch):
    t = _Scripted({47511: 1}, [LinkDown("connection closed by peer"), LinkDown("connection failed")])
    w = _writer(t, goodwe_profile)
    monkeypatch.setattr(w, "read_retry_delays", lambda: (1.1, 2.1))
    assert await w.async_write_restore(RegisterWrite("mode", 47511, 1)) == OK
    assert writer_sleeps == [1.1, 2.1] and t.writes == 0


# ── gałęzie z przeglądu ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retried_pre_read_word_is_the_base_for_bit_fields(deye_profile):
    # Pierwszy odczyt przed zapisem ginie; drugi przynosi słowo z bitem właściciela — złożenie na nim.
    t = _Scripted({172: 0b100}, [_lost()])
    t.kind = "modbus_rtu"                         # Deye: łącze bez widoków (jeden odczyt przed zapisem)
    assert await _writer(t, deye_profile).async_write(RegisterWrite("tou.1.grid_charge", 172, 0b01)) == OK
    assert t.regs[172] == 0b101 and t.writes == 1


@pytest.mark.asyncio
async def test_readback_timeout_then_exception_2_is_error_without_more_reads(goodwe_profile, writer_sleeps):
    t = _Scripted({47512: 0}, [None] * PRE_READS + [_lost(), ModbusException(2), None])
    assert await _writer(t, goodwe_profile).async_write(RegisterWrite("power_w", 47512, 1500)) == ERROR
    assert t.writes == 1 and t.reads == PRE_READS + 2 and len(writer_sleeps) == 1


@pytest.mark.asyncio
async def test_pre_read_timeout_then_confirmed_exception_2_is_unsupported_nothing_sent(goodwe_profile):
    # blok EMS ginie; ponowienie samym rejestrem: wyjątek 2, potwierdzony po bloku rozdzielającym
    t = _Scripted({47510: 0}, [_lost(), ModbusException(2), None, ModbusException(2)])
    assert await _writer(t, goodwe_profile).async_write(RegisterWrite("export_limit_w", 47510, 100)) == UNSUPPORTED
    assert t.writes == 0 and t.reads == 4


@pytest.mark.asyncio
async def test_pre_read_timeout_then_unconfirmed_exception_2_is_error_nothing_sent(goodwe_profile):
    t = _Scripted({47510: 0}, [_lost(), ModbusException(2), None, None])
    assert await _writer(t, goodwe_profile).async_write(RegisterWrite("export_limit_w", 47510, 100)) == ERROR
    assert t.writes == 0


@pytest.mark.asyncio
async def test_pre_read_retried_echo_5_and_unchanged_register_is_error_not_denied(goodwe_profile):
    t = _Scripted({47511: 1}, [_lost()], write_exc=ModbusException(5), apply=lambda a, v: 1)
    assert await _writer(t, goodwe_profile).async_write(RegisterWrite("mode", 47511, 10)) == ERROR
    assert t.writes == 1
