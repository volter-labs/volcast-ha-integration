"""Symulowane urządzenie: bank rejestrów, usterki i obsługa PDU Modbus.

Kod celowo niezależny od `core/transports/*_frames.py` (własne CRC w wersji tablicowej,
własne składanie odpowiedzi) — symulator nie może potwierdzać sam siebie.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

UNREADABLE = -1          # `RegisterBank.read`: odczyt da ramkę niepoprawnej długości


def _crc_table() -> list[int]:
    table = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
        table.append(c)
    return table


_TABLE = _crc_table()


def crc16_table(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc = (crc >> 8) ^ _TABLE[(crc ^ b) & 0xFF]
    return crc


def with_crc(body: bytes) -> bytes:
    return body + struct.pack("<H", crc16_table(body))


def crc_ok(frame: bytes) -> bool:
    return len(frame) >= 4 and struct.unpack("<H", frame[-2:])[0] == crc16_table(frame[:-2])


class RegisterBank:
    """Rejestry urządzenia. Adres spoza mapy czyta się jako 0 (jak w falownikach)."""

    def __init__(self, words: Mapping[int, int], *, unsupported: Iterable[int] = (),
                 unreadable: Iterable[int] = (), clamp: Mapping[int, tuple[int, int]] | None = None,
                 ignore_writes: Iterable[int] = ()) -> None:
        self._w = {int(a): int(v) & 0xFFFF for a, v in words.items()}
        self.unsupported = set(unsupported)
        self.unreadable = set(unreadable)
        self.clamp = dict(clamp or {})
        self.ignore_writes = set(ignore_writes)
        self.writes: list[tuple[int, int]] = []

    def read(self, addr: int, count: int) -> list[int] | int:
        """Słowa albo kod wyjątku Modbus (int ≥ 1), albo `UNREADABLE`."""
        if count < 1 or count > 125 or addr + count > 0x10000:
            return 3
        span = range(addr, addr + count)
        if any(a in self.unsupported for a in span):
            return 2
        if any(a in self.unreadable for a in span):
            return UNREADABLE
        return [self._w.get(a, 0) for a in span]

    def write(self, addr: int, values: Sequence[int]) -> int | None:
        """None = przyjęte (echo), int = kod wyjątku."""
        span = range(addr, addr + len(values))
        if not values or addr + len(values) > 0x10000:
            return 3
        if any(a in self.unsupported for a in span):
            return 2
        for a, v in zip(span, values):
            self.writes.append((a, v))
            if a in self.ignore_writes:
                continue
            if a in self.clamp:
                lo, hi = self.clamp[a]
                v = min(max(v, lo), hi)
            self._w[a] = v & 0xFFFF
        return None

    def poke(self, addr: int, value: int) -> None:
        """Ktoś inny (właściciel, chmura producenta) zmienia rejestr."""
        self._w[addr] = value & 0xFFFF


@dataclass
class Faults:
    drop_next: int = 0              # nie odpowiadaj na N kolejnych żądań (i ich nie wykonuj)
    mute_write_echo: int = 0        # zapisz, ale nie odsyłaj echa (N razy)
    delay_s: float = 0.0
    late_duplicate: bool = False    # odeślij odpowiedź drugi raz po następnym żądaniu
    stray_every: int = 0            # co N-te żądanie dorzuć obcą ramkę przed właściwą
    garbage_next: int = 0           # N kolejnych odpowiedzi to śmieci
    wrong_echo_value: bool = False  # echo zapisu z inną wartością
    reset_after: int = 0            # TCP: zerwij połączenie po N żądaniach na połączeniu
    max_clients: int = 0            # TCP: 0 = bez limitu; 1 = kolejny klient od razu zamykany
    delay_only_next: int = 0        # >0: `delay_s` tylko dla N kolejnych odpowiedzi, potem bez opóźnienia
    exception_on_write: dict = field(default_factory=dict)   # rejestr → kod: zapisz, ale odpowiedz wyjątkiem
    chunked: bool = False           # TCP: odpowiedź w 3 kawałkach
    oversize_next: int = 0          # TCP: N kolejnych odpowiedzi to strumień ponad limit bufora klienta
    heartbeat_next: int = 0         # V5: N razy ramka protokołu loggera (heartbeat) przed odpowiedzią
    asleep_next: int = 0            # V5: N odpowiedzi loggera bez ramki RTU (falownik uśpiony)
    mute_write_addrs: set = field(default_factory=set)       # zapisy tych rejestrów zawsze bez echa

    def take_delay(self) -> float:
        """Opóźnienie następnej odpowiedzi (zużywa licznik `delay_only_next`)."""
        if self.delay_only_next > 0:
            self.delay_only_next -= 1
            d = self.delay_s
            if self.delay_only_next == 0:
                self.delay_s = 0.0
            return d
        return self.delay_s


GARBAGE = bytes.fromhex("deadbeef0bad")


def handle_pdu(bank: RegisterBank, pdu: bytes, faults: Faults) -> tuple[bytes, bool]:
    """PDU żądania → (PDU odpowiedzi, czy to zapis)."""
    if len(pdu) < 5:
        return bytes([(pdu[0] if pdu else 0) | 0x80, 3]), False
    fc = pdu[0]
    addr, second = struct.unpack(">HH", pdu[1:5])
    if fc == 0x03:
        r = bank.read(addr, second)
        if r == UNREADABLE:
            # Odpowiedź innej długości niż żądana (jak nagranie rejestru nieczytelnego).
            return bytes([0x03, 2 * second + 2]) + bytes(2 * second + 2), False
        if isinstance(r, int):
            return bytes([0x83, r]), False
        return bytes([0x03, 2 * len(r)]) + b"".join(struct.pack(">H", w) for w in r), False
    if fc == 0x06:
        code = bank.write(addr, [second])
        if code is None:
            code = faults.exception_on_write.pop(addr, None)
        if code is not None:
            return bytes([0x86, code]), True
        value = (second + 1) & 0xFFFF if faults.wrong_echo_value else second
        return bytes([0x06]) + struct.pack(">HH", addr, value), True
    if fc == 0x10:
        if len(pdu) < 6 or len(pdu) < 6 + pdu[5] or pdu[5] != 2 * second:
            return bytes([0x90, 3]), True
        values = [struct.unpack(">H", pdu[6 + 2 * i:8 + 2 * i])[0] for i in range(second)]
        code = bank.write(addr, values)
        if code is None:
            code = faults.exception_on_write.pop(addr, None)
        if code is not None:
            return bytes([0x90, code]), True
        count = second + 1 if faults.wrong_echo_value else second
        return bytes([0x10]) + struct.pack(">HH", addr, count), True
    return bytes([fc | 0x80, 1]), False


def rtu_request_length(buf: bytes) -> int | None:
    """Długość żądania RTU w strumieniu; None = za mało bajtów."""
    if len(buf) < 2:
        return None
    if buf[1] == 0x10:
        return None if len(buf) < 7 else 9 + buf[6]
    return 8
