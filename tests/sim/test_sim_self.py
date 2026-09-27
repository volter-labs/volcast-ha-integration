"""Samokontrola symulatorów: bank rejestrów, usterki i cztery serwery na pętli zwrotnej."""
import asyncio
import gc
import socket
import warnings

import pytest

from custom_components.volcast.core.transports import modbus_frames as mf
from custom_components.volcast.core.transports import v5_frames as v5
from tests.sim.device import UNREADABLE, Faults, RegisterBank, crc16_table
from tests.sim.servers import goodwe_udp_server, modbus_tcp_server, rtu_tcp_server, solarman_v5_server

LOGGER = 1234567890


# ── bank rejestrów ────────────────────────────────────────────────────────


def test_bank_unsupported_returns_exception_2():
    bank = RegisterBank({10: 1, 11: 2}, unsupported=[11])
    assert bank.read(10, 1) == [1]
    assert bank.read(10, 2) == 2
    assert bank.write(11, [5]) == 2 and bank.writes == []


def test_bank_clamp_applies_on_write():
    bank = RegisterBank({47760: 100}, clamp={47760: (10, 95)})
    assert bank.write(47760, [100]) is None
    assert bank.read(47760, 1) == [95]
    assert bank.writes == [(47760, 100)]


def test_bank_ignore_writes_poke_unreadable_and_missing():
    bank = RegisterBank({1: 7}, ignore_writes=[1], unreadable=[3])
    assert bank.write(1, [9]) is None and bank.read(1, 1) == [7]
    bank.poke(1, 42)
    assert bank.read(1, 1) == [42]
    assert bank.read(3, 1) == UNREADABLE and bank.read(2, 2) == UNREADABLE
    assert bank.read(500, 2) == [0, 0]                    # brak w mapie = 0, jak w urządzeniu
    bank.write(20, [1, 2])
    assert bank.writes[-2:] == [(20, 1), (21, 2)] and bank.read(20, 2) == [1, 2]


def test_sim_crc_is_independent_and_agrees():
    for body in (b"", b"\xf7\x03\xb9\x95\x00\x04", bytes(range(256))):
        assert crc16_table(body) == mf.crc16(body)


# ── serwery ───────────────────────────────────────────────────────────────


async def _udp_exchange(port: int, payload: bytes, *, timeout: float = 1.0) -> bytes | None:
    loop = asyncio.get_running_loop()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setblocking(False)
    try:
        await loop.sock_sendto(s, payload, ("127.0.0.1", port))
        try:
            return await asyncio.wait_for(loop.sock_recv(s, 1024), timeout)
        except asyncio.TimeoutError:
            return None
    finally:
        s.close()


@pytest.mark.asyncio
async def test_udp_server_answers_golden_read(goodwe_udp_sim):
    reply = await _udp_exchange(goodwe_udp_sim.port, bytes.fromhex("f703b995000465ef"))
    assert reply.hex() == "aa55f7030800003e80000b228e645a"
    assert goodwe_udp_sim.requests == 1


@pytest.mark.asyncio
async def test_udp_write_echo_and_exception(goodwe_udp_sim, goodwe_bank):
    req = mf.write_single_request(0xF7, 47511, 1)
    assert await _udp_exchange(goodwe_udp_sim.port, req) == b"\xaa\x55" + req
    assert goodwe_bank.writes == [(47511, 1)] and goodwe_bank.read(47511, 1) == [1]
    reply = await _udp_exchange(goodwe_udp_sim.port, mf.read_request(0xF7, 47760, 1))
    with pytest.raises(mf.FrameError) as e:
        mf.parse_aa55_read(reply, 0xF7, 1)
    assert e.value.kind == "length"                      # nieczytelny rejestr — ramka niepoprawna


