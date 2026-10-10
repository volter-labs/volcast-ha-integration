"""Modbus TCP: nagłówek MBAP + PDU (port 502). Odpowiedź dopasowana po TID i jednostce."""
from __future__ import annotations

from .base import ModbusException, Request, Stray
from .modbus_frames import (
    READ_FUNCTIONS, FrameError, mbap, parse_mbap, parse_pdu_read, parse_pdu_write)
from .stream import StreamTransport


class ModbusTcpTransport(StreamTransport):
    kind = "modbus_tcp"

    def __init__(self, cfg, host, **kw) -> None:
        super().__init__(cfg, host, **kw)
        self._tid = 0

    def _frame_length(self, prefix: bytes) -> int | None:
        if len(prefix) < 6:
            return None
        return 6 + int.from_bytes(prefix[4:6], "big")

    def _encode(self, req: Request) -> tuple[bytes, object]:
        self._tid = (self._tid + 1) & 0xFFFF
        return mbap(self._tid, self.cfg.unit, req.pdu), self._tid

    def _match(self, frame: bytes, req: Request, tid):
        try:
            got_tid, unit, pdu = parse_mbap(frame)
        except FrameError:
            raise Stray("mbap") from None
        if got_tid != tid or unit != self.cfg.unit:
            raise Stray("tid or unit")
        if pdu and pdu[0] & 0x80 and pdu[0] != req.fc | 0x80:
            raise Stray("exception for another function")
        try:
            if req.fc in READ_FUNCTIONS:
                return parse_pdu_read(pdu, req.count, req.fc)
            parse_pdu_write(pdu, req.fc, req.addr, req.echo)
            return None
        except FrameError as err:
            if err.kind == "exception" and err.code is not None:
                raise ModbusException(err.code) from None
            raise Stray(err.kind) from None
