"""Moduł Wi-Fi GoodWe (UDP 8899, AA55) z zachowaniami zmierzonymi na żywym falowniku.

* `replay` — odpowiedź na żądanie to POPRZEDNIA odpowiedź tej samej funkcji (identyczna ramka, bez
  wykonania żądania); kilka `replay` z rzędu = ta sama nieaktualna ramka kilka razy;
* obce ramki innych klientów (FC 3 o długości 45/125/24 słów, także 4 słowa jak blok DOD Boxa)
  dorzucane przed odpowiedzią;
* `late` — odpowiedź po `late_s` (ponad limit czasu transportu); `drop` — bez odpowiedzi;
  `exc2` — jednorazowy wyjątek 2 zamiast odpowiedzi (staje się „poprzednią odpowiedzią”);
* `apply_lag` — pierwsze N odczytów obejmujących zapisany rejestr pokazuje wartość sprzed zapisu.

Plan działań: `plan` dla kolejnych odczytów, `write_plan` dla kolejnych zapisów (zużywane po kolei,
potem „ok”); `foreign` — numer żądania (od 1, wszystkie funkcje) → liczby słów obcych ramek przed
odpowiedzią. Ramki składane niezależnie od kodu transportu.
"""
from __future__ import annotations

import asyncio
import struct

from .device import UNREADABLE, Faults, RegisterBank, crc_ok, handle_pdu, with_crc
from .servers import UdpServer

FOREIGN_LENGTHS = (45, 125, 24)                 # odczyty innego klienta zmierzone na żywo


def aa55(unit: int, pdu: bytes) -> bytes:
    return b"\xaa\x55" + with_crc(bytes([unit]) + pdu)


def read_pdu(words) -> bytes:
    return bytes([0x03, 2 * len(words)]) + b"".join(struct.pack(">H", w & 0xFFFF) for w in words)


class FlakyModule(UdpServer):
    def __init__(self, bank: RegisterBank, unit: int = 0xF7, *, late_s: float = 0.5) -> None:
        super().__init__(bank, Faults(), unit)
        self.plan: list[str] = []                  # działania dla kolejnych ODCZYTÓW (FC 3)
        self.write_plan: list[str] = []            # działania dla kolejnych zapisów
        self.foreign: dict[int, tuple[int, ...]] = {}
        self.foreign_word = 0x1234
        self.late_s = late_s
        self.apply_lag = 0
        self._lag: dict[int, list[int]] = {}       # rejestr → [stara wartość, pozostałe odczyty]
        self._last: dict[int, bytes] = {}          # funkcja → ostatnia wysłana odpowiedź
        self.actions: list[str] = []               # wykonane działania (do asercji)

    def inject(self, frame: bytes) -> None:
        """Wyślij ramkę do każdego znanego klienta (np. nieaktualna odpowiedź w kolejce gniazda)."""
        for peer in self._peers:
            self._transport.sendto(frame, peer)

    def _read(self, addr: int, count: int) -> bytes:
        r = self.bank.read(addr, count)
        if r == UNREADABLE:
            return bytes([0x03, 2 * count + 2]) + bytes(2 * count + 2)
        if isinstance(r, int):
            return bytes([0x83, r])
        words = list(r)
        for i, a in enumerate(range(addr, addr + count)):
            lag = self._lag.get(a)
            if lag is not None and lag[1] > 0:
                lag[1] -= 1
                words[i] = lag[0]
        return read_pdu(words)

    def _execute(self, pdu: bytes) -> bytes:
        fc = pdu[0]
        addr, second = struct.unpack(">HH", pdu[1:5])
        if fc == 0x03:
            return self._read(addr, second)
        old = self.bank.read(addr, 1)
        resp, _ = handle_pdu(self.bank, pdu, self.faults)
        if not resp[0] & 0x80 and isinstance(old, list) and self.apply_lag:
            self._lag[addr] = [old[0], self.apply_lag]
        return resp

    def on_datagram(self, data: bytes, addr) -> None:
        if len(data) < 8 or data[0] != self.unit or not crc_ok(data):
            return
        if addr not in self._peers:
            self._peers.add(addr)
            self.clients += 1
        pdu = data[1:-2]
        self.requests += 1
        self.log.append((pdu[0], struct.unpack(">H", pdu[1:3])[0], struct.unpack(">H", pdu[3:5])[0]))
        queue = self.plan if pdu[0] == 0x03 else self.write_plan
        action = queue.pop(0) if queue else "ok"
        self.actions.append(action)
        frames = [aa55(self.unit, read_pdu([self.foreign_word] * n)) for n in self.foreign.pop(self.requests, ())]
        if action == "replay" and pdu[0] in self._last:
            frames.append(self._last[pdu[0]])
        elif action == "exc2":                     # jednorazowy wyjątek 2 (bez wykonania żądania)
            reply = aa55(self.unit, bytes([pdu[0] | 0x80, 2]))
            self._last[pdu[0]] = reply
            frames.append(reply)
        elif action != "drop":
            reply = aa55(self.unit, self._execute(pdu))
            self._last[pdu[0]] = reply
            if action != "late":
                frames.append(reply)
            else:
                self._handles.append(asyncio.get_running_loop().call_later(
                    self.late_s, lambda: self._send_to(addr, [reply])))
        self._send_to(addr, frames)

    def _send_to(self, addr, frames) -> None:
        if self._transport is not None and not self._transport.is_closing():
            for fr in frames:
                self._transport.sendto(fr, addr)


async def flaky_module(bank: RegisterBank, **kw) -> FlakyModule:
    return await FlakyModule(bank, **kw).start()
