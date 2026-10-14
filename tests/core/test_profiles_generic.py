"""Test generyczny profili sterowany danymi: każdy `<katalog>/*.json` przez te same asercje.

Katalog: zmienna `VOLCAST_PROFILES_DIR` czytana WYŁĄCZNIE tutaj (np. `profiles/` repo Volcast, golden
obok profilu w `<id>.golden/`); domyślnie profile wbudowane pakietu (golden w `tests/golden/<id>/`,
bez `expected.json` — stan wzorcowy wbudowanych pilnują testy per profil). Kod integracji zmiennej
nie zna. Testy per profil zostają: pokrywają niuanse marek.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from custom_components.volcast.core import profile_feed
from custom_components.volcast.core.profile import PROFILES_DIR, builtin_ids, load_profile
from custom_components.volcast.core.profile_schema import validate_profile
from tests.core import profile_golden as pg
from tests.core.modbus.helpers import GOLDEN, golden_image, goodwe_image

PROFILES_DIR_ENV = "VOLCAST_PROFILES_DIR"
DIR = Path(os.environ.get(PROFILES_DIR_ENV) or PROFILES_DIR)
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


def _packaged_images() -> list:
    """Obrazy golden pakietu jako (id, etykieta, obraz); goodwe_et to nagrane ramki."""
    out = []
    for d in sorted(GOLDEN.iterdir()):
        pid = d.name.replace("_", "-")
        if d.name == "goodwe_et":
            out.append((pid, f"tests/golden/{d.name}", goodwe_image()))
        elif (d / "registers.json").is_file():
            out.append((pid, f"tests/golden/{d.name}", golden_image(pid)))
    return out


def _directory_images(paths) -> list:
    """Obrazy golden profili katalogu; profil z zepsutym golden pomija się tu (pada we własnym teście)."""
    out = []
    for path in paths:
        if _packaged(path):
            continue
        try:
            out.append((path.stem, f"{path.stem}.golden", pg.golden_dir_image(_golden_dir(path), load_profile(path))))
        except (AssertionError, OSError, ValueError):
            continue
    return out


@pytest.fixture(scope="module")
def all_images():
    return _packaged_images() + _directory_images(PATHS)


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


def test_feed_loader_accepts(case):
    # Profil przyjęty przez tę bramę trafia do podpisanego feedu; ładowarka feedu odrzuca CAŁY feed,
    # gdy choć jeden wpis nie przejdzie jej reguł — brama musi być co najmniej tak samo ścisła.
    path = case[0]
    try:
        profile_feed._one_profile(json.loads(path.read_text(encoding="utf-8")))
    except profile_feed.FeedRejected as err:
        pytest.fail(f"{path.name}: ładowarka feedu odrzuciłaby profil ({err})")


def test_identify_matches_own_golden(case):
    _, profile, golden = case
    pg.assert_identify_matches(profile, pg.golden_dir_image(golden, profile))


def test_identify_not_overbroad(case):
    pg.assert_identify_not_overbroad(case[1])


def test_identify_rejects_every_other_golden(case, all_images):
    pg.assert_rejects_foreign_images(case[1], all_images)


def test_every_read_key_decodes(case):
    _, profile, golden = case
    pg.assert_reads_decode(profile, pg.golden_dir_image(golden, profile))


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


def test_capabilities_match_writes(case):
    pg.assert_capabilities_match_writes(case[1])


def test_ha_regexes_compile(case):
    pg.assert_ha_regexes_compile(case[1])


def test_sources_public(case):
    pg.assert_sources_public(case[1].raw)


def test_origin_rules(case):
    path, profile, golden = case
    if (expected := _expected(path)) is not None:
        pg.assert_origin_rules(profile, expected, golden)


# --- Środowisko: zmienna dotyczy tylko tego testu, lista profili ---


def test_integration_ignores_profiles_dir_env(monkeypatch, tmp_path):
    shutil.copy(PROFILES_DIR / "sofar-hyd.json", tmp_path / "zz-foreign.json")
    packaged = builtin_ids()
    monkeypatch.setenv(PROFILES_DIR_ENV, str(tmp_path))
    assert builtin_ids() == packaged
    assert packaged and "zz-foreign" not in packaged


def test_profile_paths_skip_schema_file(tmp_path):
    for name in ("b.json", "a.json", "schema-lite.json", "notes.md"):
        (tmp_path / name).write_text("{}")
    assert [p.name for p in pg.profile_paths(tmp_path)] == ["a.json", "b.json"]


def test_packaged_ids_are_the_builtin_profiles():
    assert pg.PACKAGED_IDS == frozenset(builtin_ids())


# --- Fixtury zewnętrznego katalogu: zestaw dobry przechodzi, zepsuty pada ---

# Stan wzorcowy obrazu tests/golden/sofar_hyd (README katalogu), konwencja rdzenia: bateria dodatnia =
# rozładowanie, sieć dodatnia = pobór. Wpisany ręcznie — niezależna wyrocznia znaku mocy.
SOFAR_REF = {"soc": 55, "pv_power_w": 3200, "battery_power_w": -1500, "grid_power_w": 400, "load_power_w": 2100}
NEW_ID = "acme-new"


def _external(tmp_path, *, pid="sofar-hyd", profile_edit=None, expected_edit=None, with_expected=True):
    """Katalog w układzie repo Volcast z profilem sofar-hyd (opcjonalnie pod innym id) i jego obrazem."""
    raw = json.loads((PROFILES_DIR / "sofar-hyd.json").read_text(encoding="utf-8"))
    raw["id"] = pid
    if profile_edit:
        profile_edit(raw)
    path = tmp_path / f"{pid}.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    golden = tmp_path / f"{pid}.golden"
    golden.mkdir()
    shutil.copy(GOLDEN / "sofar_hyd" / "registers.json", golden / "registers.json")
    expected = {"reference": dict(SOFAR_REF), "intents": sorted(raw["intents"]),
                "modes": sorted(raw.get("modes", {}))}
    if expected_edit:
        expected_edit(expected)
    if with_expected:
        (golden / "expected.json").write_text(json.dumps(expected), encoding="utf-8")
    return path, golden


def _run_all(path, golden, foreign=None):
    """Wszystkie asercje testu generycznego; obce obrazy domyślnie = obrazy pakietu (bez sofar_hyd,
    bo fixtura pod nowym id to kopia sofar-hyd)."""
    profile = load_profile(path)
    expected = pg.load_expected(golden)
    image = pg.golden_dir_image(golden, profile)
    if foreign is None:
        foreign = [i for i in _packaged_images() if i[0] != "sofar-hyd"]
    pg.assert_identify_matches(profile, image)
    pg.assert_rejects_foreign_images(profile, foreign)
    pg.assert_identify_not_overbroad(profile)
    pg.assert_reads_decode(profile, image)
    pg.assert_reference_decodes(profile, image, expected["reference"])
    pg.assert_intents_consistent(profile)
    pg.assert_expected_lists_match(profile, expected)
    pg.assert_capabilities_match_writes(profile)
    pg.assert_ha_regexes_compile(profile)
    pg.assert_sources_public(profile.raw)
    pg.assert_origin_rules(profile, expected, golden)


def _auto(e):
    e["origin"] = "auto"


def _verified(raw):
    raw["status"] = "verified"
    raw["modbus"]["status"] = "verified"


def test_external_fixture_passes(tmp_path):
    _run_all(*_external(tmp_path), foreign=_packaged_images())


def test_auto_draft_fixture_passes(tmp_path):
    _run_all(*_external(tmp_path, expected_edit=_auto), foreign=_packaged_images())


def test_new_auto_draft_fixture_passes(tmp_path):
    _run_all(*_external(tmp_path, pid=NEW_ID, expected_edit=_auto))


def _generic_run(profiles_dir: Path) -> subprocess.CompletedProcess:
    """Ten moduł uruchomiony jak w CI repo Volcast: katalog profili ze zmiennej środowiskowej."""
    env = {**os.environ, PROFILES_DIR_ENV: str(profiles_dir)}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", __file__,
         "-k", "test_profile_validates or test_feed_loader_accepts"],
        cwd=Path(__file__).resolve().parents[2], env=env, capture_output=True, text=True, timeout=300)


def test_generic_run_passes_a_good_directory(tmp_path):
    _external(tmp_path, pid=NEW_ID, expected_edit=_auto)
    run = _generic_run(tmp_path)
    assert run.returncode == 0, run.stdout[-2000:]


@pytest.mark.parametrize("regex", ["^" + "A" * 250, "^(a+)+$", "^(?:[0-9a-z]+_)+soc$", "^(unclosed"],
                         ids=["too_long", "nested_quantifier", "nested_noncapturing", "uncompilable"])
def test_unsafe_ha_regex_fails_the_generic_run(tmp_path, regex):
    # Wzorzec, którego ładowarka feedu nie przyjmie, nie może przejść bramy profili.
    def edit(raw):
        raw["ha"]["integrations"][0]["entities"]["soc"]["unique_id_regex"] = regex
    _external(tmp_path, pid=NEW_ID, profile_edit=edit, expected_edit=_auto)
    raw = json.loads((tmp_path / f"{NEW_ID}.json").read_text(encoding="utf-8"))
    assert any(e.startswith("$.ha.integrations[0].entities.soc.unique_id_regex:") for e in validate_profile(raw))
    run = _generic_run(tmp_path)
    assert run.returncode == 1, run.stdout[-2000:]
    assert NEW_ID in run.stdout


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


def test_unreadable_read_key_fails(tmp_path):
    def phantom(raw):
        raw["read"]["load_power_w"] = {"addr": 40000, "type": "u16"}  # rejestru brak w obrazie
    with pytest.raises(AssertionError, match="load_power_w"):
        _run_all(*_external(tmp_path, profile_edit=phantom))


def test_capability_without_write_fails(tmp_path):
    def claim(raw):
        raw["write"].pop("soc_min", None)
        raw["capabilities"]["set_soc_floor"] = True
    with pytest.raises(AssertionError, match="set_soc_floor"):
        _run_all(*_external(tmp_path, profile_edit=claim))


@pytest.mark.parametrize("regex", [".*", "^.*", "SP1"])
def test_broad_model_regex_of_auto_profile_fails(tmp_path, regex):
    def broaden(raw):
        raw["identify"]["model_regex"] = [regex]
    with pytest.raises(AssertionError, match="model_regex"):
        _run_all(*_external(tmp_path, pid=NEW_ID, profile_edit=broaden, expected_edit=_auto))


@pytest.mark.parametrize("regex", [".*", "^.*", "SP1", "^.", "^.+", "^SP1|.", r"^\S", "^[A-Za-z0-9]"])
def test_broad_model_regex_of_packaged_manual_verified_profile_fails(tmp_path, regex):
    # Id wbudowane, bez origin, verified: regex `.*` nie może przejść tylko dlatego, że to nie automat.
    def broaden(raw):
        _verified(raw)
        raw["identify"]["model_regex"] = [regex]
    with pytest.raises(AssertionError, match="model_regex"):
        _run_all(*_external(tmp_path, profile_edit=broaden), foreign=_packaged_images())


@pytest.mark.parametrize("value", [0, 0xFFFF])
def test_device_type_expect_blank_value_fails(tmp_path, value):
    # Identyfikacja po typie urządzenia: 0 i 0xFFFF to wartości pustego rejestru, nie typ.
    path, _ = _external(tmp_path, pid=NEW_ID, expected_edit=_auto)
    raw = json.loads((PROFILES_DIR / "deye-sg.json").read_text(encoding="utf-8"))
    raw["id"] = NEW_ID
    raw["identify"]["registers"]["device_type"]["expect"] = [5, value]
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(AssertionError, match="device_type.expect"):
        pg.assert_identify_not_overbroad(load_profile(path))


def test_device_type_expect_too_broad_fails(tmp_path):
    # Lista typów obejmująca prawie całą przestrzeń rejestru to nie identyfikacja.
    path, _ = _external(tmp_path, pid=NEW_ID, expected_edit=_auto)
    raw = json.loads((PROFILES_DIR / "deye-sg.json").read_text(encoding="utf-8"))
    raw["id"] = NEW_ID
    raw["identify"]["registers"]["device_type"]["expect"] = list(range(1, 65535))
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(AssertionError, match="device_type.expect"):
        pg.assert_identify_not_overbroad(load_profile(path))


def test_identify_overlapping_another_golden_fails(tmp_path):
    # Id inne niż sofar-hyd, identyfikacja jak sofar → rozpoznaje obraz tests/golden/sofar_hyd.
    with pytest.raises(AssertionError, match="sofar_hyd"):
        _run_all(*_external(tmp_path, pid=NEW_ID, expected_edit=_auto), foreign=_packaged_images())


# --- Reguły origin (domknięte bezpiecznie) ---


def _origin(tmp_path, **kw):
    path, golden = _external(tmp_path, **kw)
    pg.assert_origin_rules(load_profile(path), pg.load_expected(golden), golden)


def test_auto_origin_must_stay_draft(tmp_path):
    with pytest.raises(AssertionError, match="status draft"):
        _origin(tmp_path, profile_edit=_verified, expected_edit=_auto)


def test_auto_origin_modbus_status_alone_verified_fails(tmp_path):
    def modbus_only(raw):
        raw["modbus"]["status"] = "verified"
    with pytest.raises(AssertionError, match="modbus.status draft"):
        _origin(tmp_path, profile_edit=modbus_only, expected_edit=_auto)


def test_new_id_without_origin_fails(tmp_path):
    with pytest.raises(AssertionError, match="origin auto"):
        _origin(tmp_path, pid=NEW_ID, profile_edit=_verified)


def test_new_id_with_manual_origin_fails(tmp_path):
    with pytest.raises(AssertionError, match="origin auto"):
        _origin(tmp_path, pid=NEW_ID, expected_edit=lambda e: e.update(origin="manual"))


def test_new_id_auto_verified_fails(tmp_path):
    with pytest.raises(AssertionError, match="draft"):
        _origin(tmp_path, pid=NEW_ID, profile_edit=_verified, expected_edit=_auto)


@pytest.mark.parametrize("origin", [None, "manual"])
def test_packaged_id_manual_verified_passes(tmp_path, origin):
    edit = (lambda e: e.update(origin=origin)) if origin else None
    _origin(tmp_path, profile_edit=_verified, expected_edit=edit)


def test_unknown_origin_fails(tmp_path):
    with pytest.raises(AssertionError, match="origin"):
        _origin(tmp_path, expected_edit=lambda e: e.update(origin="Auto"))


def test_auto_origin_requires_reference(tmp_path):
    with pytest.raises(AssertionError, match="reference"):
        _origin(tmp_path, expected_edit=lambda e: e.update(origin="auto", reference=None))


@pytest.mark.parametrize("key", ["soc", "battery_power_w", "grid_power_w", "pv_power_w"])
def test_auto_reference_must_cover_power_and_soc(tmp_path, key):
    def drop(e):
        e.update(origin="auto")
        del e["reference"][key]
    with pytest.raises(AssertionError, match=key):
        _origin(tmp_path, expected_edit=drop)


def test_auto_profile_reading_no_power_or_soc_fails(tmp_path):
    def strip(raw):
        for key in pg.AUTO_REFERENCE_KEYS:
            raw["read"].pop(key, None)
    def ref(e):
        e.update(origin="auto")
        e["reference"] = {"load_power_w": 2100}
    with pytest.raises(AssertionError, match="nie czyta"):
        _origin(tmp_path, profile_edit=strip, expected_edit=ref)


@pytest.mark.parametrize("key", ["battery_power_w", "grid_power_w"])
def test_auto_reference_zero_power_fails(tmp_path, key):
    def zero(e):
        e.update(origin="auto")
        e["reference"][key] = 0
    with pytest.raises(AssertionError, match=f"{key} = 0"):
        _origin(tmp_path, expected_edit=zero)


def test_auto_origin_requires_registers_image(tmp_path):
    path, golden = _external(tmp_path, expected_edit=_auto)
    (golden / "registers.json").unlink()
    with pytest.raises(AssertionError, match="registers.json"):
        pg.assert_origin_rules(load_profile(path), pg.load_expected(golden), golden)
