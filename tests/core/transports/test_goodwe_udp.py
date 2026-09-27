"""Transport GoodWe UDP (żądanie gołe RTU, odpowiedź AA55) na symulatorze z pętli zwrotnej."""
import asyncio

import pytest

from custom_components.volcast.core.transports.base import (
    LinkDown, ModbusException, RequestTimeout, TransportConfig)
from custom_components.volcast.core.transports.factory import make_transport
from custom_components.volcast.core.transports.modbus_frames import FC_WRITE_MULTIPLE, FC_WRITE_SINGLE

from .helpers import FakeClock


def _cfg(sim, **kw):
    return TransportConfig(kind="goodwe_udp", host=sim.host, port=sim.port, unit=0xF7,
                           timeout_s=0.3, gap_s=0.0, **kw)


@pytest.mark.asyncio
async def test_read_golden_block(goodwe_udp_sim):
    t = make_transport(_cfg(goodwe_udp_sim), allow_loopback=True)
    try:
        assert t.kind == "goodwe_udp"
        assert await t.read(47509, 4) == [0, 16000, 11, 8846]
        assert t.stats.requests == 1 and t.stats.last_ok_mono is not None
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_late_duplicate_is_stray_not_answer(goodwe_udp_sim, sim_faults):
    sim_faults.late_duplicate = True
    t = make_transport(_cfg(goodwe_udp_sim), allow_loopback=True)
    try:
        await t.read(47509, 4)
        assert await t.read(45356, 1) == [5]          # spóźniona kopia poprzedniej odpowiedzi odrzucona
        assert t.stats.stray >= 1
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_late_reply_same_length_after_timeout_not_accepted(goodwe_udp_sim, goodwe_bank, sim_faults):
    # odpowiedź na read(45356, 1) spóźnia się ponad timeout i przychodzi w trakcie read(47511, 1)
    sim_faults.delay_s = 0.45
    sim_faults.delay_only_next = 1
    t = make_transport(_cfg(goodwe_udp_sim, read_tries=1), allow_loopback=True)
    try:
        with pytest.raises(RequestTimeout):
            await t.read(45356, 1)
        assert await t.read(47511, 1) == goodwe_bank.read(47511, 1)   # nie wartość z 45356
        assert t.stats.channel_resets >= 1
        assert t.stats.timeouts == 1
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_read_retried_on_fresh_channel(goodwe_udp_sim, sim_faults):
    sim_faults.drop_next = 2
    t = make_transport(_cfg(goodwe_udp_sim, read_tries=3), allow_loopback=True)
    try:
        assert await t.read(45356, 1) == [5]
        assert t.stats.channel_resets == 2 and t.stats.consecutive_timeouts == 0
        assert goodwe_udp_sim.clients == 3                      # każda próba z nowego portu źródłowego
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_read_gives_up_after_read_tries(goodwe_udp_sim, sim_faults):
    sim_faults.drop_next = 5
    t = make_transport(_cfg(goodwe_udp_sim, read_tries=2), allow_loopback=True)
    try:
        with pytest.raises(RequestTimeout):
            await t.read(45356, 1)
        assert goodwe_udp_sim.requests == 2
        assert t.stats.timeouts == 2 and t.stats.consecutive_timeouts == 2
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_write_resends_once_when_echo_lost(goodwe_udp_sim, goodwe_bank, sim_faults):
    sim_faults.mute_write_echo = 1
    t = make_transport(_cfg(goodwe_udp_sim), allow_loopback=True)
    sent = []
    try:
        await t.write(47511, [10], function=FC_WRITE_SINGLE, on_send=lambda: sent.append(1))
        assert goodwe_bank.writes == [(47511, 10), (47511, 10)]
        assert len(sent) == 2
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_write_gives_up_after_two_sends(goodwe_udp_sim, goodwe_bank, sim_faults):
    sim_faults.mute_write_echo = 5
    t = make_transport(_cfg(goodwe_udp_sim), allow_loopback=True)
    try:
        with pytest.raises(RequestTimeout):
            await t.write(47511, [10], function=FC_WRITE_SINGLE)
        assert len(goodwe_bank.writes) == 2
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_wrong_echo_value_never_accepted(goodwe_udp_sim, goodwe_bank, sim_faults):
    sim_faults.wrong_echo_value = True
    t = make_transport(_cfg(goodwe_udp_sim), allow_loopback=True)
    try:
        with pytest.raises(RequestTimeout):
            await t.write(47511, [10], function=FC_WRITE_SINGLE)
        assert t.stats.stray >= 1
        assert len(goodwe_bank.writes) == 1                     # była odpowiedź (obca) → bez ponownej wysyłki
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_exception_2_raised_without_retry(goodwe_udp_sim, goodwe_bank):
    goodwe_bank.unsupported.add(47760)
    t = make_transport(_cfg(goodwe_udp_sim), allow_loopback=True)
    try:
        with pytest.raises(ModbusException) as e:
            await t.write(47760, [90], function=FC_WRITE_SINGLE)
        assert e.value.code == 2 and goodwe_udp_sim.requests == 1
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_read_exception_raised_without_retry(goodwe_udp_sim, goodwe_bank):
    goodwe_bank.unsupported.add(45356)
    t = make_transport(_cfg(goodwe_udp_sim), allow_loopback=True)
    try:
        with pytest.raises(ModbusException) as e:
            await t.read(45356, 1)
        assert e.value.code == 2 and goodwe_udp_sim.requests == 1
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_wrong_length_reply_is_stray(goodwe_udp_sim):
    # 47760 odpowiada ramką innej długości niż żądana (jak w nagraniu) — nigdy nie jest odpowiedzią.
    t = make_transport(_cfg(goodwe_udp_sim, read_tries=1), allow_loopback=True)
    try:
        with pytest.raises(RequestTimeout):
            await t.read(47760, 1)
        assert t.stats.stray == 1
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_foreign_unit_reply_is_stray(goodwe_udp_sim, sim_faults):
    sim_faults.stray_every = 1
    t = make_transport(_cfg(goodwe_udp_sim), allow_loopback=True)
    try:
        assert await t.read(45356, 1) == [5]
        assert t.stats.stray == 1
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_garbage_reply_is_stray(goodwe_udp_sim, sim_faults):
    sim_faults.garbage_next = 1
    t = make_transport(_cfg(goodwe_udp_sim, read_tries=2), allow_loopback=True)
    try:
        assert await t.read(45356, 1) == [5]
        assert t.stats.stray == 1
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_fc16_write(goodwe_udp_sim, goodwe_bank):
    t = make_transport(_cfg(goodwe_udp_sim), allow_loopback=True)
    try:
        await t.write(47510, [4000], function=FC_WRITE_MULTIPLE)
        assert goodwe_bank.writes == [(47510, 4000)]
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_concurrent_requests_are_serialized(goodwe_udp_sim):
    t = make_transport(_cfg(goodwe_udp_sim), allow_loopback=True)
    try:
        a, b, c = await asyncio.gather(t.read(47509, 4), t.read(45356, 1), t.read(47511, 1))
        assert a == [0, 16000, 11, 8846] and b == [5] and c == [11]
        assert goodwe_udp_sim.requests == 3 and t.stats.stray == 0
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_gap_enforced_between_frames(goodwe_udp_sim):
    clock = FakeClock()
    cfg = TransportConfig(kind="goodwe_udp", host=goodwe_udp_sim.host, port=goodwe_udp_sim.port,
                          unit=0xF7, timeout_s=0.3, gap_s=0.2)
    t = make_transport(cfg, clock=clock, sleep=clock.sleep, allow_loopback=True)
    try:
        await t.read(45356, 1)
        assert clock.sleeps == []                       # pierwsza ramka bez czekania
        await t.read(45356, 1)
        assert clock.sleeps == [pytest.approx(0.2)]
        clock.advance(0.05)
        clock.sleeps.clear()
        await t.read(45356, 1)
        assert clock.sleeps == [pytest.approx(0.15)]
        clock.advance(5)
        clock.sleeps.clear()
        await t.read(45356, 1)
        assert clock.sleeps == []
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_cancel_mid_request_leaves_transport_usable(goodwe_udp_sim, goodwe_bank, sim_faults):
    sim_faults.delay_s = 0.2
    sim_faults.delay_only_next = 1
    t = make_transport(_cfg(goodwe_udp_sim), allow_loopback=True)
    try:
        task = asyncio.ensure_future(t.read(45356, 1))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await t.read(47511, 1) == [11]
        await asyncio.sleep(0.25)                       # spóźniona odpowiedź na anulowane żądanie
        assert await t.read(45356, 1) == [5]
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_close_is_idempotent_and_cancels_reader(goodwe_udp_sim, sim_faults):
    sim_faults.drop_next = 10
    t = make_transport(_cfg(goodwe_udp_sim), allow_loopback=True)
    task = asyncio.ensure_future(t.read(45356, 1))
    await asyncio.sleep(0.05)
    await t.close()
    with pytest.raises(LinkDown):
        await asyncio.wait_for(task, 0.2)
    await t.close()
    with pytest.raises(LinkDown):
        await t.read(45356, 1)


@pytest.mark.asyncio
async def test_bad_arguments_rejected_without_sending(goodwe_udp_sim):
    t = make_transport(_cfg(goodwe_udp_sim), allow_loopback=True)
    try:
        with pytest.raises(ValueError):
            await t.read(47509, 0)
        with pytest.raises(ValueError):
            await t.write(47511, [10, 11], function=FC_WRITE_SINGLE)
        with pytest.raises(ValueError):
            await t.write(47511, [10], function=3)
        assert goodwe_udp_sim.requests == 0
    finally:
        await t.close()
