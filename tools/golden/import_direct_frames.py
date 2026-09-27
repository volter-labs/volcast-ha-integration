"""Ramki z diagnostyki połączenia próbnego → złote wektory w formacie `tests/golden/goodwe_et/frames.json`.

Użycie: python tools/golden/import_direct_frames.py <diagnostics.json> --profile deye-sg --out tests/golden/deye_sg/frames.json

Diagnostyka maskuje ramki już w integracji (rejestry seryjne wyzerowane, numer loggera wyzerowany,
sumy przeliczone). Ten skrypt jest DRUGĄ strażą przed publikacją (repo publiczne): odmawia zapisu,
gdy w zdekodowanych bajtach ramek albo w tekście jest coś, co wygląda na numer seryjny
(ciąg w stylu seriala GoodWe, 10-cyfrowy numer loggera), UUID albo niezerowy numer loggera V5.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SERIAL_RE = re.compile(rb"\d{5}[A-Z]{3}\d{3}[A-Z0-9]{5}")
LOGGER_RE = re.compile(rb"(?<!\d)\d{10}(?!\d)")
TEXT_LOGGER_RE = re.compile(r"(?<![0-9a-fA-F])\d{10}(?![0-9a-fA-F])")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-", re.IGNORECASE)
_HEX_RE = re.compile(r"[0-9a-fA-F]*")


def _frames(doc: dict) -> list[dict]:
    frames = ((doc.get("control") or {}).get("direct") or {}).get("frames")
    if not isinstance(frames, list) or not frames:
        raise SystemExit("brak ramek w diagnostyce (tylko połączenie próbne je nagrywa)")
    return frames


def _check_bytes(raw: bytes, where: str) -> None:
    if SERIAL_RE.search(raw) or LOGGER_RE.search(raw):
        raise SystemExit(f"coś jak numer seryjny w ramce {where}")
    if raw[:1] == b"\xa5" and len(raw) >= 11 and raw[7:11] != b"\x00\x00\x00\x00":
        raise SystemExit(f"niezerowy numer loggera w ramce {where}")


def _check_text(value, where: str) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            _check_text(v, f"{where}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _check_text(v, f"{where}[{i}]")
    elif isinstance(value, str) and not _HEX_RE.fullmatch(value):
        if UUID_RE.search(value) or TEXT_LOGGER_RE.search(value) or SERIAL_RE.search(value.encode()):
            raise SystemExit(f"coś jak identyfikator w tekście {where}")


def convert(doc: dict) -> dict:
    out: dict[str, dict] = {}
    for i, f in enumerate(_frames(doc)):
        _check_text(f, f"frames[{i}]")
        offset, count = int(f["offset"]), int(f["count"])
        request = f.get("request") or ""
        response = f.get("response") or ""
        for field, value in (("request", request), ("response", response)):
            if not _HEX_RE.fullmatch(value):
                raise SystemExit(f"frames[{i}].{field} nie jest zapisem szesnastkowym")
            _check_bytes(bytes.fromhex(value), f"frames[{i}].{field}")
        out[f"block_{offset}_{count}"] = {"offset": offset, "count": count, "request": request,
                                          "response": response, "valid": bool(request and response)}
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("diagnostics", type=Path)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    doc = json.loads(args.diagnostics.read_text(encoding="utf-8"))
    profile = ((doc.get("control") or {}).get("direct") or {}).get("profile")
    if profile is not None and profile != args.profile:
        raise SystemExit(f"diagnostyka dotyczy profilu {profile}, nie {args.profile}")
    frames = convert(doc)
    text = json.dumps(frames, indent=1) + "\n"
    _check_text(json.loads(text), "out")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1:])
