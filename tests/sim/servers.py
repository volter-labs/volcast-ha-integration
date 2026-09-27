"""Serwery symulatora na 127.0.0.1 (port efemeryczny): GoodWe UDP/AA55, Modbus TCP,
RTU przez bramkę TCP i Solarman V5. Usterki z `Faults` czytane na bieżąco."""
from __future__ import annotations

import asyncio
import struct

from .device import GARBAGE, Faults, RegisterBank, crc_ok, handle_pdu, rtu_request_length, with_crc

HOST = "127.0.0.1"


class SimServer:
    host: str = HOST

    def __init__(self, bank: RegisterBank, faults: Faults, unit: int) -> None:
        self.bank = bank
        self.faults = faults
        self.unit = unit
        self.port = 0
        self.requests = 0
        self.clients = 0
        self._last: bytes | None = None
        self._closed = False

    # Wspólna obsługa żądania: usterki + ramkowanie odpowiedzi przez podklasę.
    def _respond(self, pdu: bytes, frame) -> list[bytes]:
        """`frame(pdu, stray)` składa odpowiedź; zwraca ramki do wysłania w kolejności."""
        self.requests += 1
        f = self.faults
        if f.drop_next > 0:
            f.drop_next -= 1
            return []
        resp, is_write = handle_pdu(self.bank, pdu, f)
        if is_write and f.mute_write_echo > 0:
            f.mute_write_echo -= 1
            return []
        framed = frame(resp, False)
        if f.garbage_next > 0:
            f.garbage_next -= 1
            framed = GARBAGE
        out: list[bytes] = []
        if f.late_duplicate and self._last is not None:
            out.append(self._last)
        if f.stray_every and self.requests % f.stray_every == 0:
            out.append(frame(resp, True))
        out.append(framed)
        self._last = framed
        return out

    async def close(self) -> None:
        raise NotImplementedError


# ── UDP (GoodWe AA55) ─────────────────────────────────────────────────────


class _UdpProto(asyncio.DatagramProtocol):
    def __init__(self, server: "UdpServer") -> None:
        self.server = server

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        self.server.on_datagram(data, addr)


class UdpServer(SimServer):
    def __init__(self, bank, faults, unit) -> None:
        super().__init__(bank, faults, unit)
        self._transport = None
        self._peers: set = set()
        self._handles: list[asyncio.TimerHandle] = []

    async def start(self) -> "UdpServer":
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(lambda: _UdpProto(self), local_addr=(HOST, 0))
        self.port = self._transport.get_extra_info("sockname")[1]
        return self

    def _frame(self, pdu: bytes, stray: bool) -> bytes:
        unit = self.unit ^ 0x01 if stray else self.unit
        return b"\xaa\x55" + with_crc(bytes([unit]) + pdu)

    def on_datagram(self, data: bytes, addr) -> None:
        if len(data) < 4 or data[0] != self.unit or not crc_ok(data):
            return
        if addr not in self._peers:
            self._peers.add(addr)
            self.clients += 1
        frames = self._respond(data[1:-2], self._frame)
        if not frames:
            return

        def send() -> None:
            if self._transport is not None and not self._transport.is_closing():
                for fr in frames:
                    self._transport.sendto(fr, addr)
        if self.faults.delay_s > 0:
            self._handles.append(asyncio.get_running_loop().call_later(self.faults.delay_s, send))
        else:
            send()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for h in self._handles:
            h.cancel()
        if self._transport is not None:
            self._transport.close()
        await asyncio.sleep(0)


# ── TCP (Modbus TCP, RTU przez bramkę, Solarman V5) ───────────────────────


