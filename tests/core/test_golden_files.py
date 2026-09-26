"""Złote wektory z implementacji referencyjnej: kompletność i brak danych identyfikujących."""
import json
import re
from pathlib import Path

G = Path(__file__).resolve().parents[1] / "golden" / "goodwe_et"

FAKE_SERIAL = b"GOLDENSERIAL0000"
SERIAL_RE = re.compile(rb"\d{5}ETU\d{3}W\d{4}")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-")
# Znane, nieidentyfikujące napisy w odpowiedziach: model i wersje firmware'u.
KNOWN_TEXT = re.compile(rb"GOLDENSERIAL0000|GW8KN-ET|\d{5}-\d{2}-S\d{2}")


def _frames() -> dict:
    return json.loads((G / "frames.json").read_text())


def test_all_golden_files_present_and_nonempty():
    for name, minimum in (("mapper", 300), ("guards", 1000), ("applier", 20)):
        doc = json.loads((G / f"{name}.json").read_text())
        assert doc["source"].startswith("reference firmware")
        assert len(doc["vectors"]) >= minimum


def test_live_plan_is_anonymised():
    plan = json.loads((G / "plan_live.json").read_text())
    assert plan["schedule_id"] == "golden-plan"
    assert len(plan["slots"]) == 14


def test_device_info_carries_fake_serial_with_valid_crc():
    raw = bytes.fromhex(_frames()["device_info"]["response"])
    assert FAKE_SERIAL in raw
    crc = 0xFFFF
    for b in raw[2:-2]:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    assert raw[-2:] == bytes((crc & 0xFF, crc >> 8))


def test_no_serials_in_decoded_frames():
    # Ramki są w hex — sprawdzamy zdekodowane bajty, nie tekst JSON.
    for name, block in _frames().items():
        for field in ("request", "response"):
            raw = bytes.fromhex(block[field])
            assert not SERIAL_RE.search(raw), f"serial w {name}.{field}"
            for run in re.findall(rb"[\x20-\x7e]{16,}", raw):
                rest = KNOWN_TEXT.sub(b"", run).strip()
                assert len(rest) < 16, f"nieznany napis w {name}.{field}"


def test_no_serials_or_uuids_in_json_text():
    blob = "".join(p.read_text() for p in G.glob("*.json"))
    assert not SERIAL_RE.search(blob.encode())
    assert not UUID_RE.search(blob)
