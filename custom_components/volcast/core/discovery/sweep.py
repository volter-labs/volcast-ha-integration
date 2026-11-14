"""Kandydaci z lokalnych podsieci: unicast UDP 48899 albo TCP connect na porcie celu.

`local_subnets` to czysta funkcja (reguły RFC 1918, cięcie do /24). `sweep` robi I/O wyłącznie przez
wstrzyknięte `connect` / `udp_probe`, więc testy nie dotykają sieci. Adresów nie logujemy.

UWAGA: unicast 48899 nie jest zweryfikowany na prawdziwych loggerach — dlatego `udp_probe` jest wstrzykiwany.
"""
from __future__ import annotations

import asyncio
import ipaddress
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

from .identify import Candidate

_PRIVATE = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
_UDP_TRANSPORTS = frozenset({"goodwe_udp", "solarman_v5"})
_TCP_TRANSPORTS = frozenset({"modbus_tcp", "rtu_tcp"})
_MAX_PREFIX_CUT = 24
_PROBE_GRACE_S = 0.5            # zapas ponad `per_try_s` na utworzenie gniazda i zebranie odpowiedzi


def _is_private(ip: ipaddress.IPv4Address) -> bool:
    return any(ip in n for n in _PRIVATE)


def _net24(ip: ipaddress.IPv4Address) -> ipaddress.IPv4Network:
    return ipaddress.IPv4Network((int(ip) & 0xFFFFFF00, 24))


def local_subnets(adapters: Sequence[Mapping[str, Any]], old_host: str) -> list[ipaddress.IPv4Network]:
    """Podsieci do przeszukania: prywatne IPv4 z włączonych adapterów + /24 starego hosta.

    Sieć większa niż /24 jest cięta do /24 z adresem HA. Adresy publiczne, link-local, loopback i CGNAT
    są pomijane.
    """
    found: set[ipaddress.IPv4Network] = set()
    for adapter in adapters:
        if not adapter.get("enabled"):
            continue
        for entry in adapter.get("ipv4") or ():
            try:
                iface = ipaddress.IPv4Interface(f"{entry['address']}/{int(entry['network_prefix'])}")
            except (KeyError, TypeError, ValueError):
                continue
            if not _is_private(iface.ip):
                continue
            found.add(_net24(iface.ip) if iface.network.prefixlen < _MAX_PREFIX_CUT else iface.network)
    try:
        old = ipaddress.IPv4Address(old_host)
    except ValueError:
        old = None
    if old is not None and _is_private(old):
        found.add(_net24(old))
    return sorted(found, key=lambda n: (int(n.network_address), n.prefixlen))


async def sweep(
    subnets: Iterable[ipaddress.IPv4Network],
    *,
    transport: str,
    port: int,
    connect: Callable[[str, int, float], Awaitable[bool]] | None,
    udp_probe: Callable[..., Awaitable[Any]] | None,
    concurrency: int = 32,
    per_try_s: float = 0.5,
    budget_s: float = 60,
    skip: Iterable[str] = (),
) -> list[Candidate]:
    """Sprawdź adresy z podsieci i zwróć kandydatów (`source="sweep"`), bez duplikatów.

    `goodwe_udp`/`solarman_v5` → `udp_probe(target=ip, timeout=…)` (odpowiedź z listą `replies` = kandydat);
    `modbus_tcp`/`rtu_tcp` → `await connect(ip, port, per_try_s)` zwracające bool. Po `budget_s` niedokończone
    próby są anulowane, a zwracane są dotychczasowe trafienia. `skip` — adresy własne HA itp.
    """
    if transport in _UDP_TRANSPORTS:
        if udp_probe is None:
            return []
    elif transport in _TCP_TRANSPORTS:
        if connect is None:
            return []
    else:
        return []

    skipped = {str(s) for s in skip}
    targets: list[str] = []
    seen: set[str] = set()
    for net in subnets:
        for ip in net.hosts():
            text = str(ip)
            if text in seen or text in skipped or not _is_private(ip):
                continue
            seen.add(text)
            targets.append(text)

    sem = asyncio.Semaphore(max(1, concurrency))
    hits: dict[str, int | None] = {}   # adres -> numer loggera (tylko transport V5)

    async def probe(host: str) -> None:
        if transport in _UDP_TRANSPORTS:
            res = await udp_probe(target=host, timeout=per_try_s)  # type: ignore[misc]
            replies = getattr(res, "replies", None) if res is not None else None
            # odpowiedź liczy się tylko od sondowanego hosta (`ip` z treści odpowiedzi loggera)
            own = [r for r in (replies or ()) if getattr(r, "ip", None) == host]
            if own:
                hits[host] = next((r.logger_serial for r in own if getattr(r, "logger_serial", None) is not None), None)
        elif await connect(host, port, per_try_s):  # type: ignore[misc]
            hits[host] = None

    async def one(host: str) -> None:
        async with sem:
            try:
                # sonda UDP nasłuchuje dokładnie `per_try_s`, więc zewnętrzny limit musi mieć zapas
                await asyncio.wait_for(probe(host), per_try_s + _PROBE_GRACE_S)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — pojedyncza próba (też timeout) nie psuje skanu; bez adresu w logu
                return

    tasks = [asyncio.ensure_future(one(h)) for h in targets]
    try:
        if tasks:
            await asyncio.wait(tasks, timeout=budget_s)
    finally:
        # także przy anulowaniu z zewnątrz: żadna próba nie działa po powrocie
        for t in tasks:
            if not t.done():
                t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    return [
        Candidate(host=h, source="sweep", logger_serial=hits[h] if transport == "solarman_v5" else None,
                  transports=(transport,))
        for h in sorted(hits, key=lambda h: int(ipaddress.IPv4Address(h)))
    ]
