"""Test generyczny profili sterowany danymi: każdy `<katalog>/*.json` przez te same asercje.

Katalog: `VOLCAST_PROFILES_DIR` (np. `profiles/` repo Volcast, golden obok profilu w `<id>.golden/`),
domyślnie profile wbudowane pakietu (golden w `tests/golden/<id>/`, bez `expected.json` — stan
wzorcowy wbudowanych pilnują testy per profil). Testy per profil zostają: pokrywają niuanse marek.
"""
import json
import shutil
from pathlib import Path

import pytest

from custom_components.volcast.core.profile import PROFILES_DIR, load_profile, profiles_dir
from custom_components.volcast.core.profile_schema import validate_profile
from tests.core import profile_golden as pg
from tests.core.modbus.helpers import GOLDEN, golden_image, goodwe_image
from tests.core.test_profile_sofar_hyd import REFERENCE as SOFAR_REFERENCE

DIR = profiles_dir()
PATHS = pg.profile_paths(DIR)


def _packaged(path: Path) -> bool:
    # Profil wbudowany bez katalogu golden obok: dane w `tests/golden/<id>/`, bez expected.json.
    return path.parent.resolve() == PROFILES_DIR and not (path.parent / f"{path.stem}.golden").is_dir()


def _golden_dir(path: Path) -> Path:
    if not _packaged(path):
        return path.parent / f"{path.stem}.golden"
    return GOLDEN / path.stem.replace("-", "_")


def _expected(path: Path) -> dict | None:
    # Wbudowane profile pakietu nie mają `expected.json` — ich stan wzorcowy jest w testach per profil.
    if _packaged(path):
        return None
    return pg.load_expected(_golden_dir(path))


def _foreign_image(profile):
    # Obraz GoodWe ET jest obcy dla każdego profilu poza samym goodwe-et; dla niego obcy jest Deye.
    return golden_image("deye-sg") if profile.id == "goodwe-et" else goodwe_image()


@pytest.fixture(params=PATHS, ids=[p.stem for p in PATHS])
def case(request):
    path = request.param
    return path, load_profile(path), _golden_dir(path)


def test_directory_has_profiles():
    assert PATHS, f"brak profili w {DIR}"


def test_profile_validates(case):
    path, profile, _ = case
    assert validate_profile(json.loads(path.read_text(encoding="utf-8"))) == []
    assert profile.id == path.stem


def test_identify_matches_own_golden_and_rejects_foreign(case):
    _, profile, golden = case
    pg.assert_identify_matches(profile, pg.golden_dir_image(golden, profile))
    pg.assert_identify_rejects(profile, _foreign_image(profile))


def test_reads_decode_expected_reference(case):
    path, profile, golden = case
    expected = _expected(path)
    if expected is None or expected["reference"] is None:
        pytest.skip("brak stanu wzorcowego w expected.json (ramki nagrane albo profil wbudowany)")
    pg.assert_reference_decodes(profile, pg.golden_dir_image(golden, profile), expected["reference"])


def test_intents_consistent(case):
    path, profile, _ = case
    pg.assert_intents_consistent(profile)
    if (expected := _expected(path)) is not None:
        pg.assert_expected_lists_match(profile, expected)


def test_ha_regexes_compile(case):
    pg.assert_ha_regexes_compile(case[1])


def test_sources_public(case):
    pg.assert_sources_public(case[1].raw)


def test_origin_rules(case):
    path, profile, golden = case
    if (expected := _expected(path)) is not None:
        pg.assert_origin_rules(profile, expected, golden)


# --- Środowisko: wybór katalogu i lista profili ---


def test_profiles_dir_defaults_to_package(monkeypatch):
    monkeypatch.delenv("VOLCAST_PROFILES_DIR", raising=False)
    assert profiles_dir() == PROFILES_DIR


def test_profiles_dir_reads_env(monkeypatch, tmp_path):
    monkeypatch.setenv("VOLCAST_PROFILES_DIR", str(tmp_path))
    assert profiles_dir() == tmp_path


def test_profile_paths_skip_schema_file(tmp_path):
    for name in ("b.json", "a.json", "schema-lite.json", "notes.md"):
        (tmp_path / name).write_text("{}")
    assert [p.name for p in pg.profile_paths(tmp_path)] == ["a.json", "b.json"]


# --- Fixtury zewnętrznego katalogu: zestaw dobry przechodzi, zepsuty pada ---


