"""Przypadki wspólne dla transportów strumieniowych (Modbus TCP, RTU przez bramkę TCP, Solarman V5).

Każdy plik testowy transportu importuje te funkcje (pytest zbiera je w jego przestrzeni nazw)
i definiuje fixture `link` z opisem symulatora i rejestrów.
"""
from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass

import pytest

from custom_components.volcast.core.transports.base import (
    LinkDown, ModbusException, RequestTimeout, TransportConfig)
from custom_components.volcast.core.transports.factory import make_transport

from .helpers import FakeClock


@dataclass
class Link:
    sim: object
    bank: object
    faults: object
    kind: str
    unit: int
    logger_serial: int | None
    a: tuple[int, int, list[int]]            # odczyt A: adres, liczba, oczekiwane słowa
    b: tuple[int, int, list[int]]            # odczyt B (ta sama długość co A, inna wartość)
    write: tuple[int, int]                   # rejestr i wartość do zapisu
    function: int

    def cfg(self, **kw) -> TransportConfig:
        base = dict(kind=self.kind, host=self.sim.host, port=self.sim.port, unit=self.unit,
                    timeout_s=0.3, gap_s=0.0, logger_serial=self.logger_serial)
        base.update(kw)
        return TransportConfig(**base)

    def open(self, **kw):
        extra = {k: kw.pop(k) for k in ("clock", "sleep") if k in kw}
        return make_transport(self.cfg(**kw), allow_loopback=True, **extra)


def link_factory(sim, bank, faults, kind, unit, logger_serial, a, b, write, function) -> Link:
    return Link(sim, bank, faults, kind, unit, logger_serial, a, b, write, function)


async def _read(t, spec):
    addr, count, _ = spec
    return await t.read(addr, count)


@pytest.mark.asyncio
async def test_read_block(link):
    t = link.open()
    try:
        assert link.sim.clients == 0                   # połączenie leniwe
        assert t.kind == link.kind
        assert await _read(t, link.a) == link.a[2]
        assert await _read(t, link.b) == link.b[2]
        assert link.sim.clients == 1 and t.stats.requests == 2
        assert t.stats.last_ok_mono is not None
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_late_reply_same_length_after_timeout_not_accepted(link):
    link.faults.delay_s = 0.45
    link.faults.delay_only_next = 1
    t = link.open(read_tries=1)
    try:
        with pytest.raises(RequestTimeout):
            await _read(t, link.a)
        assert await _read(t, link.b) == link.b[2]       # nie wartość odczytu A
        assert t.stats.channel_resets >= 1 and t.stats.peer_resets == 0
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_tcp_write_no_resend_resets_channel(link):
    link.faults.mute_write_echo = 1
    addr, value = link.write
    sent = []
    t = link.open()
    try:
        with pytest.raises(RequestTimeout):
            await t.write(addr, [value], function=link.function, on_send=lambda: sent.append(1))
        assert link.bank.writes == [(addr, value)]
        assert sent == [1]
        assert t.stats.channel_resets == 1
        assert await _read(t, link.a) == link.a[2]       # kolejne żądanie na nowym połączeniu
        assert link.sim.clients == 2
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_write_echo_accepted(link):
    addr, value = link.write
    t = link.open()
    try:
        await t.write(addr, [value], function=link.function)
        assert link.bank.writes == [(addr, value)]
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_wrong_echo_value_never_accepted(link):
    link.faults.wrong_echo_value = True
    addr, value = link.write
    t = link.open()
    try:
        with pytest.raises(RequestTimeout):
            await t.write(addr, [value], function=link.function)
        assert t.stats.stray >= 1 and len(link.bank.writes) == 1
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_exception_2_raised_without_retry(link):
    addr, value = link.write
    link.bank.unsupported.add(addr)
    t = link.open()
    try:
        with pytest.raises(ModbusException) as e:
            await t.write(addr, [value], function=link.function)
        assert e.value.code == 2 and link.sim.requests == 1
        with pytest.raises(ModbusException):
            await t.read(addr, 1)
        assert link.sim.requests == 2
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_mismatched_tid_is_stray(link):
    # Obca ramka (inny TID / sekwencja / jednostka) przed właściwą odpowiedzią.
    link.faults.stray_every = 1
    t = link.open()
    try:
        assert await _read(t, link.a) == link.a[2]
        assert t.stats.stray == 1 and t.stats.unsolicited == 0
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_partial_frame_reassembled(link):
    link.faults.chunked = True
    t = link.open()
    try:
        assert await _read(t, link.a) == link.a[2]
        assert t.stats.stray == 0
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_peer_reset_counts_and_backoff(link):
    clock = FakeClock()
    link.faults.reset_after = 1
    t = link.open(clock=clock)
    try:
        assert await _read(t, link.a) == link.a[2]
        with pytest.raises(LinkDown):
            await _read(t, link.a)
        assert t.stats.peer_resets == 1
        clients = link.sim.clients
        with pytest.raises(LinkDown):
            await _read(t, link.a)                        # w odwrocie: bez łączenia
        assert link.sim.clients == clients
        clock.advance(1.01)
        link.faults.reset_after = 0
        assert await _read(t, link.a) == link.a[2]
        assert link.sim.clients == clients + 1 and t.stats.reconnects == 1
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_on_send_not_called_when_link_down_before_send(link):
    clock = FakeClock()
    link.faults.reset_after = 1
    addr, value = link.write
    sent = []
    t = link.open(clock=clock)
    try:
        await _read(t, link.a)
        with pytest.raises(LinkDown):
            await _read(t, link.a)                        # zerwane przez drugą stronę → odwrót
        with pytest.raises(LinkDown):
            await t.write(addr, [value], function=link.function, on_send=lambda: sent.append(1))
        assert sent == [] and link.bank.writes == []
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_backoff_doubles_and_resets_after_success(link):
    clock = FakeClock()
    t = link.open(clock=clock, connect_timeout_s=0.5)
    # Port bez nasłuchu na pętli zwrotnej: odrzucenie połączenia.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]
    dead = link.open(clock=clock, port=free_port)
    try:
        with pytest.raises(LinkDown):
            await _read(dead, link.a)
        clock.advance(0.9)
        with pytest.raises(LinkDown):
            await _read(dead, link.a)                     # 1 s odwrotu jeszcze trwa
        clock.advance(0.2)
        with pytest.raises(LinkDown):
            await _read(dead, link.a)                     # druga próba → odwrót 2 s
        clock.advance(1.5)
        with pytest.raises(LinkDown):
            await _read(dead, link.a)
        assert dead.stats.peer_resets == 0
        assert await _read(t, link.a) == link.a[2]
    finally:
        await dead.close()
        await t.close()


