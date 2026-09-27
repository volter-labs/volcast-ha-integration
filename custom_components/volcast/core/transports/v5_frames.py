"""Ramki Solarman V5 (logger → falownik przez TCP 8899) — czyste funkcje, bez gniazd.

Układ według publicznego opisu protokołu (pysolarmanv5, „Solarman V5 Protocol"):
wszystkie pola V5 little-endian, ramka RTU w środku big-endian (jak w Modbusie).

    A5 | długość ładunku u16 | kod sterujący u16 | sekwencja u16 | numer loggera u32 |
    ładunek | suma kontrolna u8 | 15

Ładunek żądania: typ 0x02, czujnik 0x0000, 3×u32 zera, ramka RTU (15 B + RTU).
Ładunek odpowiedzi: typ, status, 3×u32, ramka RTU (14 B + RTU).
Suma kontrolna = suma bajtów od długości do końca ładunku mod 256.

Osobliwości loggerów (za źródłem):
* sekwencja — logger odsyła tylko PIERWSZY bajt; drugi podbija sam, więc porównujemy `seq & 0xFF`;
* status odpowiedzi — źródło nie opisuje żadnej wartości jako błędu, więc żadnej nie odrzucamy;
* typ ramki odpowiedzi ≠ 0x02 (logger, chmura) — to nie odpowiedź falownika (`frame_type`);
* ładunek bez pełnej ramki RTU (< 5 B) — logger przy uśpionym falowniku (`asleep`);
* podwójne CRC (0x0000 za poprawną ramką RTU) — zdejmowane, gdy ramka bez nich ma poprawne CRC;
* ramki protokołu loggera (m.in. heartbeat 0x4710) to nie odpowiedzi (`control`); źródło
  potwierdza je „odpowiedzią czasu" — buduje ją `protocol_ack`.

Kolejność kontroli w `decode_response`: długość, start, koniec, suma kontrolna, numer loggera,
kod sterujący, sekwencja, typ ramki, ramka RTU. Numer loggera przed kodem: ramka protokołu
NASZEGO loggera jest zawsze `control` (niezależnie od sekwencji), obcego — `serial`.
"""
from __future__ import annotations

import time

from .modbus_frames import crc16

V5_START = 0xA5
V5_END = 0x15
CTRL_REQUEST = 0x4510
CTRL_RESPONSE = 0x1510
MAX_FRAME = 1024
_OVERHEAD = 13                  # start + długość + kod + sekwencja + numer loggera + suma + koniec
_HEADER = 11
_REQ_PAYLOAD_HEAD = 15
_RESP_PAYLOAD_HEAD = 14
_FRAME_TYPE_INVERTER = 0x02
_CTRL_SUFFIX = 0x10
# Kody ramek protokołu loggera (starszy bajt kodu), na które źródło odpowiada.
_PROTOCOL_CODES = frozenset({0x41, 0x42, 0x43, 0x47, 0x48})     # handshake, data, info, heartbeat, report
_RESPONSE_OFFSET = 0x30        # kod odpowiedzi = kod żądania − 0x30 (starszy bajt)


