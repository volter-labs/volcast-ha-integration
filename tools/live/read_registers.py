"""Odczyt rejestrów holding (Modbus FC 3) na żywym falowniku — WYŁĄCZNIE odczyt.

Użycie: .venv/bin/python -m tools.live.read_registers --host <ip> --addr 47000 --count 1

Wypisuje słowa (dziesiętnie i hex) oraz dokładną ramkę żądania i odpowiedzi w formacie wpisu
`tests/golden/goodwe_et/frames.json` (`offset`, `count`, `request`, `response`, `valid`).
Narzędzie nie ma ścieżki zapisu: każdy kod funkcji inny niż 3 odrzuca `ensure_read_only`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from custom_components.volcast.core.transports.base import LinkDown, RequestTimeout, TransportConfig, TransportError
from custom_components.volcast.core.transports.goodwe_udp import GoodweUdpTransport

READ_HOLDING = 3


class WriteRefused(Exception):
    """Próba użycia kodu funkcji innego niż 3 (odczyt holding)."""


def ensure_read_only(fc: int) -> None:
    if fc != READ_HOLDING:
        raise WriteRefused(f"function code {fc} refused: this tool only reads (FC 3)")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Read-only Modbus FC 3 register reader (GoodWe UDP).")
    p.add_argument("--host", required=True)
    p.add_argument("--addr", type=int, required=True)
    p.add_argument("--count", type=int, required=True)
    p.add_argument("--port", type=int, default=8899)
    p.add_argument("--unit-id", type=int, default=247)
    p.add_argument("--transport", choices=["goodwe_udp"], default="goodwe_udp")
    p.add_argument("--timeout", type=float, default=2.0, help="seconds to wait for a reply")
    p.add_argument("--tries", type=int, default=3)
    return p


def _fail(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 1


async def read_registers(host: str, addr: int, count: int, *, port: int = 8899, unit: int = 247,
                         timeout_s: float = 2.0, tries: int = 3, fc: int = READ_HOLDING) -> tuple[list[int], dict]:
    """Zwraca (słowa, wpis w formacie frames.json). Rzuca `TransportError` przy braku odpowiedzi."""
    ensure_read_only(fc)
    cfg = TransportConfig(kind="goodwe_udp", host=host, port=port, unit=unit, timeout_s=timeout_s,
                          read_tries=tries)
    transport = GoodweUdpTransport(cfg, host)
    frames: list[tuple[bytes, bytes | None]] = []
    transport.recorder = lambda _req, request, response: frames.append((request, response))
    try:
        words = await transport.read(addr, count, fc=fc)
    finally:
        await transport.close()
    request, response = frames[-1] if frames else (b"", None)
    entry = {"offset": addr, "count": count, "request": request.hex(),
             "response": (response or b"").hex(), "valid": response is not None}
    return words, entry


async def amain(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 0 <= args.addr <= 0xFFFF or not 1 <= args.count <= 125 or args.addr + args.count > 0x10000:
        return _fail("addr must be 0..65535 and count 1..125")
    try:
        words, entry = await read_registers(args.host, args.addr, args.count, port=args.port, unit=args.unit_id,
                                            timeout_s=args.timeout, tries=args.tries)
    except WriteRefused as err:
        return _fail(str(err))
    except RequestTimeout:
        return _fail(f"no response from {args.host}:{args.port} (timeout {args.timeout}s, {args.tries} tries)")
    except LinkDown as err:
        return _fail(f"link down: {err}")
    except TransportError as err:
        return _fail(f"read failed: {err}")
    for i, w in enumerate(words):
        print(f"{args.addr + i}: {w} (0x{w:04x})")
    print(json.dumps(entry, indent=1))
    return 0


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
