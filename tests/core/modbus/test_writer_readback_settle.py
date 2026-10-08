"""Pisarz: odczyt zwrotny równy wartości sprzed zapisu nie jest od razu odmową — odczekanie i ponowny
odczyt (falownik stosuje nastawę z opóźnieniem); pisarz nie ponawia zapisu (jedna próba na klucz)."""
from dataclasses import replace

import pytest

from custom_components.volcast.core.modbus import writer as writer_mod
from custom_components.volcast.core.modbus.client import RegisterClient
from custom_components.volcast.core.modbus.writer import RegisterWriter
from custom_components.volcast.core.registers import RegisterWrite
from custom_components.volcast.core.transports.base import ModbusException, RequestTimeout
from custom_components.volcast.core.write_sequence import DENIED, ERROR, OK, OK_ADJUSTED


class _Lagging:
    """Bank rejestrów, w którym zapis „dochodzi” po `lag` odczytach obejmujących rejestr."""

    def __init__(self, regs, *, kind="goodwe_udp", lag=0, apply=lambda addr, v: v, write_exc=None,
                 lose_after_write=0):
        self.kind = kind
        self.regs = dict(regs)
        self.lag = lag
        self.apply = apply
        self.write_exc = write_exc
        self.lose_after_write = lose_after_write
        self._old: dict[int, list] = {}
        self._lose = 0
        self.writes = 0
        self.reads = 0

    async def read(self, addr, count, *, tries=None):
        self.reads += 1
        if self._lose > 0:
            self._lose -= 1
            raise RequestTimeout(silent=True)
        out = []
        for a in range(addr, addr + count):
            old = self._old.get(a)
            if old is not None and old[1] > 0:
                old[1] -= 1
                out.append(old[0])
            else:
                out.append(self.regs.get(a, 0))
        return out

    async def write(self, addr, values, *, function, on_send=None):
        self.writes += 1
        if on_send:
            on_send()
        self._old[addr] = [self.regs.get(addr, 0), self.lag]
        self.regs[addr] = self.apply(addr, values[0])
        self._lose = self.lose_after_write
        if self.write_exc is not None:
            raise self.write_exc


def _writer(t, profile):
    return RegisterWriter(RegisterClient(t, profile), profile)


def _settles(sleeps, settle=1.5):
    return [d for d in sleeps if d == settle]


@pytest.mark.asyncio
@pytest.mark.parametrize("lag", [1, 2])
async def test_delayed_apply_is_ok_after_settle_reread(goodwe_profile, writer_sleeps, lag):
    t = _Lagging({45356: 10}, lag=lag)
    assert await _writer(t, goodwe_profile).async_write(RegisterWrite("soc_min", 45356, 20)) == OK
    assert t.writes == 1
    assert _settles(writer_sleeps) == [1.5] * lag


@pytest.mark.asyncio
async def test_never_applied_is_denied_after_exactly_two_rereads(goodwe_profile, writer_sleeps):
    t = _Lagging({47512: 0}, apply=lambda a, v: 0)
    w = _writer(t, goodwe_profile)
    reads_before_write = []
    orig = t.write

    async def spy(*a, **k):
        reads_before_write.append(t.reads)
        await orig(*a, **k)
    t.write = spy
    assert await w.async_write(RegisterWrite("power_w", 47512, 1500)) == DENIED
    assert t.writes == 1
    assert _settles(writer_sleeps) == [1.5, 1.5] == [d for d in writer_sleeps if d != writer_mod.READ_RETRY_BACKOFF_S]
    assert t.reads - reads_before_write[0] == 1 + writer_mod.READBACK_SETTLE_READS == 3


@pytest.mark.asyncio
async def test_settle_reread_with_safe_clamp_is_adjusted(goodwe_profile, writer_sleeps):
    t = _Lagging({47512: 0}, lag=1, apply=lambda a, v: min(v, 1000))
    out = await _writer(t, goodwe_profile).async_write(RegisterWrite("power_w", 47512, 1500))
    assert out == OK_ADJUSTED and out.actual == 1000.0 and t.writes == 1
    assert _settles(writer_sleeps) == [1.5]