class V5Error(ValueError):
    """Ramka V5 odrzucona. `kind`: short|start|end|checksum|serial|control|sequence|frame_type|asleep|length.

    (`status` nie jest zgłaszany — żadna wartość bajtu statusu nie jest opisana jako błąd.)
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def _int_in(name: str, v, lo: int, hi: int) -> None:
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise ValueError(f"{name} poza zakresem {lo}..{hi}: {v!r}")


def _checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def _build(control: int, seq_bytes: bytes, logger_serial: int, payload: bytes) -> bytes:
    body = (len(payload).to_bytes(2, "little") + control.to_bytes(2, "little") + seq_bytes
            + logger_serial.to_bytes(4, "little") + payload)
    return bytes([V5_START]) + body + bytes([_checksum(body), V5_END])


def encode_request(logger_serial: int, seq: int, rtu_frame: bytes) -> bytes:
    """Żądanie V5 niosące gołą ramkę RTU."""
    _int_in("numer loggera", logger_serial, 0, 2**32 - 1)
    _int_in("sekwencja", seq, 0, 0xFFFF)
    if not isinstance(rtu_frame, (bytes, bytearray)) or not rtu_frame:
        raise ValueError("ramka RTU musi być niepustym ciągiem bajtów")
    if _OVERHEAD + _REQ_PAYLOAD_HEAD + len(rtu_frame) > MAX_FRAME:
        raise ValueError(f"ramka V5 dłuższa niż {MAX_FRAME} B")
    payload = bytes([_FRAME_TYPE_INVERTER]) + bytes(2) + bytes(12) + bytes(rtu_frame)
    return _build(CTRL_REQUEST, seq.to_bytes(2, "little"), logger_serial, payload)


def frame_length(prefix: bytes) -> int | None:
    """Pełna długość ramki w strumieniu z jej pierwszych bajtów; None = za mało bajtów."""
    if len(prefix) < 3:
        return None
    if prefix[0] != V5_START:
        raise V5Error("start", "brak bajtu startu V5")
    n = _OVERHEAD + int.from_bytes(prefix[1:3], "little")
    if n > MAX_FRAME:
        raise V5Error("length", f"ramka V5 dłuższa niż {MAX_FRAME} B")
    return n


def control_code(frame: bytes) -> int | None:
    """Kod sterujący ramki V5; None, gdy to nie jest (kompletny nagłówek) ramki V5."""
    if len(frame) < 5 or frame[0] != V5_START:
        return None
    return int.from_bytes(frame[3:5], "little")


def _checked(frame: bytes) -> bytes:
    """Ramka przycięta do zadeklarowanej długości, po kontroli długości, startu, końca i sumy."""
    if len(frame) < _OVERHEAD:
        raise V5Error("short", "ramka krótsza niż nagłówek i stopka V5")
    if frame[0] != V5_START:
        raise V5Error("start", "brak bajtu startu V5")
    n = _OVERHEAD + int.from_bytes(frame[1:3], "little")
    if len(frame) < n:
        raise V5Error("short", "ramka krótsza niż zadeklarowany ładunek")
    frame = frame[:n]
    if frame[-1] != V5_END:
        raise V5Error("end", "brak bajtu końca V5")
    if frame[-2] != _checksum(frame[1:-2]):
        raise V5Error("checksum", "suma kontrolna V5 się nie zgadza")
    return frame


def _serial_of(frame: bytes) -> int:
    return int.from_bytes(frame[7:11], "little")


def _strip_double_crc(rtu: bytes) -> bytes:
    if len(rtu) >= 6 and rtu.endswith(b"\x00\x00"):
        inner = rtu[:-2]
        if crc16(inner[:-2]) == (inner[-2] | inner[-1] << 8):
            return inner
    return rtu


def decode_response(frame: bytes, logger_serial: int, seq: int) -> bytes:
    """Odpowiedź V5 na nasze żądanie → goła ramka RTU (bez nadmiarowego, podwójnego CRC)."""
    _int_in("numer loggera", logger_serial, 0, 2**32 - 1)
    _int_in("sekwencja", seq, 0, 0xFFFF)
    frame = _checked(bytes(frame))
    # NOŚNA KOLEJNOŚĆ — nie zmieniać (także „dla zgodności ze źródłem"): numer loggera przed kodem
    # sterującym i sekwencją. Transport liczy `control` jako ramkę protokołu NASZEGO loggera
    # (`unsolicited`), a `serial`/`sequence` jako obcą ramkę (`stray` = sygnał innego klienta).
    # Sekwencja sprawdzana wcześniej zrobiłaby z heartbeatu naszego loggera fałszywą kolizję.
    if _serial_of(frame) != logger_serial:
        raise V5Error("serial", "ramka innego loggera")
    if control_code(frame) != CTRL_RESPONSE:
        raise V5Error("control", "ramka protokołu loggera, nie odpowiedź Modbus")
    if frame[5] != seq & 0xFF:
        raise V5Error("sequence", "sekwencja innego żądania")
    payload = frame[_HEADER:-2]
    if payload and payload[0] != _FRAME_TYPE_INVERTER:
        raise V5Error("frame_type", "odpowiedź nie pochodzi od falownika")
    # Pusty albo ucięty ładunek też jest odpowiedzią-błędem loggera, nie obcą ramką.
    rtu = payload[_RESP_PAYLOAD_HEAD:]
    if len(rtu) < 5:
        raise V5Error("asleep", "odpowiedź bez ramki RTU (falownik nie odpowiada)")
    return _strip_double_crc(rtu)


def protocol_ack(frame: bytes, logger_serial: int, *, now: int | None = None) -> bytes | None:
    """Potwierdzenie ramki protokołu naszego loggera, tak jak robi to źródło; inaczej None.

    Odpowiedź czasu: kod = kod ramki − 0x30 (np. heartbeat 0x4710 → 0x1710), sekwencja z
    pierwszym bajtem +1, ładunek 0x00 0x01 + czas uniksowy u32 + 0 u32. `now` — do testów.
    """
    try:
        frame = _checked(bytes(frame))
    except V5Error:
        return None
    if _serial_of(frame) != logger_serial:
        return None
    if frame[3] != _CTRL_SUFFIX or frame[4] not in _PROTOCOL_CODES:
        return None
    unix = int(time.time()) if now is None else now
    _int_in("czas", unix, 0, 2**32 - 1)
    control = _CTRL_SUFFIX | ((frame[4] - _RESPONSE_OFFSET) << 8)
    seq = bytes([(frame[5] + 1) & 0xFF, frame[6]])
    payload = b"\x00\x01" + unix.to_bytes(4, "little") + bytes(4)
    return _build(control, seq, logger_serial, payload)
