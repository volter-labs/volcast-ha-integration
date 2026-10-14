"""Asercje wielokrotnego użytku dla profilu marki — wspólny szablon testów każdego profilu."""
import json
import re
from pathlib import Path

import pytest

from custom_components.volcast.core.modbus.identity import identity_info
from custom_components.volcast.core.registers import RegisterImage, read_values
from custom_components.volcast.core.transports.modbus_frames import parse_aa55_read
from tests.core.modbus.helpers import _image_from_doc

NEUTRAL_DIRECTIONS = ("neutral", "idle")
# Plik w katalogu profili, który nie jest profilem (schemat układu w repo Volcast).
NON_PROFILE_FILES = ("schema-lite.json",)
ORIGINS = ("auto", "manual")
# Odnośniki, których profil w publicznym repo nie może cytować: załączniki wrzucane na GitHub
# (user-attachments) i pliki wgrane na fora (iobroker, loxforum) bywają kopiami dokumentów producenta
# z zakazem redystrybucji; linki do dyskusji są dozwolone.
NON_PUBLIC_REF_MARKERS = (
    "user-attachments",
    "forum.iobroker.net/assets",
    "loxforum.com/filedata",
    "/assets/uploads/files",
)


def profile_paths(directory: Path) -> list[Path]:
    """Pliki profili katalogu (`*.json` bez plików pomocniczych), posortowane."""
    return sorted(p for p in Path(directory).glob("*.json") if p.name not in NON_PROFILE_FILES)


def load_expected(golden_dir: Path) -> dict:
    """`expected.json` obok obrazu golden; brak pliku = czytelny błąd, nie cichy pominięty test."""
    path = Path(golden_dir) / "expected.json"
    assert path.is_file(), f"brak {path} — profil z zewnętrznego katalogu musi mieć expected.json"
    expected = json.loads(path.read_text(encoding="utf-8"))
    assert "reference" in expected, f"{path}: brak klucza reference (null, gdy obrazu nie ma)"
    return expected


def golden_dir_image(golden_dir: Path, profile) -> RegisterImage:
    """Obraz rejestrów z `registers.json` albo z nagranych ramek AA55 (`frames.json`, GoodWe)."""
    golden_dir = Path(golden_dir)
    registers = golden_dir / "registers.json"
    if registers.is_file():
        return _image_from_doc(json.loads(registers.read_text(encoding="utf-8")))
    frames = golden_dir / "frames.json"
    assert frames.is_file(), f"{golden_dir}: brak registers.json ani frames.json"
    unit = profile.raw["unit_id"]
    return RegisterImage.from_blocks({f["offset"]: parse_aa55_read(bytes.fromhex(f["response"]), unit, f["count"])
                                      for f in json.loads(frames.read_text(encoding="utf-8")).values()
                                      if f["valid"]})


def assert_reference_decodes(profile, image: RegisterImage, reference: dict) -> None:
    """Każdy klucz stanu wzorcowego przez rdzeń odczytu; float z tolerancją `approx`."""
    values = decode_reads(profile, image)
    for key, want in reference.items():
        assert key in values, f"{profile.id}: klucz {key!r} z expected.json nie występuje w read"
        assert values[key] == pytest.approx(want), f"{profile.id}: {key} = {values[key]!r}, oczekiwano {want!r}"


def assert_expected_lists_match(profile, expected: dict) -> None:
    """Listy `intents`/`modes` z expected.json zgodne z profilem (spójność plików, nie wyrocznia)."""
    raw = profile.raw
    for key, actual in (("intents", sorted(raw["intents"])), ("modes", sorted(raw.get("modes") or {}))):
        if key in expected:
            assert sorted(expected[key]) == actual, f"{profile.id}: {key} w expected.json {expected[key]} ≠ {actual}"


def assert_origin_rules(profile, expected: dict, golden_dir: Path) -> None:
    """Profil z automatu (`origin: auto`) zostaje draftem i ma własny obraz rejestrów ze stanem wzorcowym.

    Brak `origin` = profil pisany ręcznie (bez wymogu draftu)."""
    origin = expected.get("origin", "manual")
    assert origin in ORIGINS, f"{profile.id}: nieznane origin {origin!r} (dozwolone {ORIGINS})"
    if origin != "auto":
        return
    raw = profile.raw
    assert raw["status"] == "draft", f"{profile.id}: profil z automatu musi mieć status draft"
    assert raw["modbus"]["status"] == "draft", f"{profile.id}: profil z automatu musi mieć modbus.status draft"
    assert (Path(golden_dir) / "registers.json").is_file(), f"{profile.id}: profil z automatu bez registers.json"
    assert expected["reference"], f"{profile.id}: profil z automatu bez reference w expected.json"


def assert_sources_public(raw: dict) -> None:
    """Każde `sources[].ref` i oba `status_note` bez odnośników do niepublicznych kopii dokumentów."""
    texts = [s["ref"] for s in raw["sources"]] + [s["what"] for s in raw["sources"]]
    texts += [raw.get("status_note", ""), raw.get("modbus", {}).get("status_note", "")]
    for text in texts:
        for marker in NON_PUBLIC_REF_MARKERS:
            assert marker not in text, f"niepubliczny odnośnik ({marker}): {text[:80]}"


def assert_identify_matches(profile, image: RegisterImage) -> None:
    info = identity_info(profile, image)
    assert info["matched"], f"{profile.id}: obraz nie pasuje do identyfikacji profilu ({info})"


def assert_identify_rejects(profile, image: RegisterImage) -> None:
    assert not identity_info(profile, image)["matched"], f"{profile.id}: obcy obraz został rozpoznany"


def decode_reads(profile, image: RegisterImage) -> dict:
    """Wszystkie klucze `read` (sum/ref/scale/sign/word_order/fc) przez rdzeń; brak rejestru = None."""
    return read_values(profile.raw["read"], image)


def assert_intents_consistent(profile) -> None:
    raw = profile.raw
    intents = raw["intents"]
    if raw["control_model"] == "mode_setpoint":
        modes = raw["modes"]
        for name, spec in intents.items():
            assert spec["mode"] in modes, f"intent {name}: tryb {spec['mode']!r} spoza modes"
        assert modes[raw["neutral_mode"]]["direction"] in NEUTRAL_DIRECTIONS
        assert raw["baseline"]["mode"] in modes
    else:
        assert intents.get("self_consume") is not None, "brak intentu self_consume"
        assert raw["capabilities"]["time_windows"] == raw["tou"]["programs"]


def assert_ha_regexes_compile(profile) -> None:
    for integ in profile.raw["ha"]["integrations"]:
        for key, ent in integ["entities"].items():
            re.compile(ent["unique_id_regex"])  # błąd = test czerwony z nazwą klucza w śladzie
            assert ent["unique_id_regex"], f"{integ['domain']}.{key}: pusty regex"


def assert_capabilities_match_writes(profile) -> None:
    raw = profile.raw
    caps, write = raw["capabilities"], raw["write"]
    if raw["control_model"] == "time_window":
        prog = write.get("tou_program", {})
        has_power, has_soc = "power_w" in prog, "soc" in prog
        floor = ceiling = has_soc
    else:
        has_power = "power_w" in write
        floor, ceiling = "soc_min" in write, "soc_max" in write
    expected = {
        "set_power_w": has_power,
        "set_soc_floor": floor,
        "set_soc_ceiling": ceiling,
        "limit_export": "export_limit_w" in write or "export_limit_enabled" in write,
    }
    for cap, want in expected.items():
        assert caps[cap] is want, f"capability {cap}={caps[cap]} nie zgadza się z kluczami zapisu"