@pytest.mark.asyncio
async def test_udp_faults_drop_mute_wrong_echo_garbage(goodwe_udp_sim, goodwe_bank, sim_faults):
    port = goodwe_udp_sim.port
    sim_faults.drop_next = 1
    assert await _udp_exchange(port, mf.read_request(0xF7, 47509, 4), timeout=0.2) is None
    sim_faults.mute_write_echo = 1
    assert await _udp_exchange(port, mf.write_single_request(0xF7, 47512, 500), timeout=0.2) is None
    assert goodwe_bank.read(47512, 1) == [500]           # zapis doszedł, echo zgubione
    sim_faults.wrong_echo_value = True
    reply = await _udp_exchange(port, mf.write_single_request(0xF7, 47512, 600))
    with pytest.raises(mf.FrameError) as e:
        mf.parse_aa55_write(reply, 0xF7, 47512, 600)
    assert e.value.kind == "echo"
    sim_faults.wrong_echo_value = False
    sim_faults.garbage_next = 1
    reply = await _udp_exchange(port, mf.read_request(0xF7, 47509, 4))
    with pytest.raises(mf.FrameError):
        mf.parse_aa55_read(reply, 0xF7, 4)
    # Ramka z obcym adresem jednostki albo złym CRC — bez odpowiedzi.
    assert await _udp_exchange(port, mf.read_request(0x11, 47509, 4), timeout=0.2) is None
    bad = bytearray(mf.read_request(0xF7, 47509, 4)); bad[-1] ^= 0xFF
    assert await _udp_exchange(port, bytes(bad), timeout=0.2) is None


@pytest.mark.asyncio
async def test_udp_late_duplicate_and_stray(goodwe_udp_sim, sim_faults):
    loop = asyncio.get_running_loop()
    sim_faults.late_duplicate = True
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setblocking(False)
    try:
        dst = ("127.0.0.1", goodwe_udp_sim.port)
        await loop.sock_sendto(s, mf.read_request(0xF7, 45356, 1), dst)
        first = await asyncio.wait_for(loop.sock_recv(s, 1024), 1)
        await loop.sock_sendto(s, mf.read_request(0xF7, 47509, 4), dst)
        again = await asyncio.wait_for(loop.sock_recv(s, 1024), 1)
        second = await asyncio.wait_for(loop.sock_recv(s, 1024), 1)
        assert again == first                             # spóźniona kopia poprzedniej odpowiedzi
        assert mf.parse_aa55_read(second, 0xF7, 4)
        sim_faults.late_duplicate = False
        sim_faults.stray_every = 1
        await loop.sock_sendto(s, mf.read_request(0xF7, 45356, 1), dst)
        stray = await asyncio.wait_for(loop.sock_recv(s, 1024), 1)
        real = await asyncio.wait_for(loop.sock_recv(s, 1024), 1)
        # Kopia spóźniona z poprzedniego żądania już nie przychodzi (flaga zdjęta).
        assert stray != real and mf.parse_aa55_read(real, 0xF7, 1) == [5]
    finally:
        s.close()


async def _tcp_exchange(reader, writer, frame: bytes, n: int) -> bytes:
    writer.write(frame)
    await writer.drain()
    return await asyncio.wait_for(reader.readexactly(n), 1)


@pytest.mark.asyncio
async def test_modbus_tcp_server_read_and_write(modbus_tcp_sim, goodwe_bank):
    reader, writer = await asyncio.open_connection("127.0.0.1", modbus_tcp_sim.port)
    try:
        resp = await _tcp_exchange(reader, writer, mf.mbap(0x1234, 247, mf.pdu_read(47509, 4)), 6 + 1 + 2 + 8)
        tid, unit, pdu = mf.parse_mbap(resp)
        assert (tid, unit) == (0x1234, 247)
        assert mf.parse_pdu_read(pdu, 4) == [0, 16000, 11, 8846]
        resp = await _tcp_exchange(reader, writer, mf.mbap(2, 247, mf.pdu_write_multiple(47510, [100, 1])), 12)
        mf.parse_pdu_write(mf.parse_mbap(resp)[2], mf.FC_WRITE_MULTIPLE, 47510, 2)
        assert goodwe_bank.read(47510, 2) == [100, 1]
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_tcp_server_max_clients_one(modbus_tcp_sim, sim_faults):
    sim_faults.max_clients = 1
    r1, w1 = await asyncio.open_connection("127.0.0.1", modbus_tcp_sim.port)
    await asyncio.sleep(0.05)
    r2, w2 = await asyncio.open_connection("127.0.0.1", modbus_tcp_sim.port)
    try:
        assert await asyncio.wait_for(r2.read(10), 1) == b""          # drugi klient zamknięty
        resp = await _tcp_exchange(r1, w1, mf.mbap(1, 247, mf.pdu_read(45356, 1)), 11)
        assert mf.parse_pdu_read(mf.parse_mbap(resp)[2], 1) == [5]
        assert modbus_tcp_sim.clients == 2
    finally:
        for w in (w1, w2):
            w.close()
            await w.wait_closed()


