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
    return _with_crc(bytes([unit, FC_READ, addr >> 8, addr & 0xFF, count >> 8, count & 0xFF]))


def parse_aa55_read(frame: bytes, unit: int, count: int) -> list[int]:
    if len(frame) < 7:
        raise FrameError("short", "ramka krótsza niż nagłówek")
    if frame[0:2] != b"\xaa\x55" or frame[2] != unit:
        raise FrameError("header", "brak AA55 albo obcy adres")
    if crc16(frame[2:-2]) != (frame[-2] | frame[-1] << 8):
        raise FrameError("crc", "CRC się nie zgadza")
    fc = frame[3]
    if fc == (FC_READ | 0x80):
        # Wyjątek 2 = „nie znam rejestru" — dla próby możliwości to odpowiedź, nie awaria.
        raise FrameError("exception", f"wyjątek Modbus {frame[4]}", code=frame[4])
    if fc != FC_READ:
        raise FrameError("function", f"nieoczekiwana funkcja 0x{fc:02x}")
    n = frame[4]
    data = frame[5:-2]
    if n != count * 2 or len(data) != n:
        raise FrameError("length", f"oczekiwano {count * 2} B danych, jest {len(data)}")
    return [int.from_bytes(data[i:i + 2], "big") for i in range(0, n, 2)]