class TcpServer(SimServer):
    def __init__(self, bank, faults, unit) -> None:
        super().__init__(bank, faults, unit)
        self._server: asyncio.base_events.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> "TcpServer":
        self._server = await asyncio.start_server(self._handle, HOST, 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    # Podklasy: długość ramki żądania z bufora i obsługa ramki → ramki odpowiedzi.
    def _request_length(self, buf: bytes) -> int | None:
        raise NotImplementedError

    def _on_frame(self, frame: bytes) -> list[bytes]:
        raise NotImplementedError

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.clients += 1
        if self.faults.max_clients and len(self._writers) >= self.faults.max_clients:
            writer.close()
            return
        task = asyncio.current_task()
        self._tasks.add(task)
        self._writers.add(writer)
        served = 0
        buf = b""
        try:
            while True:
                data = await reader.read(1024)
                if not data:
                    return
                buf += data
                while True:
                    n = self._request_length(buf)
                    if n is None or len(buf) < n:
                        break
                    frame, buf = buf[:n], buf[n:]
                    out = self._on_frame(frame)
                    if out and self.faults.delay_s > 0:
                        await asyncio.sleep(self.faults.delay_s)
                    for fr in out:
                        writer.write(fr)
                    await writer.drain()
                    served += 1
                    if self.faults.reset_after and served >= self.faults.reset_after:
                        return
        except (ConnectionError, asyncio.CancelledError):
            return
        finally:
            self._writers.discard(writer)
            self._tasks.discard(task)
            writer.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._server is not None:
            self._server.close()
        for w in list(self._writers):
            w.close()
        for t in list(self._tasks):
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server is not None:
            await self._server.wait_closed()


class ModbusTcpServer(TcpServer):
    def _request_length(self, buf: bytes) -> int | None:
        return None if len(buf) < 6 else 6 + struct.unpack(">H", buf[4:6])[0]

    def _on_frame(self, frame: bytes) -> list[bytes]:
        tid, proto, _length, unit = struct.unpack(">HHHB", frame[:7])
        if proto != 0 or unit != self.unit:
            return []

        def mbap(pdu: bytes, stray: bool) -> bytes:
            t = (tid + 1) & 0xFFFF if stray else tid
            return struct.pack(">HHHB", t, 0, len(pdu) + 1, unit) + pdu
        return self._respond(frame[7:], mbap)


class RtuTcpServer(TcpServer):
    def _request_length(self, buf: bytes) -> int | None:
        return rtu_request_length(buf)

    def _on_frame(self, frame: bytes) -> list[bytes]:
        if frame[0] != self.unit or not crc_ok(frame):
            return []

        def rtu(pdu: bytes, stray: bool) -> bytes:
            return with_crc(bytes([self.unit ^ 0x01 if stray else self.unit]) + pdu)
        return self._respond(frame[1:-2], rtu)


class SolarmanV5Server(TcpServer):
    def __init__(self, bank, faults, unit, logger_serial: int) -> None:
        super().__init__(bank, faults, unit)
        self.logger_serial = logger_serial
        self._counter = 0x50           # drugi bajt sekwencji — licznik loggera

    def _request_length(self, buf: bytes) -> int | None:
        if len(buf) < 3:
            return None
        if buf[0] != 0xA5:
            return len(buf)            # śmieci — odrzucone w całości
        return 13 + struct.unpack("<H", buf[1:3])[0]

    def _on_frame(self, frame: bytes) -> list[bytes]:
        if (len(frame) < 13 + 15 or frame[0] != 0xA5 or frame[-1] != 0x15
                or frame[-2] != sum(frame[1:-2]) & 0xFF):
            return []
        control, seq_lo, serial = struct.unpack("<HBxI", frame[3:11])
        if control != 0x4510 or serial != self.logger_serial:
            return []
        rtu_req = frame[11 + 15:-2]
        if len(rtu_req) < 4 or rtu_req[0] != self.unit or not crc_ok(rtu_req):
            return []

        def v5(pdu: bytes, stray: bool) -> bytes:
            self._counter = (self._counter + 1) & 0xFF
            rtu = with_crc(bytes([self.unit]) + pdu)
            payload = bytes([0x02, 0x01]) + bytes(12) + rtu
            seq = bytes([(seq_lo + 1) & 0xFF if stray else seq_lo, self._counter])
            body = struct.pack("<HH", len(payload), 0x1510) + seq + struct.pack("<I", self.logger_serial) + payload
            return b"\xa5" + body + bytes([sum(body) & 0xFF, 0x15])
        return self._respond(rtu_req[1:-2], v5)


async def goodwe_udp_server(bank: RegisterBank, faults: Faults, unit: int = 0xF7) -> SimServer:
    return await UdpServer(bank, faults, unit).start()


async def modbus_tcp_server(bank: RegisterBank, faults: Faults, unit: int = 247) -> SimServer:
    return await ModbusTcpServer(bank, faults, unit).start()


async def rtu_tcp_server(bank: RegisterBank, faults: Faults, unit: int = 1) -> SimServer:
    return await RtuTcpServer(bank, faults, unit).start()


async def solarman_v5_server(bank: RegisterBank, faults: Faults, logger_serial: int, unit: int = 1) -> SimServer:
    return await SolarmanV5Server(bank, faults, unit, logger_serial).start()
