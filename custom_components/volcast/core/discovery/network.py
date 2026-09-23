"""Sonda rozgłoszeniowa dongli Wi-Fi (GoodWe, Solarman/LSW) na UDP 48899. Tylko odczyt."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

PROBE_MESSAGE = b"WIFIKIT-214028-READ"


@dataclass
class LoggerReply:
    raw: str
    ip: str | None
    mac: str | None
    name: str | None


@dataclass
class NetworkProbeResult:
    sent: bool
    replies: list[LoggerReply] = field(default_factory=list)
    error: str | None = None


def parse_reply(raw: bytes) -> LoggerReply:
    text = raw.decode("ascii", errors="replace").strip()
    parts = [p.strip() for p in text.split(",")]
    if len(parts) >= 2 and parts[0].count(".") == 3:
        return LoggerReply(
            raw=text,
            ip=parts[0],
            mac=parts[1] or None,
            name=parts[2] if len(parts) > 2 and parts[2] else None,
        )
    return LoggerReply(raw=text, ip=None, mac=None, name=None)


class _Collector(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.raw: list[bytes] = []

    def datagram_received(self, data: bytes, addr) -> None:
        if data != PROBE_MESSAGE and data not in self.raw:
            self.raw.append(data)


async def probe_udp_48899(
    target: str = "255.255.255.255", port: int = 48899, timeout: float = 2.0
) -> NetworkProbeResult:
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
        return NetworkProbeResult(sent=True, replies=[parse_reply(r) for r in proto.raw])
    except Exception as err:  # noqa: BLE001
        return NetworkProbeResult(sent=False, error=f"{type(err).__name__}: {err}")
    finally:
        transport.close()
