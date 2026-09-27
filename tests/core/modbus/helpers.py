"""Obrazy rejestrów z danych złotych — wspólne dla testów ścieżki rejestrów."""
import json
from pathlib import Path

from custom_components.volcast.core.registers import RegisterImage
from custom_components.volcast.core.transports.modbus_frames import parse_aa55_read

GOLDEN = Path(__file__).resolve().parents[2] / "golden"


def goodwe_image() -> RegisterImage:
    frames = json.loads((GOLDEN / "goodwe_et" / "frames.json").read_text())
    return RegisterImage.from_blocks({f["offset"]: parse_aa55_read(bytes.fromhex(f["response"]), 0xF7, f["count"])
                                      for f in frames.values() if f["valid"]})


def deye_words() -> dict[int, int]:
    doc = json.loads((GOLDEN / "deye_sg" / "registers.json").read_text())
    return {int(a): w for a, w in doc["registers"].items()}


def deye_image(**over: int) -> RegisterImage:
    words = deye_words()
    words.update({int(a): w for a, w in over.items()})
    return RegisterImage(words)
