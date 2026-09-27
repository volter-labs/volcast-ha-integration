"""GoodWe UDP: żądanie jako goła ramka RTU, odpowiedź `AA55` + RTU (port 8899, jak Box).

Gniazdo UDP jest „połączone” z celem (jądro odrzuca datagramy od innych nadawców), a nadawca
jest mimo to sprawdzany przy każdym datagramie. Reset kanału = nowe gniazdo, czyli nowy port
źródłowy: spóźniona odpowiedź na stare żądanie trafia w zamknięty port, nie w nowe żądanie.
Bez `SO_BROADCAST` — adres rozgłoszeniowy podany jako cel po prostu nie wyjdzie.
"""
from __future__ import annotations

import asyncio
from collections import deque

from .base import BaseTransport, LinkDown, Request, Stray, match_rtu
from .modbus_frames import rtu

_CLOSE_WAIT_S = 1.0
# Niezamówione datagramy czekające na odbiór — z limitem (zalew obcymi ramkami nie rośnie bez końca).
MAX_QUEUED_DATAGRAMS = 64


class _Datagrams(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.queue: deque[tuple[bytes, object]] = deque(maxlen=MAX_QUEUED_DATAGRAMS)
        self.event = asyncio.Event()
        self.lost = False
        self.closed = asyncio.get_running_loop().create_future()
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        self.queue.append((data, addr))
        self.event.set()

    def error_received(self, exc: Exception) -> None:
        # ICMP „port nieosiągalny” itp. — brak odpowiedzi rozstrzygnie limit czasu.
        pass

    def connection_lost(self, exc) -> None:
        self.lost = True
        self.event.set()
        if not self.closed.done():
            self.closed.set_result(None)


class GoodweUdpTransport(BaseTransport):
    kind = "goodwe_udp"
    resend_writes = True

    def __init__(self, cfg, host, **kw) -> None:
        super().__init__(cfg, host, **kw)
        self._proto: _Datagrams | None = None
        self._peer: tuple | None = None

    async def _open(self) -> None:
        if self._closed:
            raise LinkDown("transport closed")
        if self._proto is not None and not self._proto.lost:
            return
        self._proto = None
        loop = asyncio.get_running_loop()
        try:
            transport, proto = await loop.create_datagram_endpoint(
                _Datagrams, remote_addr=(self._host, self.cfg.port))
        except OSError:
            raise LinkDown("cannot open datagram endpoint") from None
        if self._closed:
            transport.close()
            await _wait_closed(proto)
            raise LinkDown("transport closed")
        self._proto = proto
        self._peer = transport.get_extra_info("peername")

    async def _close_channel(self) -> bool:
        proto, self._proto = self._proto, None
        if proto is None:
            return False
        if proto.transport is not None:
            proto.transport.close()
        await _wait_closed(proto)
        return True

    def _drain(self) -> bool:
        proto = self._proto
        while proto is not None and proto.queue:
            proto.queue.popleft()
            self.stats.stray += 1
        return False

    def _encode(self, req: Request) -> tuple[bytes, object]:
        return rtu(self.cfg.unit, req.pdu), None

    def _send(self, data: bytes) -> None:
        proto = self._proto
        if proto is None or proto.transport is None or proto.lost:
            raise LinkDown("channel closed")
        proto.transport.sendto(data)

    async def _next_frame(self, deadline: float) -> bytes | None:
        loop = asyncio.get_running_loop()
        while True:
            proto = self._proto
            if self._closed or proto is None or proto.lost:
                raise LinkDown("channel closed")
            if proto.queue:
                data, addr = proto.queue.popleft()
                if self._peer is not None and tuple(addr[:2]) != tuple(self._peer[:2]):
                    self.stats.stray += 1
                    continue
                return data
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            proto.event.clear()
            try:
                await asyncio.wait_for(proto.event.wait(), remaining)
            except TimeoutError:
                return None

    def _match(self, frame: bytes, req: Request, ctx):
        if frame[:2] != b"\xaa\x55":
            raise Stray("header")
        return match_rtu(frame[2:], self.cfg.unit, req)


async def _wait_closed(proto) -> None:
    try:
        await asyncio.wait_for(asyncio.shield(proto.closed), _CLOSE_WAIT_S)
    except TimeoutError:
        pass
