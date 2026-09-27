"""Klient rejestrów: odczyt stanu według planu bloków, pojedyncze rejestry, tożsamość urządzenia.

* Rejestry bez odczytu (`unreadable`, np. odpowiadające ramką złej długości) nie są odpytywane
  wcale — co cykl kosztowałyby `read_tries × timeout` łącza i fałszywe „obce ramki”.
* Blok z wyjątkiem 2 (dziura w mapie urządzenia wewnątrz scalonego bloku) jest dzielony na
  zakresy kluczy i czytany ponownie; inny błąd bloku zostawia jego klucze bez odczytu (None).
* `LinkDown` albo cisza (przekroczenie czasu bez ŻADNEJ ramki) przerywa cykl — kolejne bloki
  i tak by nie przeszły, a milczący falownik na UDP nie może kosztować minuty na cykl.
* Cykl ma limit czasu STRACONEGO na przekroczenia czasu (odpowiedzi się nie liczą — wolne łącze
  z poprawnymi odpowiedziami czyta wszystko; próby przekroczone przed udaną liczą się, z licznika
  `stats.timeouts` transportu): co najmniej `read_tries × timeout` transportu, żeby
  pojedynczy blok zawsze miał pełne próby. Liczba prób bloku jest przycinana do pozostałego
  limitu, a po jego wyczerpaniu reszta bloków jest oznaczana jako nieudana. Tożsamość nie ma
  limitu (1–3 bloki; cisza i zerwanie i tak przerywają).
* Gdy nie udał się ŻADEN blok, `read_state` rzuca ostatni błąd — wołający zatrzymuje poprzedni
  odczyt zamiast dostać „świeży” odczyt bez wartości.
* `at_mono` odczytu to chwila STARTU cyklu (bloki nie są atomowe — zapis mógł wejść między nie).
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Callable, Iterable

from ..registers import RegisterImage
from ..transports.base import LinkDown, ModbusException, RegisterTransport, RequestTimeout, TransportError
from .blocks import read_plan, split_block
from .identity import device_fingerprint
from .reading import DirectReading, build_reading

DEFAULT_CYCLE_BUDGET_S = 5.0
_DEFAULT_TIMEOUT_S = 2.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _OutOfTime(TransportError):
    """Limit czasu cyklu odczytu wyczerpany — nic nie wysłano."""


def _aborts(err: TransportError) -> bool:
    """Błąd, po którym dalsze bloki cyklu nie mają sensu."""
    return isinstance(err, (LinkDown, _OutOfTime)) or (isinstance(err, RequestTimeout) and err.silent)


class RegisterClient:
    def __init__(self, transport: RegisterTransport, profile, *, clock: Callable[[], float] = time.monotonic,
                 utcnow: Callable[[], datetime] = _utcnow, salt: bytes | None = None,
                 unreadable: Iterable[str] = (), cycle_budget_s: float = DEFAULT_CYCLE_BUDGET_S) -> None:
        self.transport = transport
        self.profile = profile
        self._clock = clock
        self._utcnow = utcnow
        self._salt = salt
        self.unreadable: frozenset[str] = frozenset(unreadable)
        self.cycle_budget_s = cycle_budget_s
        self.last_frames: list[dict] = []       # {"addr","count","ok"} — bez bajtów ramek

    async def read_block(self, addr: int, count: int, *, tries: int | None = None) -> list[int]:
        return await self.transport.read(addr, count, tries=tries)

    async def read_register(self, addr: int) -> int:
        return (await self.transport.read(addr, 1))[0]

    def _link(self) -> tuple[float, int]:
        cfg = getattr(self.transport, "cfg", None)
        return getattr(cfg, "timeout_s", _DEFAULT_TIMEOUT_S), getattr(cfg, "read_tries", 1)

    async def _timed_read(self, block, lost: list[float]) -> list[int]:
        """Odczyt bloku z liczbą prób przyciętą do limitu czasu straconego w tym cyklu.

        `lost[0]` — czas dotąd stracony na przekroczenia czasu (aktualizowany tutaj).
        """
        timeout, read_tries = self._link()
        budget = max(self.cycle_budget_s, read_tries * timeout)
        remaining = budget - lost[0]
        if remaining < timeout:
            raise _OutOfTime("read cycle out of time")
        tries = max(1, min(read_tries, math.floor(remaining / timeout)))
        stats = getattr(self.transport, "stats", None)
        before = getattr(stats, "timeouts", 0)
        t0 = self._clock()
        try:
            out = await self.read_block(*block, tries=tries)
        except RequestTimeout:
            lost[0] += max(max(0.0, self._clock() - t0), self._timed_out(stats, before) * timeout)
            raise
        # Próby, które przekroczyły czas przed udaną, też są czasem straconym.
        lost[0] += self._timed_out(stats, before) * timeout
        return out

    @staticmethod
    def _timed_out(stats, before: int) -> int:
        after = getattr(stats, "timeouts", before)
        return max(0, after - before) if isinstance(after, int) else 0

    # ── stan ──

    async def read_state(self) -> DirectReading:
        started = self._clock()
        started_utc = self._utcnow()
        lost = [0.0]
        frames: list[dict] = []
        blocks: dict[int, list[int]] = {}
        last_err: TransportError | None = None
        plan = read_plan(self.profile, exclude=self.unreadable)
        for i, block in enumerate(plan):
            try:
                blocks[block[0]] = await self._timed_read(block, lost)
                frames.append(_frame(block, True))
                continue
            except TransportError as err:
                frames.append(_frame(block, False))
                last_err = err
                if isinstance(err, ModbusException) and err.code == 2:
                    last_err = await self._read_split(block, blocks, frames, lost) or err
            if _aborts(last_err):
                frames.extend(_frame(b, False) for b in plan[i + 1:])
                break
        self.last_frames = frames
        if not blocks:
            raise last_err or TransportError("nothing read")
        return build_reading(self.profile, RegisterImage.from_blocks(blocks),
                             at_mono=started, at_utc=started_utc)

    async def _read_split(self, block, blocks, frames, lost) -> TransportError | None:
        """Blok z dziurą nieobsługiwaną przez urządzenie → zakresy kluczy osobno.

        Zwraca ostatni błąd; błąd przerywający cykl kończy też podział.
        """
        err_out = None
        for sub in split_block(block, self.profile):
            if sub == tuple(block):
                continue
            try:
                blocks[sub[0]] = await self._timed_read(sub, lost)
                frames.append(_frame(sub, True))
            except TransportError as err:
                frames.append(_frame(sub, False))
                err_out = err
                if _aborts(err):
                    break
        return err_out

    # ── tożsamość ──

    async def read_identity(self) -> str | None:
        """Odcisk urządzenia z rejestrów identyfikacyjnych; None = nieczytelne albo nierozpoznane."""
        if not self._salt:
            raise ValueError("identity needs the installation salt")
        blocks: dict[int, list[int]] = {}
        for addr, count in self.profile.modbus.identify_reads:
            try:
                blocks[addr] = await self.read_block(addr, count)
            except TransportError:
                return None
        return device_fingerprint(self._salt, self.profile, RegisterImage.from_blocks(blocks))


def _frame(block, ok: bool) -> dict:
    return {"addr": block[0], "count": block[1], "ok": ok}
