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


def _golden_doc(profile_id: str) -> dict:
    return json.loads((GOLDEN / profile_id.replace("-", "_") / "registers.json").read_text())


def golden_words(profile_id: str) -> dict[int, int]:
    """Rejestry holding (FC 3) z `tests/golden/<id>/registers.json`."""
    return {int(a): w for a, w in _golden_doc(profile_id)["registers"].items()}


def _image_from_doc(doc: dict, **over: int) -> RegisterImage:
    words = {int(a): w for a, w in doc["registers"].items()}
    words.update({int(a): w for a, w in over.items()})
    return RegisterImage(words, {int(a): w for a, w in doc.get("input_registers", {}).items()})


def golden_image(profile_id: str, **over: int) -> RegisterImage:
    """Obraz rejestrów profilu; opcjonalny `input_registers` to przestrzeń FC 4. `over` nadpisuje holding."""
    return _image_from_doc(_golden_doc(profile_id), **over)


def deye_words() -> dict[int, int]:
    return golden_words("deye-sg")


def deye_image(**over: int) -> RegisterImage:
    return golden_image("deye-sg", **over)
