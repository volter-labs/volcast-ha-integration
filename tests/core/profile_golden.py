"""Asercje wielokrotnego użytku dla profilu marki — wspólny szablon testów każdego profilu."""
import json
import re
from pathlib import Path

import pytest

from custom_components.volcast.core.modbus.identity import identity_info
from custom_components.volcast.core.profile import PROFILES_DIR
from custom_components.volcast.core.registers import RegisterImage, read_values
from custom_components.volcast.core.transports.modbus_frames import parse_aa55_read

NEUTRAL_DIRECTIONS = ("neutral", "idle")
# Plik w katalogu profili, który nie jest profilem (schemat układu w repo Volcast).
NON_PROFILE_FILES = ("schema-lite.json",)
ORIGINS = ("auto", "manual")
# Stan wzorcowy profilu z automatu musi obejmować te klucze (o ile są w `read`); moce baterii i sieci
# niezerowe — przy 0 W odwrócony znak przechodzi niezauważony.
AUTO_REFERENCE_KEYS = ("soc", "battery_power_w", "grid_power_w", "pv_power_w")
AUTO_NONZERO_KEYS = ("battery_power_w", "grid_power_w")
# Słowa niezapisanego / nieobsługiwanego rejestru — nie mogą wskazywać typu urządzenia.
BLANK_REGISTER_VALUES = (0x0000, 0xFFFF)
# Modele niezwiązane z żadnym falownikiem: regex pasujący do któregokolwiek pasuje do dowolnego modelu
# (`^.`, `^.+`, `^SP1|.`). `re.match` jak w produkcji (identity.py).
MODEL_PROBES = ("A", "Q", "z", "0", "7", "-", "QX", "ZZZZZZZZ", "Q0-Q0", "00000000", "xyz123", "NOT-A-MODEL")
# Górna granica długości `device_type.expect` (dziś najdłuższa lista ma 24 wartości).
MAX_DEVICE_TYPE_EXPECT = 64
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


# Id profili wbudowanych w pakiet; każdy inny profil jest nowy i musi przyjść jako draft z automatu.
PACKAGED_IDS = frozenset(p.stem for p in profile_paths(PROFILES_DIR))


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
        doc = json.loads(registers.read_text(encoding="utf-8"))
        return RegisterImage({int(a): w for a, w in doc["registers"].items()},
                             {int(a): w for a, w in doc.get("input_registers", {}).items()})
    frames = golden_dir / "frames.json"
    assert frames.is_file(), f"{golden_dir}: brak registers.json ani frames.json"
    unit = profile.raw["unit_id"]
    return RegisterImage.from_blocks({f["offset"]: parse_aa55_read(bytes.fromhex(f["response"]), unit, f["count"])
                                      for f in json.loads(frames.read_text(encoding="utf-8")).values()
                                      if f["valid"]})


def assert_reads_decode(profile, image: RegisterImage) -> None:
    """Każdy klucz `read` daje wartość z własnego obrazu golden (brak rejestru = obraz niepełny)."""
    missing = sorted(k for k, v in decode_reads(profile, image).items() if v is None)
    assert not missing, f"{profile.id}: obraz golden nie daje wartości dla {missing}"


def assert_reference_decodes(profile, image: RegisterImage, reference: dict) -> None:
    """Każdy klucz stanu wzorcowego przez rdzeń odczytu; float z tolerancją `approx`."""
    values = decode_reads(profile, image)
    for key, want in reference.items():
        assert key in values, f"{profile.id}: klucz {key!r} z expected.json nie występuje w read"
        assert values[key] == pytest.approx(want), f"{profile.id}: {key} = {values[key]!r}, oczekiwano {want!r}"


def assert_rejects_foreign_images(profile, images) -> None:
    """Profil nie rozpoznaje obrazu golden żadnego innego profilu (identyfikacja: pierwszy trafiony wygrywa)."""
    claimed = [label for pid, label, image in images
               if pid != profile.id and identity_info(profile, image)["matched"]]
    assert not claimed, f"{profile.id}: rozpoznaje obce obrazy golden {claimed}"