@pytest.mark.asyncio
async def test_tcp_reset_after(rtu_tcp_sim, sim_faults):
    sim_faults.reset_after = 1
    reader, writer = await asyncio.open_connection("127.0.0.1", rtu_tcp_sim.port)
    try:
        resp = await _tcp_exchange(reader, writer, mf.read_request(1, 148, 1), 7)
        assert mf.parse_rtu_read(resp, 1, 1)
        assert await asyncio.wait_for(reader.read(10), 1) == b""       # zerwane po 1 żądaniu
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_rtu_tcp_server_split_frames_and_fc16(rtu_tcp_sim, deye_bank):
    reader, writer = await asyncio.open_connection("127.0.0.1", rtu_tcp_sim.port)
    try:
        req = mf.read_request(1, 148, 6)
        writer.write(req[:3])                              # ramka w dwóch kawałkach strumienia
        await writer.drain()
        await asyncio.sleep(0.02)
        resp = await _tcp_exchange(reader, writer, req[3:], 5 + 12)
        assert mf.parse_rtu_read(resp, 1, 6) == [0, 500, 1000, 1400, 1800, 2200]
        resp = await _tcp_exchange(reader, writer, mf.write_multiple_request(1, 148, [130]), 8)
        mf.parse_rtu_write(resp, 1, mf.FC_WRITE_MULTIPLE, 148, 1)
        assert deye_bank.read(148, 1) == [130]
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_v5_server_echoes_sequence_first_byte(v5_sim):
    reader, writer = await asyncio.open_connection("127.0.0.1", v5_sim.port)
    try:
        req = v5.encode_request(LOGGER, 0x012A, mf.read_request(1, 146, 1))
        writer.write(req)
        await writer.drain()
        head = await asyncio.wait_for(reader.readexactly(3), 1)
        frame = head + await asyncio.wait_for(reader.readexactly(v5.frame_length(head) - 3), 1)
        assert frame[5] == 0x2A and frame[6] != 0x01            # drugi bajt — licznik loggera
        rtu = v5.decode_response(frame, LOGGER, 0x012A)
        assert mf.parse_rtu_read(rtu, 1, 1) == [0x00FF]
        # Żądanie do innego loggera — bez odpowiedzi.
        writer.write(v5.encode_request(LOGGER + 1, 1, mf.read_request(1, 146, 1)))
        await writer.drain()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(reader.read(10), 0.2)
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_servers_close_cleanly(goodwe_bank):
    faults = Faults()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        servers = [await goodwe_udp_server(goodwe_bank, faults),
                   await modbus_tcp_server(goodwe_bank, faults),
                   await rtu_tcp_server(goodwe_bank, faults),
                   await solarman_v5_server(goodwe_bank, faults, logger_serial=LOGGER)]
        conns = [await asyncio.open_connection("127.0.0.1", s.port) for s in servers[1:]]
        for s in servers:
            await s.close()
            await s.close()                                    # idempotentne
        for _, w in conns:
            w.close()
            await w.wait_closed()
        gc.collect()
    assert not [w for w in caught if issubclass(w.category, ResourceWarning)]
    assert all(s.host == "127.0.0.1" and s.port > 0 for s in servers)
