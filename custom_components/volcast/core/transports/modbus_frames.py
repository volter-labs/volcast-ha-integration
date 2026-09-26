"""Ramki Modbus RTU i opakowanie AA55 GoodWe — czyste funkcje, bez gniazd.

Kolejność kontroli odpowiedzi: długość, nagłówek, CRC, kod funkcji, długość danych.
"""
from __future__ import annotations

FC_READ = 0x03


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


def read_request(unit: int, addr: int, count: int) -> bytes:
    if not 0 <= unit <= 0xFF:
        raise ValueError(f"adres jednostki poza zakresem 0..255: {unit}")
    if not 0 <= addr <= 0xFFFF:
        raise ValueError(f"adres rejestru poza zakresem 0..65535: {addr}")
    if not 1 <= count <= 125:
        raise ValueError(f"liczba rejestrów poza zakresem 1..125: {count}")
    return _with_crc(bytes([unit, FC_READ, addr >> 8, addr & 0xFF, count >> 8, count & 0xFF]))


def parse_aa55_read(frame: bytes, unit: int, count: int) -> list[int]:
    # Lustro `mb_parse_read_response` z firmware'u Boxa: kolejność kontroli i traktowanie
    # bajtów za deklarowaną długością (ramka może mieć nadmiarowe wypełnienie) muszą się zgadzać.
    if len(frame) < 2:
        raise FrameError("short", "ramka krótsza niż nagłówek AA55")
    if frame[0:2] != b"\xaa\x55":
        raise FrameError("header", "brak nagłówka AA55")
    rtu = frame[2:]
    if len(rtu) < 2:
        raise FrameError("short", "ramka krótsza niż adres i funkcja")
    if rtu[0] != unit:
        raise FrameError("header", "obcy adres jednostki")
    fc = rtu[1]
    if fc & 0x80:
        # Wyjątek Modbus: adres, funkcja(|0x80), kod, CRC(lo, hi) — dowolny kod funkcji z bitem błędu.
        if len(rtu) < 5:
            raise FrameError("short", "ramka wyjątku krótsza niż 5 bajtów")
        if crc16(rtu[:3]) != (rtu[3] | rtu[4] << 8):
            raise FrameError("crc", "CRC się nie zgadza")
        raise FrameError("exception", f"wyjątek Modbus {rtu[2]}", code=rtu[2])
    if fc != FC_READ:
        raise FrameError("function", f"nieoczekiwana funkcja 0x{fc:02x}")
    if len(rtu) < 3:
        raise FrameError("short", "ramka krótsza niż nagłówek odczytu")
    n = rtu[2]
    needed = 3 + n + 2
    if len(rtu) < needed:
        raise FrameError("short", "ramka krótsza niż zadeklarowane dane")
    if crc16(rtu[:3 + n]) != (rtu[3 + n] | rtu[3 + n + 1] << 8):
        raise FrameError("crc", "CRC się nie zgadza")
    if n != count * 2:
        raise FrameError("length", f"oczekiwano {count * 2} B danych, jest {n}")
    data = rtu[3:3 + n]
    return [int.from_bytes(data[i:i + 2], "big") for i in range(0, n, 2)]
