"""Wspólna część transportów TCP (Modbus TCP, RTU przez bramkę TCP, Solarman V5).

Połączenie trwałe, otwierane leniwie przy pierwszym żądaniu. Strumień jest cięty na ramki
według długości z nagłówka ramki (podklasa), z limitem bufora `MAX_BUFFER`; strumienia, którego
nie da się dalej ciąć (śmieci, nieznana funkcja RTU, deklarowana długość ponad limit), nie
próbujemy ratować — połączenie jest zamykane i liczone jako `stray`.

Zerwanie przez drugą stronę (EOF, reset) → `peer_resets` + odwrót 1→2→4…→60 s; w odwrocie
żądanie kończy się `LinkDown` bez próby łączenia. Nasze zamknięcie przy resecie kanału to
`channel_resets` i nie uruchamia odwrotu. Udana wymiana zeruje odwrót.
"""
from __future__ import annotations

import asyncio
import logging

from .base import BaseTransport, Desync, LinkDown, RequestTimeout

_LOGGER = logging.getLogger(__name__)
MAX_BUFFER = 1024
_CLOSE_WAIT_S = 1.0


class _Stream(asyncio.Protocol):
    def __init__(self) -> None:
        self.buf = bytearray()
        self.overflow = False
        self.lost = False
        self.event = asyncio.Event()
        self.closed = asyncio.get_running_loop().create_future()
        self.transport: asyncio.Transport | None = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def data_received(self, data: bytes) -> None:
        self.buf += data
        if len(self.buf) > MAX_BUFFER:
            self.overflow = True
            self.buf.clear()
        self.event.set()

    def eof_received(self):
        return False                 # zamknij — połowiczne połączenie nic nam nie da

    def connection_lost(self, exc) -> None:
        self.lost = True
        self.event.set()
        if not self.closed.done():
            self.closed.set_result(None)


class StreamTransport(BaseTransport):
    """Podklasy: `_frame_length(prefix)`, `_encode`, `_match`, opcjonalnie `_idle_frame`."""

    def __init__(self, cfg, host, **kw) -> None:
        super().__init__(cfg, host, **kw)
        self._proto: _Stream | None = None
        self._connected_once = False
        self._backoff = 0.0
        self._retry_at: float | None = None

    # ── ramkowanie (podklasy) ──

    def _frame_length(self, prefix: bytes) -> int | None:
        """Długość ramki z jej początku; None = za mało bajtów; wyjątek = strumień nie do cięcia."""
        raise NotImplementedError

    def _idle_frame(self, frame: bytes) -> None:
        """Ramka zastana w buforze przed wysłaniem żądania: domyślnie obca."""
        self.stats.stray += 1

    # ── kanał ──

    async def _open(self) -> None:
        if self._closed:
            raise LinkDown("transport closed")
        proto = self._proto
        if proto is not None and not proto.lost:
            return
        if proto is not None:
            # Druga strona zamknęła połączenie, gdy nic nie wysyłaliśmy.
            self._proto = None
            self._peer_lost()
        if self._retry_at is not None and self._clock() < self._retry_at:
            raise LinkDown("reconnect backoff")
        loop = asyncio.get_running_loop()
        try:
            transport, proto = await asyncio.wait_for(
                loop.create_connection(_Stream, self._host, self.cfg.port), self.cfg.connect_timeout_s)
        except (OSError, TimeoutError):
            self._arm_backoff()
            _LOGGER.debug("%s: connect failed", self.kind)
            raise LinkDown("connection failed") from None
        if self._closed:
            transport.close()
            await _wait_closed(proto)
            raise LinkDown("transport closed")
        if self._connected_once:
            self.stats.reconnects += 1
        self._connected_once = True
        self._proto = proto

    def _peer_lost(self) -> None:
        self.stats.peer_resets += 1
        self._arm_backoff()
        _LOGGER.debug("%s: connection closed by peer", self.kind)
        raise LinkDown("connection closed by peer")

    def _arm_backoff(self) -> None:
        self._backoff = min(self.cfg.backoff_max_s, max(self.cfg.backoff_min_s, 2 * self._backoff))
        self._retry_at = self._clock() + self._backoff

    def _on_answer(self) -> None:
        self._backoff = 0.0
        self._retry_at = None

    async def _close_channel(self) -> bool:
        proto, self._proto = self._proto, None
        if proto is None:
            return False
        if proto.transport is not None:
            proto.transport.close()
        await _wait_closed(proto)
        return True

    def _send(self, data: bytes) -> None:
        proto = self._proto
        if proto is None or proto.transport is None or proto.lost or proto.transport.is_closing():
            raise LinkDown("channel closed")
        proto.transport.write(data)

    # ── cięcie strumienia ──

    def _cut(self, proto: _Stream) -> bytes | None:
        if proto.overflow:
            raise Desync("buffer limit")
        try:
            n = self._frame_length(bytes(proto.buf[:16]))
        except ValueError:            # FrameError / V5Error (np. funkcja RTU nie do odcięcia)
            raise Desync("undelimitable frame") from None
        if n is None:
            return None
        if n > MAX_BUFFER:
            raise Desync("frame over limit")
        if len(proto.buf) < n:
            return None
        frame = bytes(proto.buf[:n])
        del proto.buf[:n]
        return frame

    def _drain(self) -> bool:
        proto = self._proto
        if proto is None:
            return False
        try:
            while (frame := self._cut(proto)) is not None:
                self._idle_frame(frame)
        except Desync:
            return True
        # Urwana ramka w buforze: nie wiadomo, gdzie zaczyna się następna odpowiedź.
        return bool(proto.buf)

    async def _next_frame(self, deadline: float) -> bytes | None:
        loop = asyncio.get_running_loop()
        while True:
            proto = self._proto
            if self._closed or proto is None:
                raise LinkDown("channel closed")
            try:
                frame = self._cut(proto)
            except Desync:
                self.stats.stray += 1
                _LOGGER.debug("%s: stream out of sync", self.kind)
                raise RequestTimeout("stream out of sync") from None
            if frame is not None:
                return frame
            if proto.lost:
                self._proto = None
                self._dirty = False
                self._peer_lost()
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            proto.event.clear()
            try:
                await asyncio.wait_for(proto.event.wait(), remaining)
            except TimeoutError:
                return None


async def _wait_closed(proto) -> None:
    try:
        await asyncio.wait_for(asyncio.shield(proto.closed), _CLOSE_WAIT_S)
    except TimeoutError:
        pass