def _external(tmp_path, *, profile_edit=None, expected_edit=None, with_expected=True):
    """Katalog w układzie repo Volcast z profilem sofar-hyd i jego obrazem golden."""
    raw = json.loads((PROFILES_DIR / "sofar-hyd.json").read_text(encoding="utf-8"))
    if profile_edit:
        profile_edit(raw)
    path = tmp_path / "sofar-hyd.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    golden = tmp_path / "sofar-hyd.golden"
    golden.mkdir()
    shutil.copy(GOLDEN / "sofar_hyd" / "registers.json", golden / "registers.json")
    expected = {"reference": dict(SOFAR_REFERENCE), "intents": sorted(raw["intents"]),
                "modes": sorted(raw.get("modes", {}))}
    if expected_edit:
        expected_edit(expected)
    if with_expected:
        (golden / "expected.json").write_text(json.dumps(expected), encoding="utf-8")
    return path, golden


def _run_all(path, golden):
    profile = load_profile(path)
    expected = pg.load_expected(golden)
    image = pg.golden_dir_image(golden, profile)
    pg.assert_identify_matches(profile, image)
    pg.assert_identify_rejects(profile, goodwe_image())
    pg.assert_reference_decodes(profile, image, expected["reference"])
    pg.assert_intents_consistent(profile)
    pg.assert_expected_lists_match(profile, expected)
    pg.assert_ha_regexes_compile(profile)
    pg.assert_sources_public(profile.raw)
    pg.assert_origin_rules(profile, expected, golden)


def test_external_fixture_passes(tmp_path):
    _run_all(*_external(tmp_path))


def test_auto_draft_fixture_passes(tmp_path):
    _run_all(*_external(tmp_path, expected_edit=lambda e: e.update(origin="auto")))


def test_wrong_power_sign_fails(tmp_path):
    def flip(raw):
        raw["read"]["battery_power_w"]["sign"] = 1  # Sofar: dodatnie przy ładowaniu → potrzebne -1
    with pytest.raises(AssertionError, match="battery_power_w"):
        _run_all(*_external(tmp_path, profile_edit=flip))


def test_missing_expected_fails_readably(tmp_path):
    with pytest.raises(AssertionError, match="expected.json"):
        _run_all(*_external(tmp_path, with_expected=False))


def test_reference_key_outside_read_fails(tmp_path):
    def extra(e):
        e["reference"]["no_such_key"] = 1
    with pytest.raises(AssertionError, match="no_such_key"):
        _run_all(*_external(tmp_path, expected_edit=extra))


def test_expected_intents_drift_fails(tmp_path):
    with pytest.raises(AssertionError, match="intents"):
        _run_all(*_external(tmp_path, expected_edit=lambda e: e["intents"].pop()))


def test_auto_origin_must_stay_draft(tmp_path):
    def verified(raw):
        raw["status"] = "verified"
        raw["modbus"]["status"] = "verified"
    with pytest.raises(AssertionError, match="draft"):
        _run_all(*_external(tmp_path, profile_edit=verified, expected_edit=lambda e: e.update(origin="auto")))


def test_auto_origin_requires_reference(tmp_path):
    path, golden = _external(tmp_path, expected_edit=lambda e: e.update(origin="auto", reference=None))
    with pytest.raises(AssertionError, match="reference"):
        pg.assert_origin_rules(load_profile(path), pg.load_expected(golden), golden)


def test_auto_origin_requires_registers_image(tmp_path):
    path, golden = _external(tmp_path, expected_edit=lambda e: e.update(origin="auto"))
    (golden / "registers.json").unlink()
    with pytest.raises(AssertionError, match="registers.json"):
        pg.assert_origin_rules(load_profile(path), pg.load_expected(golden), golden)


def test_unknown_origin_fails(tmp_path):
    path, golden = _external(tmp_path, expected_edit=lambda e: e.update(origin="Auto"))
    with pytest.raises(AssertionError, match="origin"):
        pg.assert_origin_rules(load_profile(path), pg.load_expected(golden), golden)


def test_missing_origin_is_manual_and_allows_verified(tmp_path):
    def verified(raw):
        raw["status"] = "verified"
    path, golden = _external(tmp_path, profile_edit=verified)
    pg.assert_origin_rules(load_profile(path), pg.load_expected(golden), golden)