@pytest.mark.asyncio
async def test_settle_reread_with_other_mode_is_error(goodwe_profile, writer_sleeps):
    t = _Lagging({47511: 1}, lag=1, apply=lambda a, v: 2)
    assert await _writer(t, goodwe_profile).async_write(RegisterWrite("mode", 47511, 10)) == ERROR
    assert t.writes == 1


@pytest.mark.asyncio
async def test_echo_exception_with_unchanged_register_is_denied_without_settle(goodwe_profile, writer_sleeps):
    # Urządzenie jawnie odrzuciło zapis (wyjątek ≠ 2, 5) — to nie opóźnione zastosowanie.
    t = _Lagging({47511: 1}, apply=lambda a, v: 1, write_exc=ModbusException(4))
    assert await _writer(t, goodwe_profile).async_write(RegisterWrite("mode", 47511, 10)) == DENIED
    assert _settles(writer_sleeps) == [] and t.writes == 1


@pytest.mark.asyncio
async def test_lost_echo_with_unchanged_register_is_error_without_settle(goodwe_profile, writer_sleeps):
    t = _Lagging({47511: 1}, apply=lambda a, v: 1, write_exc=RequestTimeout(silent=True))
    assert await _writer(t, goodwe_profile).async_write(RegisterWrite("mode", 47511, 10)) == ERROR
    assert _settles(writer_sleeps) == [] and t.writes == 1


@pytest.mark.asyncio
async def test_settle_reread_lost_is_error_not_denied(goodwe_profile, writer_sleeps):
    t = _Lagging({47512: 0}, apply=lambda a, v: 0)
    w = _writer(t, goodwe_profile)
    orig = t.read
    state = {"after_write": 0}

    async def read(addr, count, *, tries=None):
        if t.writes:
            state["after_write"] += 1
            if state["after_write"] > 1:                    # pierwszy odczyt zwrotny przechodzi, potem cisza
                t.reads += 1
                raise RequestTimeout(silent=True)
        return await orig(addr, count, tries=tries)
    t.read = read
    assert await w.async_write(RegisterWrite("power_w", 47512, 1500)) == ERROR
    assert t.writes == 1


@pytest.mark.asyncio
async def test_settle_time_comes_from_profile(goodwe_profile, writer_sleeps):
    gw = replace(goodwe_profile, readback_settle_s=0.4)
    t = _Lagging({45356: 10}, lag=1)
    assert await _writer(t, gw).async_write(RegisterWrite("soc_min", 45356, 20)) == OK
    assert _settles(writer_sleeps, 0.4) == [0.4] and _settles(writer_sleeps) == []


@pytest.mark.asyncio
async def test_profile_without_settle_field_uses_default(deye_profile, writer_sleeps):
    assert "readback_settle_s" not in deye_profile.raw["write_policy"]
    t = _Lagging({148: 100}, kind="modbus_tcp", lag=1)
    assert await _writer(t, deye_profile).async_write(RegisterWrite("tou.1.start", 148, 130)) == OK
    assert _settles(writer_sleeps) == [1.5] and t.writes == 1


@pytest.mark.asyncio
async def test_settle_on_udp_sim_single_write_frame(goodwe_writer, goodwe_bank, goodwe_udp_sim, writer_sleeps):
    # Symulator: rejestr bez zmian → DENIED dopiero po ponownych odczytach; jedna ramka FC 6.
    goodwe_bank.ignore_writes.add(47512)
    assert await goodwe_writer.async_write(RegisterWrite("power_w", 47512, 3000)) == DENIED
    assert [e[0] for e in goodwe_udp_sim.log].count(0x06) == 1
    assert _settles(writer_sleeps) == [1.5, 1.5]
