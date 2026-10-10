"""Ramki Modbus (PDU, RTU, MBAP) i opakowanie AA55 GoodWe — czyste funkcje, bez gniazd.

Kolejność kontroli odpowiedzi RTU: długość, nagłówek, CRC, kod funkcji, długość danych;
przy zapisie CRC jest sprawdzane PRZED porównaniem echa (zepsuta ramka to nie „obce echo").
Echo zapisu innego rejestru, wartości albo liczby rejestrów = `FrameError(kind="echo")`.
"""
from __future__ import annotations

from typing import Sequence

FC_READ = 0x03
FC_READ_INPUT = 0x04
READ_FUNCTIONS = (FC_READ, FC_READ_INPUT)     # holding, input — odpowiedź tego samego kształtu
FC_WRITE_SINGLE = 0x06
FC_WRITE_MULTIPLE = 0x10
MAX_READ_REGISTERS = 125
MAX_WRITE_REGISTERS = 123
_MBAP_HEADER = 7            # TID(2) + protokół(2) + długość(2) + jednostka(1)
_MAX_PDU = 253


class FrameError(ValueError):
    def __init__(self, kind: str, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.code = code


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _with_crc(body: bytes) -> bytes:
    c = crc16(body)
    return body + bytes([c & 0xFF, c >> 8])


def _crc_ok(frame: bytes, n: int) -> bool:
    """CRC (lo, hi) zaraz za `n` bajtami ramki."""
    return crc16(frame[:n]) == (frame[n] | frame[n + 1] << 8)


def _int_in(name: str, v: int, lo: int, hi: int) -> None:
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise ValueError(f"{name} poza zakresem {lo}..{hi}: {v!r}")


def _be16(v: int) -> bytes:
    return bytes([v >> 8, v & 0xFF])


# ── budowanie ─────────────────────────────────────────────────────────────


def _read_function(fc: int) -> None:
    if isinstance(fc, bool) or not isinstance(fc, int) or fc not in READ_FUNCTIONS:
        raise ValueError(f"funkcja odczytu spoza {list(READ_FUNCTIONS)}: {fc!r}")


def pdu_read(addr: int, count: int, fc: int = FC_READ) -> bytes:
    _read_function(fc)
    _int_in("adres rejestru", addr, 0, 0xFFFF)
    _int_in("liczba rejestrów", count, 1, MAX_READ_REGISTERS)
    return bytes([fc]) + _be16(addr) + _be16(count)


def pdu_write_single(addr: int, value: int) -> bytes:
    _int_in("adres rejestru", addr, 0, 0xFFFF)
    _int_in("wartość rejestru", value, 0, 0xFFFF)
    return bytes([FC_WRITE_SINGLE]) + _be16(addr) + _be16(value)


def pdu_write_multiple(addr: int, values: Sequence[int]) -> bytes:
    _int_in("adres rejestru", addr, 0, 0xFFFF)
    n = len(values)
    _int_in("liczba rejestrów zapisu", n, 1, MAX_WRITE_REGISTERS)
    if addr + n > 0x10000:
        raise ValueError("zapis wychodzi poza przestrzeń adresów")
    for v in values:
        _int_in("wartość rejestru", v, 0, 0xFFFF)
    return (bytes([FC_WRITE_MULTIPLE]) + _be16(addr) + _be16(n) + bytes([2 * n])
            + b"".join(_be16(v) for v in values))


def rtu(unit: int, pdu: bytes) -> bytes:
    """Goła ramka RTU: jednostka + PDU + CRC."""
    _int_in("adres jednostki", unit, 0, 0xFF)
    return _with_crc(bytes([unit]) + pdu)


def read_request(unit: int, addr: int, count: int, fc: int = FC_READ) -> bytes:
    return rtu(unit, pdu_read(addr, count, fc))


def write_single_request(unit: int, addr: int, value: int) -> bytes:
    return rtu(unit, pdu_write_single(addr, value))


def write_multiple_request(unit: int, addr: int, values: Sequence[int]) -> bytes:
    return rtu(unit, pdu_write_multiple(addr, values))


def mbap(tid: int, unit: int, pdu: bytes) -> bytes:
    """Ramka Modbus TCP: nagłówek MBAP (protokół 0) + PDU, bez CRC."""
    _int_in("identyfikator transakcji", tid, 0, 0xFFFF)
    _int_in("adres jednostki", unit, 0, 0xFF)
    _int_in("długość PDU", len(pdu), 1, _MAX_PDU)
    return _be16(tid) + b"\x00\x00" + _be16(len(pdu) + 1) + bytes([unit]) + pdu


# ── rozbiór ───────────────────────────────────────────────────────────────


def _rtu_exception(frame: bytes) -> None:
    # Wyjątek Modbus: adres, funkcja(|0x80), kod, CRC(lo, hi) — dowolny kod funkcji z bitem błędu.
    if len(frame) < 5:
        raise FrameError("short", "ramka wyjątku krótsza niż 5 bajtów")
    if not _crc_ok(frame, 3):
        raise FrameError("crc", "CRC się nie zgadza")
    raise FrameError("exception", f"wyjątek Modbus {frame[2]}", code=frame[2])


def _rtu_head(frame: bytes, unit: int) -> int:
    if len(frame) < 2:
        raise FrameError("short", "ramka krótsza niż adres i funkcja")
    if frame[0] != unit:
        raise FrameError("header", "obcy adres jednostki")
    fc = frame[1]
    if fc & 0x80:
        _rtu_exception(frame)
    return fc


def _words(data: bytes) -> list[int]:
    return [int.from_bytes(data[i:i + 2], "big") for i in range(0, len(data), 2)]


def parse_rtu_read(rtu_frame: bytes, unit: int, count: int, fc: int = FC_READ) -> list[int]:
    # Lustro `mb_parse_read_response` z firmware'u Boxa: kolejność kontroli i traktowanie
    # bajtów za deklarowaną długością (ramka może mieć nadmiarowe wypełnienie) muszą się zgadzać.
    # `fc` — funkcja żądania (3 albo 4); odpowiedź innej funkcji odczytu to nie nasza odpowiedź.
    got = _rtu_head(rtu_frame, unit)
    if got != fc:
        raise FrameError("function", f"nieoczekiwana funkcja 0x{got:02x}")
    if len(rtu_frame) < 3:
        raise FrameError("short", "ramka krótsza niż nagłówek odczytu")
    n = rtu_frame[2]
    if len(rtu_frame) < 3 + n + 2:
        raise FrameError("short", "ramka krótsza niż zadeklarowane dane")
    if not _crc_ok(rtu_frame, 3 + n):
        raise FrameError("crc", "CRC się nie zgadza")
    if n != count * 2:
        raise FrameError("length", f"oczekiwano {count * 2} B danych, jest {n}")
    return _words(rtu_frame[3:3 + n])


def _aa55(frame: bytes) -> bytes:
    if len(frame) < 2:
        raise FrameError("short", "ramka krótsza niż nagłówek AA55")
    if frame[0:2] != b"\xaa\x55":
        raise FrameError("header", "brak nagłówka AA55")
    return frame[2:]


def parse_aa55_read(frame: bytes, unit: int, count: int) -> list[int]:
    return parse_rtu_read(_aa55(frame), unit, count)


def _check_echo(body: bytes, fc: int, addr: int, value_or_count: int) -> None:
    """`body` = 4 bajty echa zapisu: adres i wartość (FC 6) albo liczba rejestrów (FC 16)."""
    got_addr = int.from_bytes(body[0:2], "big")
    got_val = int.from_bytes(body[2:4], "big")
    if got_addr != addr or got_val != value_or_count:
        what = "wartości" if fc == FC_WRITE_SINGLE else "liczby rejestrów"
        raise FrameError("echo", f"echo innego rejestru albo {what}")


def parse_rtu_write(rtu_frame: bytes, unit: int, fc: int, addr: int, value_or_count: int) -> None:
    """Potwierdzenie zapisu (echo FC 6 albo odpowiedź FC 16); wyjątek przy czymkolwiek innym."""
    got = _rtu_head(rtu_frame, unit)
    if got != fc:
        raise FrameError("function", f"nieoczekiwana funkcja 0x{got:02x}")
    if len(rtu_frame) < 8:
        raise FrameError("short", "ramka krótsza niż potwierdzenie zapisu")
    if not _crc_ok(rtu_frame, 6):
        raise FrameError("crc", "CRC się nie zgadza")
    _check_echo(rtu_frame[2:6], fc, addr, value_or_count)


def parse_aa55_write(frame: bytes, unit: int, addr: int, value: int) -> None:
    parse_rtu_write(_aa55(frame), unit, FC_WRITE_SINGLE, addr, value)


def _pdu_head(pdu: bytes) -> int:
    if len(pdu) < 1:
        raise FrameError("short", "pusty PDU")
    fc = pdu[0]
    if fc & 0x80:
        if len(pdu) < 2:
            raise FrameError("short", "wyjątek bez kodu")
        raise FrameError("exception", f"wyjątek Modbus {pdu[1]}", code=pdu[1])
    return fc


def parse_pdu_read(pdu: bytes, count: int, fc: int = FC_READ) -> list[int]:
    got = _pdu_head(pdu)
    if got != fc:
        raise FrameError("function", f"nieoczekiwana funkcja 0x{got:02x}")
    if len(pdu) < 2:
        raise FrameError("short", "PDU krótszy niż nagłówek odczytu")
    n = pdu[1]
    if len(pdu) < 2 + n:
        raise FrameError("short", "PDU krótszy niż zadeklarowane dane")
    if n != count * 2:
        raise FrameError("length", f"oczekiwano {count * 2} B danych, jest {n}")
    return _words(pdu[2:2 + n])


def parse_pdu_write(pdu: bytes, fc: int, addr: int, value_or_count: int) -> None:
    got = _pdu_head(pdu)
    if got != fc:
        raise FrameError("function", f"nieoczekiwana funkcja 0x{got:02x}")
    if len(pdu) < 5:
        raise FrameError("short", "PDU krótszy niż potwierdzenie zapisu")
    _check_echo(pdu[1:5], fc, addr, value_or_count)


def parse_mbap(frame: bytes) -> tuple[int, int, bytes]:
    """Ramka Modbus TCP → (TID, jednostka, PDU); protokół musi być 0, długość zgodna z ramką."""
    if len(frame) < _MBAP_HEADER:
        raise FrameError("short", "ramka krótsza niż nagłówek MBAP")
    if frame[2:4] != b"\x00\x00":
        raise FrameError("protocol", "identyfikator protokołu różny od 0")
    length = int.from_bytes(frame[4:6], "big")
    if length < 2 or len(frame) != 6 + length:
        raise FrameError("length", "długość MBAP niezgodna z ramką")
    return int.from_bytes(frame[0:2], "big"), frame[6], frame[_MBAP_HEADER:]


def rtu_frame_length(prefix: bytes) -> int | None:
    """Pełna długość ramki RTU w strumieniu z jej pierwszych bajtów; None = za mało bajtów.

    Wyjątek: 5, odczyt FC 3/4: 5 + liczba bajtów danych, potwierdzenie zapisu FC 6/16: 8.
    Inna funkcja nie da się odciąć w strumieniu → `FrameError(kind="function")`.
    """
    if len(prefix) < 3:
        return None
    fc = prefix[1]
    if fc & 0x80:
        return 5
    if fc in READ_FUNCTIONS:
        return 5 + prefix[2]
    if fc in (FC_WRITE_SINGLE, FC_WRITE_MULTIPLE):
        return 8
    raise FrameError("function", f"nieobsługiwana funkcja 0x{fc:02x} w strumieniu")
