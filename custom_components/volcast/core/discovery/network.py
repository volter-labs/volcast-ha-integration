"""Sonda rozgłoszeniowa dongli Wi-Fi (GoodWe, Solarman/LSW) na UDP 48899. Tylko odczyt.

`target` musi być literałem IPv4 (np. adresem rozgłoszeniowym lub konkretnym IP) —
przy nazwie hosta `sendto` wykonałby synchroniczne DNS w pętli zdarzeń HA.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

PROBE_MESSAGE = b"WIFIKIT-214028-READ"

# Limity ochronne przed hałaśliwym/wrogim urządzeniem w sieci LAN.
MAX_REPLIES = 32
MAX_DATAGRAM_BYTES = 512
# Trzecie pole odpowiedzi loggera Solarman (LSW) to jego numer: dokładnie 10 cyfr, mieści się w u32.
_LOGGER_SERIAL_RE = re.compile(r"[0-9]{10}")


@dataclass
class LoggerReply:
    raw: str
    ip: str | None
    mac: str | None
    name: str | None
    # numer loggera Solarman (potrzebny do ramek V5); nigdy do logów ani nieredagowanej diagnostyki
    logger_serial: int | None = None


@dataclass
class NetworkProbeResult:
    sent: bool
    replies: list[LoggerReply] = field(default_factory=list)
    error: str | None = None


def parse_reply(raw: bytes) -> LoggerReply:
    text = raw.decode("ascii", errors="replace").strip()
    text = "".join(c for c in text if c.isprintable())[:128]
    parts = [p.strip() for p in text.split(",")]
    if len(parts) >= 2 and parts[0].count(".") == 3:
        name = parts[2] if len(parts) > 2 and parts[2] else None
        return LoggerReply(
            raw=text,
            ip=parts[0],
            mac=parts[1] or None,
            name=name,
            logger_serial=_logger_serial(name),
        )
    return LoggerReply(raw=text, ip=None, mac=None, name=None)


def _logger_serial(field: str | None) -> int | None:
    if field is None or not _LOGGER_SERIAL_RE.fullmatch(field):
        return None
    value = int(field)
    return value if 0 < value <= 0xFFFFFFFF else None


class _Collector(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.raw: list[bytes] = []
        self._seen: set[bytes] = set()
        self.error: Exception | None = None

    def datagram_received(self, data: bytes, addr) -> None:
        if data == PROBE_MESSAGE:
            return  # echo własnej sondy — ignorujemy
        if len(data) > MAX_DATAGRAM_BYTES:
            return
        if len(self.raw) >= MAX_REPLIES:
            return
        if data in self._seen:
            return
        self._seen.add(data)
        self.raw.append(data)

    def error_received(self, exc: Exception) -> None:
        # Tu trafiają asynchroniczne błędy wysyłki (np. ENETUNREACH/EHOSTUNREACH/
        # EACCES/ENOBUFS) — CPython łapie OSError w sendto i przekazuje go tutaj
        # zamiast go rzucać, więc bez tego callbacku błąd wysyłki byłby niewidoczny.
        if self.error is None:
            self.error = exc


async def probe_udp_48899(
    target: str = "255.255.255.255", port: int = 48899, timeout: float = 2.0
) -> NetworkProbeResult:
    """Wyślij pojedynczą sondę na UDP 48899 i zbierz odpowiedzi.

    Nigdy nie rzuca wyjątku — awarie trafiają do `NetworkProbeResult.error`.
    `sent=True` oznacza wyłącznie, że wywołanie `sendto` zostało wykonane (transport
    został utworzony i `sendto` nie rzuciło synchronicznie); nie gwarantuje dostarczenia
    pakietu. Jeśli w trakcie oczekiwania na odpowiedzi transport zgłosił błąd wysyłki
    (przez `error_received`), trafia on do `error`, a już zebrane odpowiedzi są mimo to
    zwracane w `replies`.
    """
    loop = asyncio.get_running_loop()
    try:
        transport, proto = await loop.create_datagram_endpoint(
            _Collector, local_addr=("0.0.0.0", 0), allow_broadcast=True
        )
    except Exception as err:  # noqa: BLE001 — sonda nigdy nie rzuca
        return NetworkProbeResult(sent=False, error=f"{type(err).__name__}: {err}")
    try:
        transport.sendto(PROBE_MESSAGE, (target, port))
        await asyncio.sleep(timeout)
        error = f"{type(proto.error).__name__}: {proto.error}" if proto.error else None
        return NetworkProbeResult(
            sent=True,
            replies=[parse_reply(r) for r in proto.raw],
            error=error,
        )
    except Exception as err:  # noqa: BLE001
        return NetworkProbeResult(sent=False, error=f"{type(err).__name__}: {err}")
    finally:
        transport.close()
