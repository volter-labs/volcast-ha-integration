"""Maskowanie nagranych ramek przed wyjściem z urządzenia (diagnostyka, złote wektory).

Rejestry seryjne profilu (każdy klucz `identify.registers` z `serial` w nazwie) są zerowane
w danych odpowiedzi, a sumy kontrolne przeliczane (CRC RTU, suma V5), więc ramka po masce dalej
przechodzi parsery. Numer loggera Solarman (bajty 7–10 nagłówka V5) jest zerowany w żądaniu i
odpowiedzi. Ramka, której budowy nie rozpoznajemy, nie wychodzi wcale (None) — nie zgadujemy,
gdzie mógłby być numer seryjny.
"""
from __future__ import annotations

from typing import Iterable, Mapping

from ..transports.modbus_frames import crc16

_V5_HEADER = 11
_V5_REQ_HEAD = 15
_V5_RESP_HEAD = 14


def serial_addresses(profile) -> frozenset[int]:
    regs = ((profile.raw.get("identify") or {}).get("registers") or {})
    out: set[int] = set()
    for name, spec in regs.items():
        if "serial" in name and isinstance(spec, Mapping) and isinstance(spec.get("addr"), int):
            n = spec.get("len") or spec.get("count") or 1
            out.update(range(spec["addr"], spec["addr"] + int(n)))
    return frozenset(out)


def _mask_rtu(rtu: bytearray, offset: int, count: int, serials: frozenset[int]) -> bool:
    """RTU odczytu (jednostka, funkcja, liczba bajtów, dane, CRC) — maska w miejscu; False = nierozpoznana."""
    if len(rtu) < 5 or rtu[1] != 0x03:
        return rtu[1:2] == b"\x83" and len(rtu) >= 5   # wyjątek Modbus: bez danych
    n = rtu[2]
    if n != 2 * count or len(rtu) < 3 + n + 2:
        return False
    for i in range(count):
        if offset + i in serials:
            rtu[3 + 2 * i:5 + 2 * i] = b"\x00\x00"
    crc = crc16(bytes(rtu[:3 + n]))
    rtu[3 + n], rtu[4 + n] = crc & 0xFF, crc >> 8
    return True


def _mask_mbap(frame: bytearray, offset: int, count: int, serials: frozenset[int]) -> bool:
    if len(frame) < 9:
        return False
    if frame[7] != 0x03:
        return frame[7] == 0x83
    n = frame[8]
    if n != 2 * count or len(frame) < 9 + n:
        return False
    for i in range(count):
        if offset + i in serials:
            frame[9 + 2 * i:11 + 2 * i] = b"\x00\x00"
    return True


def _v5_fix(frame: bytearray) -> None:
    frame[7:11] = b"\x00\x00\x00\x00"                    # numer loggera
    frame[-2] = sum(frame[1:-2]) & 0xFF


def _mask_v5(frame: bytearray, offset: int, count: int, serials: frozenset[int], head: int) -> bool:
    if len(frame) < _V5_HEADER + head + 2 or frame[0] != 0xA5 or frame[-1] != 0x15:
        return False
    rtu = bytearray(frame[_V5_HEADER + head:-2])
    if head == _V5_RESP_HEAD and rtu and not _mask_rtu(rtu, offset, count, serials):
        return False
    frame[_V5_HEADER + head:-2] = rtu
    _v5_fix(frame)
    return True


def redact_frame(kind: str, offset: int, count: int, request: bytes, response: bytes | None,
                 serials: frozenset[int]) -> tuple[bytes | None, bytes | None]:
    """(żądanie, odpowiedź) po masce; None w miejscu ramki nierozpoznanej."""
    req = bytearray(request)
    resp = None if response is None else bytearray(response)
    if kind == "solarman_v5":
        req_ok = _mask_v5(req, offset, count, serials, _V5_REQ_HEAD)
        resp_ok = resp is None or _mask_v5(resp, offset, count, serials, _V5_RESP_HEAD)
    elif kind == "goodwe_udp":
        req_ok = True
        body = bytearray(resp[2:]) if resp is not None else None
        resp_ok = resp is None or (resp[:2] == b"\xaa\x55" and _mask_rtu(body, offset, count, serials))
        if resp is not None and resp_ok:
            resp[2:] = body
    elif kind == "modbus_rtu":
        req_ok = True
        resp_ok = resp is None or _mask_rtu(resp, offset, count, serials)
    elif kind == "modbus_tcp":
        req_ok = True
        resp_ok = resp is None or _mask_mbap(resp, offset, count, serials)
    else:
        return None, None
    return (bytes(req) if req_ok else None), (bytes(resp) if resp is not None and resp_ok else None)


def redact_frames(kind: str, frames: Iterable[Mapping], profile) -> list[dict]:
    """Nagrane ramki klienta → lista `{"offset","count","request","response"}` hex po masce."""
    serials = serial_addresses(profile)
    out = []
    for f in frames:
        req, resp = redact_frame(kind, int(f["offset"]), int(f["count"]), f["request"], f.get("response"), serials)
        out.append({"offset": int(f["offset"]), "count": int(f["count"]),
                    "request": req.hex() if req is not None else None,
                    "response": resp.hex() if resp is not None else None})
    return out