@pytest.mark.asyncio
async def test_oversized_stream_drops_connection(link):
    link.faults.oversize_next = 1
    t = link.open(read_tries=1)
    try:
        with pytest.raises(RequestTimeout):
            await _read(t, link.a)
        assert t.stats.stray >= 1 and t.stats.channel_resets == 1
        assert await _read(t, link.a) == link.a[2]
        assert link.sim.clients == 2
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_garbage_stream_resyncs_on_fresh_connection(link):
    link.faults.garbage_next = 1
    t = link.open(read_tries=2)
    try:
        assert await _read(t, link.a) == link.a[2]
        assert t.stats.stray >= 1 and t.stats.channel_resets >= 1
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_second_client_refused_is_link_down(link):
    link.faults.max_clients = 1
    first = link.open()
    second = link.open()
    try:
        assert await _read(first, link.a) == link.a[2]
        with pytest.raises(LinkDown):
            await _read(second, link.a)
        assert second.stats.peer_resets == 1
        assert await _read(first, link.b) == link.b[2]
    finally:
        await second.close()
        await first.close()


@pytest.mark.asyncio
async def test_close_is_idempotent_and_cancels_reader(link):
    link.faults.drop_next = 10
    t = link.open()
    task = asyncio.ensure_future(_read(t, link.a))
    await asyncio.sleep(0.05)
    await t.close()
    with pytest.raises(LinkDown):
        await asyncio.wait_for(task, 0.2)
    await t.close()
    with pytest.raises(LinkDown):
        await _read(t, link.a)
    assert t.stats.peer_resets == 0


@pytest.mark.asyncio
async def test_concurrent_requests_are_serialized(link):
    t = link.open()
    try:
        a, b = await asyncio.gather(_read(t, link.a), _read(t, link.b))
        assert a == link.a[2] and b == link.b[2]
        assert link.sim.requests == 2 and t.stats.stray == 0
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_cancel_mid_request_leaves_transport_usable(link):
    link.faults.delay_s = 0.2
    link.faults.delay_only_next = 1
    t = link.open()
    try:
        task = asyncio.ensure_future(_read(t, link.a))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await _read(t, link.b) == link.b[2]
        await asyncio.sleep(0.25)
        assert await _read(t, link.a) == link.a[2]
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_gap_enforced_between_frames(link):
    clock = FakeClock()
    t = link.open(clock=clock, sleep=clock.sleep, gap_s=0.2)
    try:
        await _read(t, link.a)
        await _read(t, link.a)
        assert clock.sleeps == [pytest.approx(0.2)]
    finally:
        await t.close()


__all__ = [n for n in dir() if n.startswith("test_")] + ["link_factory", "Link"]
