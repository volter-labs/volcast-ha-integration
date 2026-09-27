"""Solarman V5: ramka RTU w kopercie V5 loggera (TCP 8899), dopasowanie po numerze loggera
i sekwencji (logger odsyła tylko pierwszy bajt sekwencji).

Ramki protokołu NASZEGO loggera (heartbeat itp.) nie są odpowiedziami ani sygnałem innego
klienta: liczone w `unsolicited` i potwierdzane tak jak w źródle protokołu. Odpowiedź loggera
bez ramki falownika = `InverterAsleep`.
"""
from __future__ import annotations

from . import v5_frames as v5
from .base import InverterAsleep, Request, Stray, Unsolicited, match_rtu
from .modbus_frames import rtu
from .stream import StreamTransport


class SolarmanV5Transport(StreamTransport):
    kind = "solarman_v5"

    def __init__(self, cfg, host, **kw) -> None:
        super().__init__(cfg, host, **kw)
        self._seq = 0

    def _frame_length(self, prefix: bytes) -> int | None:
        return v5.frame_length(prefix)

    def _encode(self, req: Request) -> tuple[bytes, object]:
        self._seq = (self._seq + 1) & 0xFFFF
        return v5.encode_request(self.cfg.logger_serial, self._seq, rtu(self.cfg.unit, req.pdu)), self._seq

    def _idle_frame(self, frame: bytes) -> None:
        ack = v5.protocol_ack(frame, self.cfg.logger_serial)
        if ack is None:
            self.stats.stray += 1
            return
        self.stats.unsolicited += 1
        self._send(ack)

    def _match(self, frame: bytes, req: Request, seq):
        try:
            payload = v5.decode_response(frame, self.cfg.logger_serial, seq)
        except v5.V5Error as err:
            # NOŚNE: `decode_response` sprawdza numer loggera PRZED kodem sterującym i sekwencją,
            # więc ramka protokołu naszego loggera jest zawsze `control` (→ unsolicited), a nigdy
            # `sequence` (→ stray, czyli sygnał innego klienta). Nie zmieniać tej kolejności.
            if err.kind == "control":
                raise Unsolicited(v5.protocol_ack(frame, self.cfg.logger_serial)) from None
            if err.kind == "frame_type":
                raise Unsolicited(None) from None     # ramka naszego loggera, nie falownika
            if err.kind == "asleep":
                raise InverterAsleep("logger reply without inverter frame") from None
            raise Stray(err.kind) from None
        return match_rtu(payload, self.cfg.unit, req)
