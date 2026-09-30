"""Kopiuje złote wektory implementacji referencyjnej do tests/golden/goodwe_et/.

Anonimizacja (repo publiczne): numer seryjny w bloku device_info zastąpiony
stałym tekstem z przeliczonym CRC; identyfikator planu zastąpiony stałą.
Straż sprawdza zdekodowane bajty ramek (nie tekst hex) i odmawia zapisu,
gdy cokolwiek wygląda na numer seryjny albo UUID.
Użycie: python tools/golden/import_box_fixtures.py <box_repo> <katalog_z_eksportem> <sha>
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[2] / "tests" / "golden" / "goodwe_et"
FAKE_SERIAL = b"GOLDENSERIAL0000"
SERIAL_RE = re.compile(rb"\d{5}ETU\d{3}W\d{4}")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-")


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def mask_device_info(resp_hex: str) -> str:
    raw = bytearray(bytes.fromhex(resp_hex))
    data_start = 5                      # aa55 + adres + funkcja + długość
    serial_at = data_start + 3 * 2      # 35003..35010 = 8 rejestrów = 16 znaków
    raw[serial_at:serial_at + 16] = FAKE_SERIAL
    crc = crc16(bytes(raw[2:-2]))
    raw[-2], raw[-1] = crc & 0xFF, crc >> 8
    return raw.hex()


def check_no_leaks(frames: dict, texts: dict[str, str]) -> None:
    """Straż przed publikacją: bajty ramek i tekst JSON bez serialu i UUID."""
    for name, block in frames.items():
        for field in ("request", "response"):
            raw = bytes.fromhex(block[field])
            if SERIAL_RE.search(raw):
                raise SystemExit(f"serial w ramce {name}.{field}")
    if FAKE_SERIAL not in bytes.fromhex(frames["device_info"]["response"]):
        raise SystemExit("device_info bez zastępczego serialu — zmienił się offset?")
    for fname, text in texts.items():
        if SERIAL_RE.search(text.encode()):
            raise SystemExit(f"serial w {fname}")
        if UUID_RE.search(text):
            raise SystemExit(f"UUID w {fname}")


def main(box: Path, exported: Path, sha: str) -> None:
    fixtures = box / "test" / "host" / "fixtures"
    frames = json.loads((fixtures / "goodwe_et_frames.json").read_text())
    frames["device_info"]["response"] = mask_device_info(frames["device_info"]["response"])

    plan = json.loads((fixtures / "plan_zywy_20260901.json").read_text())
    plan["schedule_id"] = "golden-plan"

    texts = {
        "frames.json": json.dumps(frames, indent=1) + "\n",
        "plan_live.json": json.dumps(plan, indent=1) + "\n",
    }
    for name in ("mapper", "guards", "applier", "sell"):
        doc = json.loads((exported / f"{name}.json").read_text())
        doc["source"] = f"reference firmware {sha} ({doc['source']})"
        texts[f"{name}.json"] = json.dumps(doc, indent=0) + "\n"

    check_no_leaks(frames, texts)
    OUT.mkdir(parents=True, exist_ok=True)
    for fname, text in texts.items():
        (OUT / fname).write_text(text)


if __name__ == "__main__":
    main(Path(sys.argv[1]).expanduser(), Path(sys.argv[2]), sys.argv[3])
