"""RTU przez przezroczystą bramkę TCP (konwerter RS485↔TCP): gołe ramki RTU w strumieniu.

Ramka RTU nie niesie numeru transakcji, więc dopasowanie opiera się na jednostce, funkcji,
długości i echu, a po każdym przekroczeniu czasu połączenie jest zamykane (reset kanału).
"""
from __future__ import annotations

from .base import Request, match_rtu
from .modbus_frames import rtu, rtu_frame_length
from .stream import StreamTransport


class RtuTcpTransport(StreamTransport):
    kind = "modbus_rtu"

    def _frame_length(self, prefix: bytes) -> int | None:
        # FrameError(kind="function") dla funkcji, której nie da się odciąć → rozsynchronizowanie.
        return rtu_frame_length(prefix)

    def _encode(self, req: Request) -> tuple[bytes, object]:
        return rtu(self.cfg.unit, req.pdu), None

    def _match(self, frame: bytes, req: Request, ctx):
        return match_rtu(frame, self.cfg.unit, req)