def assert_expected_lists_match(profile, expected: dict) -> None:
    """Listy `intents`/`modes` z expected.json zgodne z profilem (spójność plików, nie wyrocznia)."""
    raw = profile.raw
    for key, actual in (("intents", sorted(raw["intents"])), ("modes", sorted(raw.get("modes") or {}))):
        if key in expected:
            assert sorted(expected[key]) == actual, f"{profile.id}: {key} w expected.json {expected[key]} ≠ {actual}"


def assert_origin_rules(profile, expected: dict, golden_dir: Path, packaged_ids=PACKAGED_IDS) -> None:
    """Domknięte bezpiecznie: profil spoza pakietu musi mieć `origin: auto` i zostaje draftem.

    Profil wbudowany może nie mieć `origin` (= pisany ręcznie). `origin: auto` dodatkowo wymaga
    `registers.json` i stanu wzorcowego z SoC i mocami o niezerowym znaku (wąską identyfikację
    sprawdza `assert_identify_not_overbroad` dla każdego profilu)."""
    origin = expected.get("origin")
    assert origin is None or origin in ORIGINS, f"{profile.id}: nieznane origin {origin!r} (dozwolone {ORIGINS})"
    if profile.id not in packaged_ids:
        assert origin == "auto", f"{profile.id}: nowy profil (spoza pakietu) musi mieć origin auto, jest {origin!r}"
    if origin != "auto":
        return
    raw = profile.raw
    assert raw["status"] == "draft", f"{profile.id}: profil z automatu musi mieć status draft"
    assert raw["modbus"]["status"] == "draft", f"{profile.id}: profil z automatu musi mieć modbus.status draft"
    assert (Path(golden_dir) / "registers.json").is_file(), f"{profile.id}: profil z automatu bez registers.json"
    reference = expected["reference"]
    assert reference, f"{profile.id}: profil z automatu bez reference w expected.json"
    required = [k for k in AUTO_REFERENCE_KEYS if k in raw["read"]]
    assert required, f"{profile.id}: profil z automatu nie czyta żadnego z {list(AUTO_REFERENCE_KEYS)}"
    assert not set(required) - set(reference), \
        f"{profile.id}: reference musi obejmować {required}, brak {sorted(set(required) - set(reference))}"
    for key in AUTO_NONZERO_KEYS:
        if key in reference:
            assert reference[key] != 0, f"{profile.id}: reference {key} = 0 nie sprawdza znaku"


def assert_identify_not_overbroad(profile) -> None:
    """Identyfikacja wąska dla KAŻDEGO profilu: regex modelu zakotwiczony `^`, niepasujący do pustego
    modelu (`.*` przepuszcza wszystko) ani do żadnego z `MODEL_PROBES` (`^.` przepuszcza wszystko
    niepuste); `device_type.expect` bez wartości pustego rejestru i najwyżej `MAX_DEVICE_TYPE_EXPECT`."""
    ident = profile.raw.get("identify", {})
    for rx in ident.get("model_regex", []):
        assert rx.startswith("^"), f"{profile.id}: model_regex {rx!r} bez kotwicy ^"
        assert re.search(rx, "") is None, f"{profile.id}: model_regex {rx!r} pasuje do pustego modelu"
        hits = [p for p in MODEL_PROBES if re.match(rx, p)]
        assert not hits, f"{profile.id}: model_regex {rx!r} pasuje do dowolnego modelu (np. {hits[0]!r})"
    expect = ident.get("registers", {}).get("device_type", {}).get("expect", [])
    assert len(expect) <= MAX_DEVICE_TYPE_EXPECT, \
        f"{profile.id}: device_type.expect ma {len(expect)} wartości (max {MAX_DEVICE_TYPE_EXPECT})"
    blank = sorted(set(expect) & set(BLANK_REGISTER_VALUES))
    assert not blank, f"{profile.id}: device_type.expect zawiera wartości pustego rejestru {blank}"


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
        assert caps[cap] is want, f"{profile.id}: capability {cap}={caps[cap]} nie zgadza się z kluczami zapisu"
