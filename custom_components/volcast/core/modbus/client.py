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
* Łącze bez korelacji odpowiedzi (GoodWe UDP, `views.py`): kolejność bloków `poll_order` (sąsiednie
  różnej długości, pojedyncze rejestry zapisu bez znanego bloku na końcu), blok rozdzielający przed
  odczytem tej samej długości co poprzedni, wyjątek 2 bloku dopiero po ponowieniu (za blokiem
  rozdzielającym) dzieli blok, a wartość z podziału inna niż z ostatniego całego bloku wymaga drugiego,
  zgodnego odczytu (także bez wartości odniesienia, w pierwszym cyklu) — inaczej klucze zakresu
  zostają bez wartości (nie przesunięta wartość). Sprawdzenie długości, blok rozdzielający i odczyt
  (oraz ponowienie bloku, odczyt z podziału z potwierdzeniem) idą pod jedną sesją na wyłączność
  łącza — krótką, nie na cały cykl, żeby pisarz nie czekał sekund.
* `at_mono` odczytu to chwila STARTU cyklu (bloki nie są atomowe — zapis mógł wejść między nie).
"""
from __future__ import annotations

import contextlib
import math
import time
from datetime import datetime, timezone
from typing import Callable, Iterable

from ..registers import RegisterImage
from ..transports.base import LinkDown, ModbusException, RegisterTransport, RequestTimeout, TransportError
from .blocks import block_key, make_block, read_kwargs, read_plan, split_block
from .identity import device_fingerprint
from .reading import DirectReading, build_reading
from .views import needs_disambiguation, poll_order, prev_read_count, separators_for

DEFAULT_CYCLE_BUDGET_S = 5.0
_DEFAULT_TIMEOUT_S = 2.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _OutOfTime(TransportError):
    """Limit czasu cyklu odczytu wyczerpany — nic nie wysłano."""


def _aborts(err: TransportError) -> bool:
    """Błąd, po którym dalsze bloki cyklu nie mają sensu."""
    return isinstance(err, (LinkDown, _OutOfTime)) or (isinstance(err, RequestTimeout) and err.silent)


_RECORD_MAX = 64


class RegisterClient:
    def __init__(self, transport: RegisterTransport, profile, *, clock: Callable[[], float] = time.monotonic,
                 utcnow: Callable[[], datetime] = _utcnow, salt: bytes | None = None,
                 unreadable: Iterable[str] = (), cycle_budget_s: float = DEFAULT_CYCLE_BUDGET_S,
                 record: bool = False) -> None:
        self.transport = transport
        self.profile = profile
        self._clock = clock
        self._utcnow = utcnow
        self._salt = salt
        self.unreadable: frozenset[str] = frozenset(unreadable)
        self.cycle_budget_s = cycle_budget_s
        self.last_frames: list[dict] = []       # {"addr","count","ok"} — bez bajtów ramek
        # Tryb nagrywania (połączenie próbne): surowe ramki odczytów, ostatnia na blok (offset, count).
        # SUROWE — z numerem seryjnym i numerem loggera; wychodzą wyłącznie przez maskowanie diagnostyki.
        self.record = bool(record)
        self._raw: dict[tuple[int, int], tuple[bytes, bytes | None]] = {}
        if self.record and hasattr(transport, "recorder"):
            transport.recorder = self._on_frame
        # łącze bez korelacji odpowiedzi: kolejność bloków, wyjątek 2 bloku potwierdzany, podział ostrożny
        self._disambiguate = needs_disambiguation(transport)
        self._known: dict[int, int] = {}            # słowa z ostatnich odczytów całych bloków

    def _on_frame(self, req, request: bytes, response: bytes | None) -> None:
        if getattr(req, "fc", None) != 3:
            return                                  # tylko odczyty (zapisy nie idą do wektorów)
        key = (int(req.addr), int(req.count))
        self._raw.pop(key, None)
        self._raw[key] = (request, response)
        while len(self._raw) > _RECORD_MAX:
            self._raw.pop(next(iter(self._raw)))

    def recorded(self) -> list[dict]:
        """Nagrane ramki: `{"offset","count","request","response"}` (bajty, NIEZAMASKOWANE)."""
        return [{"offset": a, "count": c, "request": req, "response": resp}
                for (a, c), (req, resp) in sorted(self._raw.items())]

    async def read_block(self, addr: int, count: int, fc: int = 3, *, tries: int | None = None) -> list[int]:
        """Blok `(adres, liczba[, funkcja])` — `read_block(*blok)`; funkcja 3 (holding) albo 4 (input)."""
        return await self.transport.read(addr, count, tries=tries, **read_kwargs(make_block(addr, count, fc)))

    async def read_register(self, addr: int, *, tries: int | None = None) -> int:
        """`tries` — mniej prób transportu niż `read_tries` (None = pełna liczba prób)."""
        if tries is None:
            return (await self.transport.read(addr, 1))[0]
        return (await self.transport.read(addr, 1, tries=tries))[0]

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
        blocks: dict[int | tuple[int, int], list[int]] = {}
        last_err: TransportError | None = None
        plan = read_plan(self.profile, exclude=self.unreadable)
        if self._disambiguate:
            plan = poll_order(self.profile, plan)
        for i, block in enumerate(plan):
            try:
                if self._disambiguate:
                    async with self._session():
                        words = await self._separated(block, lost)
                else:
                    words = await self._timed_read(block, lost)
                blocks[block_key(block)] = self._remember(block, words)
                frames.append(_frame(block, True))
                continue
            except TransportError as err:
                frames.append(_frame(block, False))
                last_err = err
                if isinstance(err, ModbusException) and err.code == 2:
                    if self._disambiguate:
                        last_err = await self._recover_block(block, blocks, frames, lost)
                    else:
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
                blocks[block_key(sub)] = await self._timed_read(sub, lost)
                frames.append(_frame(sub, True))
            except TransportError as err:
                frames.append(_frame(sub, False))
                err_out = err
                if _aborts(err):
                    break
        return err_out

    # ── łącze bez korelacji odpowiedzi (GoodWe UDP, `views.py`) ──

    def _session(self):
        """Wyłączność łącza (`BaseTransport.exclusive`) na sprawdzenie długości poprzedniego odczytu,
        blok rozdzielający i odczyt (oraz na pary: ponowienie bloku, odczyt z podziału i jego
        potwierdzenie) — sekwencja pisarza nie wejdzie między decyzję a żądanie. Sesja trwa jedną
        taką grupę (≤ 3 wymiany), nie cały cykl: pisarz czeka najwyżej na nią, nie na cały cykl
        odpytywania (kilka sekund). Transport bez tej funkcji (atrapy) — bez blokady."""
        exclusive = getattr(self.transport, "exclusive", None)
        return exclusive() if callable(exclusive) else contextlib.nullcontext()

    def _remember(self, block, words: list[int]) -> list[int]:
        """Słowa bloku przeczytanego w całości — punkt odniesienia dla wartości z podziału bloku."""
        if self._disambiguate:
            self._known.update(zip(range(block[0], block[0] + block[1]), words))
        return words

    async def _separated(self, block, lost: list[float]) -> list[int]:
        """Odczyt bloku o długości innej niż poprzednie żądanie odczytu na łączu; gdy równa — najpierw
        blok rozdzielający (znany, innej długości; wyjątek na nim → następny kandydat)."""
        if prev_read_count(self.transport, None) == block[1]:
            for sep in separators_for(self.profile, block):
                try:
                    await self._timed_read(sep, lost)
                    break
                except ModbusException:
                    continue
            else:
                raise TransportError("no separator block answered")
        return await self._timed_read(block, lost)

    async def _recover_block(self, block, blocks, frames, lost) -> TransportError | None:
        """Wyjątek 2 bloku na łączu bez korelacji: wyjątek nie ma długości, więc jedna ramka to nie
        werdykt (np. powtórzony wyjątek ostatniego odczytu poprzedniego cyklu). Blok rozdzielający
        z poprawną odpowiedzią i ponowny odczyt bloku; dopiero drugi wyjątek 2 dzieli blok na zakresy
        kluczy — każdy odczytany z długością inną niż poprzedni, a wartość inna niż z ostatniego
        całego bloku (albo bez takiej — pierwszy cykl) wymaga drugiego, zgodnego odczytu po bloku
        innej długości (inaczej klucze zakresu bez wartości: lepiej brak odczytu niż przesunięta
        wartość). Zwraca ostatni błąd albo None."""
        try:
            async with self._session():
                for sep in separators_for(self.profile, block):
                    try:
                        await self._timed_read(sep, lost)
                        break
                    except ModbusException:
                        continue
                words = await self._timed_read(block, lost)
            blocks[block_key(block)] = self._remember(block, words)
            frames.append(_frame(block, True))
            return None
        except TransportError as err:
            frames.append(_frame(block, False))
            if _aborts(err) or not (isinstance(err, ModbusException) and err.code == 2):
                return err
        err_out: TransportError | None = None
        for sub in split_block(block, self.profile):
            if sub == tuple(block):
                continue
            try:
                span = range(sub[0], sub[0] + sub[1])
                async with self._session():
                    words = await self._separated(sub, lost)
                    # bez wartości odniesienia (pierwszy cykl) albo inna niż ostatnia — drugi odczyt
                    if any(self._known.get(a) != w for a, w in zip(span, words)) \
                            and await self._separated(sub, lost) != words:
                        raise TransportError("split read not confirmed")
                self._known.update(zip(span, words))
                blocks[block_key(sub)] = words
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
        blocks: dict[int | tuple[int, int], list[int]] = {}
        for block in self.profile.modbus.identify_reads:
            try:
                blocks[block_key(block)] = await self.read_block(*block)
            except TransportError:
                return None
        return device_fingerprint(self._salt, self.profile, RegisterImage.from_blocks(blocks))


def _frame(block, ok: bool) -> dict:
    # funkcja tylko dla input — wpisy holding bez zmian (diagnostyka sprzed FC 4)
    return {"addr": block[0], "count": block[1], **read_kwargs(block), "ok": ok}
