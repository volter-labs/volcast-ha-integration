"""Wspólna część transportów rejestrów: błędy, konfiguracja, liczniki, kontrola adresu
i silnik wymiany żądanie–odpowiedź (jeden zamek, odstęp, dopasowanie, reset kanału).

Zasady (wszystkie transporty):
* jeden zamek — żądanie i jego odpowiedź są atomowe; `close()` zamka nie bierze;
* odstęp `gap_s` od końca poprzedniej wymiany (zegar monotoniczny, wstrzykiwany);
* przed wysłaniem opróżnienie bufora (UDP: także datagramy czekające w gnieździe, jeszcze
  nieodebrane przez pętlę): każda wyrzucona ramka to `stray` (ramki protokołu loggera V5 — `unsolicited`);
* odpowiedź przyjmowana wyłącznie przy pełnej zgodności (nadawca, TID/sekwencja, jednostka,
  funkcja, długość, echo); inna ramka to `stray`, a czekanie trwa do końca czasu;
* po KAŻDYM przekroczeniu czasu i po przerwanej (anulowanej) wymianie kanał jest resetowany
  przed następnym żądaniem — spóźniona odpowiedź tej samej długości nie może zostać wzięta
  za odpowiedź na kolejne żądanie (FC 3 nie niesie adresu);
* odczyt: do `read_tries` prób, każda na świeżym kanale; zapis: na UDP najwyżej dwie wysyłki
  (druga tylko przy braku jakiejkolwiek odpowiedzi; każda przez `on_send` — budżet NVM liczy obie,
  `stats.write_resends`), na TCP jedna; wyjątek Modbus bez ponawiania.

W komunikatach i logach wyłącznie rodzaj transportu i nazwy klas błędów — nigdy adres hosta.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import math
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol, Sequence

from .modbus_frames import (
    FC_READ, FC_WRITE_MULTIPLE, FC_WRITE_SINGLE, FrameError, parse_rtu_read, parse_rtu_write,
    pdu_read, pdu_write_multiple, pdu_write_single)

_LOGGER = logging.getLogger(__name__)

TRANSPORT_KINDS = ("goodwe_udp", "modbus_tcp", "modbus_rtu", "solarman_v5")
WRITE_FUNCTIONS = (FC_WRITE_SINGLE, FC_WRITE_MULTIPLE)


# ── błędy ─────────────────────────────────────────────────────────────────


class TransportError(Exception):
    """Błąd transportu. Komunikat nigdy nie zawiera adresu hosta."""


class LinkDown(TransportError):
    """Brak połączenia: odrzucone, zerwane, w odwrocie albo transport zamknięty."""


class RequestTimeout(TransportError):
    """Brak pasującej odpowiedzi w czasie. `silent` — nie przyszła żadna ramka."""

    def __init__(self, message: str = "no matching reply in time", *, silent: bool = False) -> None:
        super().__init__(message)
        self.silent = silent


class ModbusException(TransportError):
    def __init__(self, code: int) -> None:
        super().__init__(f"modbus exception {code}")
        self.code = code


class InverterAsleep(TransportError):
    """Logger odpowiedział bez ramki falownika (falownik uśpiony) — nie obca ramka, nie kolizja."""


# ── konfiguracja i liczniki ───────────────────────────────────────────────


@dataclass
class TransportStats:
    requests: int = 0               # wysłane ramki żądań (także ponowienia)
    timeouts: int = 0               # wymiany bez pasującej odpowiedzi
    stray: int = 0                  # ramki niepasujące (obce, spóźnione, zniekształcone)
    peer_resets: int = 0            # połączenie zerwane przez drugą stronę
    reconnects: int = 0             # udane połączenia po pierwszym
    unsolicited: int = 0            # ramki protokołu naszego loggera (nie sygnał innego klienta)
    consecutive_timeouts: int = 0
    channel_resets: int = 0         # nasze zamknięcie kanału (nowe gniazdo UDP / nowe połączenie TCP)
    write_resends: int = 0          # druga wysyłka zapisu po całkowitej ciszy (UDP) — liczona też w budżecie NVM
    last_ok_mono: float | None = None


@dataclass(frozen=True)
class TransportConfig:
    kind: str
    host: str
    port: int
    unit: int
    timeout_s: float = 2.0
    gap_s: float = 0.05
    read_tries: int = 3
    logger_serial: int | None = None
    connect_timeout_s: float = 5.0
    backoff_min_s: float = 1.0
    backoff_max_s: float = 60.0


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _pos(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0


def validate_config(cfg: TransportConfig) -> None:
    """ValueError przy konfiguracji spoza zakresów (bez adresu w komunikacie)."""
    if cfg.kind not in TRANSPORT_KINDS:
        raise ValueError("unknown transport kind")
    if not _int(cfg.port) or not 1 <= cfg.port <= 65535:
        raise ValueError("port out of range")
    if not _int(cfg.unit) or not 0 <= cfg.unit <= 255:
        raise ValueError("unit id out of range")
    if not _pos(cfg.timeout_s) or not _pos(cfg.connect_timeout_s):
        raise ValueError("timeout must be positive")
    if not isinstance(cfg.gap_s, (int, float)) or isinstance(cfg.gap_s, bool) \
            or not math.isfinite(cfg.gap_s) or cfg.gap_s < 0:
        raise ValueError("gap must be >= 0")
    if not _int(cfg.read_tries) or cfg.read_tries < 1:
        raise ValueError("read_tries must be >= 1")
    if not _pos(cfg.backoff_min_s) or not _pos(cfg.backoff_max_s) or cfg.backoff_max_s < cfg.backoff_min_s:
        raise ValueError("bad backoff range")
    if cfg.kind == "solarman_v5":
        if not _int(cfg.logger_serial) or not 0 < cfg.logger_serial <= 0xFFFFFFFF:
            raise ValueError("solarman_v5 requires a logger serial")


# ── adres celu ────────────────────────────────────────────────────────────

_REJECTED_NETS = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "255.255.255.255/32",
    "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32",   # dokumentacyjne
))


def check_target(host: str, *, allow_loopback: bool = False) -> str:
    """Adres celu: literał IPv4/IPv6 z sieci prywatnej albo link-local; zwraca postać kanoniczną.

    Kolejność: (1) literał (nazwa hosta → odmowa) i odpakowanie `::ffff:a.b.c.d`;
    (2) odrzucenie adresów nieokreślonych, multicast, zarezerwowanych, rozgłoszenia,
    dokumentacyjnych i pętli zwrotnej (chyba że `allow_loopback` — tylko testy/symulator);
    (3) akceptacja wyłącznie `is_private` albo `is_link_local` (IPv6 link-local tylko ze strefą).
    ValueError nigdy nie zawiera adresu.
    """
    if not isinstance(host, str) or not host or host != host.strip():
        raise ValueError("target must be an IP address literal")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError("target must be an IP address literal") from None
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    if ip.is_loopback:
        if allow_loopback:
            return str(ip)
        raise ValueError("loopback target not allowed")
    if ip.is_unspecified or ip.is_multicast or ip.is_reserved \
            or any(ip.version == n.version and ip in n for n in _REJECTED_NETS):
        raise ValueError("special-purpose address not allowed")
    if ip.is_link_local:
        if ip.version == 6 and not getattr(ip, "scope_id", None):
            raise ValueError("IPv6 link-local target needs a zone")
        return str(ip)
    if ip.is_private:
        return str(ip)
    raise ValueError("only local network addresses are allowed")


# ── protokół ──────────────────────────────────────────────────────────────


OnSend = Callable[[], None]


class RegisterTransport(Protocol):
    kind: str
    stats: TransportStats

    async def read(self, addr: int, count: int, *, tries: int | None = None) -> list[int]: ...

    async def write(self, addr: int, values: Sequence[int], *, function: int,
                    on_send: OnSend | None = None, may_resend: Callable[[], bool] | None = None) -> None: ...

    async def reset_channel(self) -> None: ...

    async def close(self) -> None: ...


# ── żądanie i dopasowanie odpowiedzi ──────────────────────────────────────


@dataclass(frozen=True)
class Request:
    fc: int
    addr: int
    count: int
    pdu: bytes
    echo: int = 0                   # FC 6: wartość, FC 16: liczba rejestrów


def read_req(addr: int, count: int) -> Request:
    return Request(FC_READ, addr, count, pdu_read(addr, count))


def write_req(function: int, addr: int, values: Sequence[int]) -> Request:
    values = list(values)
    if function == FC_WRITE_SINGLE:
        if len(values) != 1:
            raise ValueError("function 6 writes exactly one register")
        return Request(function, addr, 1, pdu_write_single(addr, values[0]), echo=values[0])
    if function == FC_WRITE_MULTIPLE:
        return Request(function, addr, len(values), pdu_write_multiple(addr, values), echo=len(values))
    raise ValueError("write function must be 6 or 16")


class Stray(Exception):
    """Ramka, która nie jest odpowiedzią na bieżące żądanie."""


class Unsolicited(Exception):
    """Ramka protokołu naszego loggera; `ack` — potwierdzenie do odesłania (albo None)."""

    def __init__(self, ack: bytes | None = None) -> None:
        super().__init__("logger protocol frame")
        self.ack = ack


class Desync(Exception):
    """Strumienia nie da się dalej ciąć na ramki — połączenie do zamknięcia."""


def match_rtu(frame: bytes, unit: int, req: Request) -> list[int] | None:
    """Goła ramka RTU → słowa (odczyt) / None (zapis); `ModbusException` albo `Stray`."""
    if len(frame) >= 2 and frame[0] == unit and frame[1] & 0x80 and frame[1] != req.fc | 0x80:
        raise Stray("exception for another function")
    try:
        if req.fc == FC_READ:
            return parse_rtu_read(frame, unit, req.count)
        parse_rtu_write(frame, unit, req.fc, req.addr, req.echo)
        return None
    except FrameError as err:
        if err.kind == "exception" and err.code is not None:
            raise ModbusException(err.code) from None
        raise Stray(err.kind) from None


# ── silnik wymiany ────────────────────────────────────────────────────────


Sleep = Callable[[float], Awaitable[None]]


class BaseTransport:
    """Wspólny silnik; podklasy dostarczają kanał (gniazdo) i ramkowanie."""

    kind = ""
    resend_writes = False            # UDP: druga wysyłka zapisu przy całkowitej ciszy

    def __init__(self, cfg: TransportConfig, host: str, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Sleep = asyncio.sleep) -> None:
        self.cfg = cfg
        self._host = host
        self._clock = clock
        self._sleep = sleep
        self.stats = TransportStats()
        # Liczba rejestrów ostatniego żądania odczytu (= długość jego odpowiedzi). Odpowiedź RTU nie
        # niesie adresu: wołający, który chce odróżnić świeżą odpowiedź od nieaktualnej, wybiera
        # odczyt innej długości (`modbus/views.py`).
        self.last_read_count: int | None = None
        self._lock = asyncio.Lock()
        # sesja na wyłączność (`exclusive`): zadanie-właściciel i zamek sesji
        self._session = asyncio.Lock()
        self._session_owner: asyncio.Task | None = None
        self._last_end: float | None = None
        self._dirty = False          # przerwana wymiana — reset kanału przed następnym żądaniem
        self._closed = False
        # nagrywanie surowych ramek (tryb próbny, diagnostyka): (żądanie, ramka, odpowiedź albo None)
        self.recorder: Callable[[Request, bytes, bytes | None], None] | None = None
        self._matched: bytes | None = None

    # ── API ──

    @contextlib.asynccontextmanager
    async def exclusive(self):
        """Łącze na wyłączność bieżącego zadania: żądania innych zadań (np. odpytywania) czekają do
        końca sesji, żądania właściciela przechodzą (sesja jest wielowejściowa w obrębie zadania).
        Każde pojedyncze żądanie i tak bierze sesję na swój czas — sesja pisarza nie przeplata się
        z żądaniem odpytywania. Zamek FIFO: czekające żądanie wchodzi zaraz po końcu sesji."""
        task = asyncio.current_task()
        if task is not None and self._session_owner is task:
            yield
            return
        async with self._session:
            self._session_owner = task
            try:
                yield
            finally:
                self._session_owner = None

    async def read(self, addr: int, count: int, *, tries: int | None = None) -> list[int]:
        """`tries` — mniej prób niż `read_tries` (limit czasu cyklu u wołającego); nigdy więcej."""
        req = read_req(addr, count)
        n = self.cfg.read_tries if tries is None else max(1, min(int(tries), self.cfg.read_tries))
        async with self.exclusive(), self._lock:
            self.last_read_count = count
            for attempt in range(n):
                try:
                    return await self._transact(req)
                except RequestTimeout:
                    if attempt == n - 1:
                        raise
        raise AssertionError("unreachable")

    async def write(self, addr: int, values: Sequence[int], *, function: int,
                    on_send: OnSend | None = None, may_resend: Callable[[], bool] | None = None) -> None:
        """`may_resend` — pytane PRZED ponowną wysyłką (np. budżet NVM); False = bez niej, wychodzi
        przekroczenie czasu pierwszej wysyłki (wynik rozstrzyga odczyt zwrotny wołającego)."""
        req = write_req(function, addr, values)
        sends = 2 if self.resend_writes else 1
        async with self.exclusive(), self._lock:
            for i in range(sends):
                try:
                    await self._transact(req, on_send)
                    return
                except RequestTimeout as err:
                    # Ponowna wysyłka tylko przy całkowitej ciszy: obca/zła ramka mogła być
                    # odpowiedzią urządzenia, której nie rozumiemy — wtedy rozstrzyga odczyt zwrotny.
                    if not err.silent or i == sends - 1 or (may_resend is not None and not may_resend()):
                        raise
                # Ta sama wartość bezwzględna drugi raz (jak Box). Każda wysyłka woła `on_send`,
                # więc budżet NVM liczy obie; tu licznik i log (bez adresu hosta).
                self.stats.write_resends += 1
                _LOGGER.debug("%s: write resent after silence", self.kind)

    async def reset_channel(self) -> None:
        async with self._lock:
            await self._reset()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._close_channel()

    # ── wymiana ──

    async def _transact(self, req: Request, on_send: OnSend | None = None):
        if self._closed:
            raise LinkDown("transport closed")
        if self._dirty:
            await self._reset()
        await self._wait_gap()
        await self._open()
        await self._collect_pending()
        if self._drain():
            # Strumień rozsynchronizowany jeszcze przed wysłaniem — świeże połączenie.
            self.stats.stray += 1
            await self._reset()
            await self._open()
        frame, ctx = self._encode(req)
        self._dirty = True
        self._matched = None
        try:
            self._send(frame)
            self.stats.requests += 1
            if on_send is not None:
                on_send()
            result = await self._await_answer(req, ctx)
        except RequestTimeout:
            self._matched = None             # ramki obce/złe nie są odpowiedzią
            self.stats.timeouts += 1
            self.stats.consecutive_timeouts += 1
            _LOGGER.debug("%s: %s", self.kind, RequestTimeout.__name__)
            await self._reset()
            raise
        except (ModbusException, InverterAsleep):
            self._answered()
            raise
        finally:
            self._last_end = self._clock()
            self._record(req, frame)
        self._answered()
        return result

    def _record(self, req: Request, frame: bytes) -> None:
        recorder = self.recorder
        if recorder is None:
            return
        try:
            recorder(req, bytes(frame), None if self._matched is None else bytes(self._matched))
        except Exception as err:  # noqa: BLE001 — nagrywanie nie psuje wymiany
            _LOGGER.debug("%s: frame recorder failed: %s", self.kind, type(err).__name__)

    def _answered(self) -> None:
        """Wymiana zakończona czystą odpowiedzią (także wyjątkiem Modbus)."""
        self._dirty = False
        self.stats.consecutive_timeouts = 0
        self.stats.last_ok_mono = self._clock()
        self._on_answer()

    async def _await_answer(self, req: Request, ctx):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.cfg.timeout_s
        got_any = False
        while True:
            frame = await self._next_frame(deadline)
            if frame is None:
                raise RequestTimeout(silent=not got_any)
            got_any = True
            self._matched = frame
            try:
                return self._match(frame, req, ctx)
            except Stray:
                self.stats.stray += 1
            except Unsolicited as u:
                self.stats.unsolicited += 1
                if u.ack is not None:
                    self._send(u.ack)

    async def _wait_gap(self) -> None:
        if self._last_end is None or self.cfg.gap_s <= 0:
            return
        wait = self._last_end + self.cfg.gap_s - self._clock()
        if wait > 0:
            await self._sleep(wait)

    async def _reset(self) -> None:
        self._dirty = False
        if await self._close_channel():
            self.stats.channel_resets += 1

    # ── dla podklas ──

    async def _open(self) -> None:
        raise NotImplementedError

    async def _close_channel(self) -> bool:
        """Zamyka kanał; True, gdy był otwarty."""
        raise NotImplementedError

    async def _collect_pending(self) -> None:
        """Hak przed `_drain`: odbiór ramek, które już czekają w gnieździe (UDP)."""

    def _drain(self) -> bool:
        """Wyrzuca zaległe ramki (liczniki); True = strumień rozsynchronizowany."""
        raise NotImplementedError

    def _encode(self, req: Request) -> tuple[bytes, object]:
        raise NotImplementedError

    def _send(self, data: bytes) -> None:
        raise NotImplementedError

    async def _next_frame(self, deadline: float) -> bytes | None:
        raise NotImplementedError

    def _match(self, frame: bytes, req: Request, ctx):
        raise NotImplementedError

    def _on_answer(self) -> None:
        """Hak: udana wymiana (TCP zeruje odwrót)."""
