"""Klient rejestrów: odczyt stanu według planu bloków, pojedyncze rejestry, tożsamość urządzenia.

Blok z wyjątkiem 2 (dziura w mapie urządzenia wewnątrz scalonego bloku) jest dzielony na
zakresy kluczy i czytany ponownie; inny błąd bloku zostawia jego klucze bez odczytu (None),
a `LinkDown` przerywa cykl (kolejne bloki i tak by nie przeszły). Gdy nie udał się ŻADEN
blok, `read_state` rzuca ostatni błąd — wołający zatrzymuje poprzedni odczyt zamiast
dostać „świeży” odczyt bez wartości.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable

from ..registers import RegisterImage
from ..transports.base import LinkDown, ModbusException, RegisterTransport, TransportError
from .blocks import read_plan, split_block
from .identity import device_fingerprint, identity_fields
from .reading import DirectReading, build_reading


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RegisterClient:
    def __init__(self, transport: RegisterTransport, profile, *, clock: Callable[[], float] = time.monotonic,
                 utcnow: Callable[[], datetime] = _utcnow, salt: bytes | None = None) -> None:
        self.transport = transport
        self.profile = profile
        self._clock = clock
        self._utcnow = utcnow
        self._salt = salt
        self.last_frames: list[dict] = []       # {"addr","count","ok"} — bez bajtów ramek

    async def read_block(self, addr: int, count: int) -> list[int]:
        return await self.transport.read(addr, count)

    async def read_register(self, addr: int) -> int:
        return (await self.transport.read(addr, 1))[0]

    async def read_state(self) -> DirectReading:
        frames: list[dict] = []
        blocks: dict[int, list[int]] = {}
        last_err: TransportError | None = None
        plan = read_plan(self.profile)
        for i, block in enumerate(plan):
            try:
                blocks[block[0]] = await self.read_block(*block)
                frames.append(_frame(block, True))
                continue
            except ModbusException as err:
                frames.append(_frame(block, False))
                last_err = err
                if err.code == 2:
                    last_err = await self._read_split(block, blocks, frames) or last_err
            except LinkDown as err:
                frames.append(_frame(block, False))
                frames.extend(_frame(b, False) for b in plan[i + 1:])
                last_err = err
                break
            except TransportError as err:
                frames.append(_frame(block, False))
                last_err = err
        self.last_frames = frames
        if not blocks:
            raise last_err or TransportError("nothing read")
        return build_reading(self.profile, RegisterImage.from_blocks(blocks),
                             at_mono=self._clock(), at_utc=self._utcnow())

    async def _read_split(self, block, blocks, frames) -> TransportError | None:
        """Blok z dziurą nieobsługiwaną przez urządzenie → zakresy kluczy osobno."""
        err_out = None
        for sub in split_block(block, self.profile):
            if sub == tuple(block):
                continue
            try:
                blocks[sub[0]] = await self.read_block(*sub)
                frames.append(_frame(sub, True))
            except TransportError as err:
                frames.append(_frame(sub, False))
                err_out = err
                if isinstance(err, LinkDown):
                    break
        return err_out

    async def read_identity(self) -> str | None:
        """Odcisk urządzenia (R28) z `identify_reads`; None = rejestrów nie da się odczytać."""
        if not self._salt:
            raise ValueError("identity needs the installation salt")
        blocks: dict[int, list[int]] = {}
        for addr, count in self.profile.modbus.identify_reads:
            try:
                blocks[addr] = await self.read_block(addr, count)
            except TransportError:
                return None
        fields = identity_fields(self.profile, RegisterImage.from_blocks(blocks))
        return device_fingerprint(self._salt, self.profile.id, fields)


def _frame(block, ok: bool) -> dict:
    return {"addr": block[0], "count": block[1], "ok": ok}
