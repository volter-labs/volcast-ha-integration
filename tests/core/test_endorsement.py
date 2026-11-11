"""Certyfikat zapisu profilu z feedu: digest, podpisywany komunikat i weryfikacja Ed25519 (rdzeń, bez HA).

Wspólne wektory: `tests/fixtures/profile_feed/endorsement-vectors.json` — kopia pliku chmury
(`profiles/fixtures/endorsement-vectors.json`). Z `VOLCAST_PROFILES_DIR` (katalog `profiles/` repo
chmury) test zgodności porównuje kopię z oryginałem (wszystko poza opisem) i sprawdza oryginał."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from custom_components.volcast.core import endorsement as en

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "profile_feed" / "endorsement-vectors.json"
PROFILES_DIR_ENV = "VOLCAST_PROFILES_DIR"
VECTORS = json.loads(FIXTURE.read_text(encoding="utf-8"))
KEYS = VECTORS["keys"]


def _check_vectors(vectors: dict) -> dict:
    """{nazwa przypadku: (oczekiwany, otrzymany)} dla przypadków niezgodnych z implementacją."""
    keys = vectors["keys"]
    wrong = {}
    for case in vectors["cases"]:
        got = en.verify_endorsement(case["profile"], keys)
        exp = case["expect"]
        if exp["ok"]:
            match = got.ok and got.key_id == exp["key_id"] and got.reason is None
        else:
            match = (not got.ok) and got.reason == exp["reason"] and got.key_id is None
        if not match:
            wrong[case["name"]] = (exp, got)
    return wrong


# --- wektory wspólne z chmurą -------------------------------------------------------------------------

def test_vector_constants_match_the_module():
    assert VECTORS["domain"] == en.ENDORSEMENT_DOMAIN == "volcast-profile-endorsement/v1"
    assert VECTORS["scope"] == en.ENDORSEMENT_SCOPE == "write"
    assert tuple(VECTORS["excluded_fields"]) == en.ENDORSEMENT_EXCLUDED_FIELDS


def test_vector_digest_and_message_are_reproduced():
    profile = VECTORS["profile"]
    assert en.endorsement_digest(profile) == VECTORS["digest"]
    msg = en.endorsement_message(profile["id"], VECTORS["digest"], VECTORS["endorsement"]["key_id"])
    assert msg == VECTORS["message"].encode("utf-8")


def test_vector_signature_verifies_with_the_published_key():
    profile = dict(VECTORS["profile"], endorsement=VECTORS["endorsement"])
    got = en.verify_endorsement(profile, KEYS)
    assert got.ok and got.key_id == VECTORS["endorsement"]["key_id"]


def test_every_vector_case_gives_the_expected_result():
    cases = VECTORS["cases"]
    assert len(cases) >= 7 and {c["expect"]["ok"] for c in cases} == {True, False}
    assert {c["expect"].get("reason") for c in cases} >= {
        "missing", "malformed", "unknown_key", "digest_mismatch", "bad_signature"}
    wrong = _check_vectors(VECTORS)
    assert not wrong, f"wynik inny niż w wektorach (oczekiwany, otrzymany): {wrong}"


def test_test_key_pairs_match_their_public_halves():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    for pair in VECTORS["test_keys"]:
        priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(pair["seed_hex"]))
        assert priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex() == pair["public_hex"]


def test_copy_matches_the_cloud_vectors_when_available():
    root = os.environ.get(PROFILES_DIR_ENV)
    if not root:
        pytest.skip("plik chmury tylko z VOLCAST_PROFILES_DIR")
    path = Path(root) / "fixtures" / "endorsement-vectors.json"
    assert path.is_file(), f"brak {path}: wspólne wektory certyfikatu są wymagane"
    cloud = json.loads(path.read_text(encoding="utf-8"))
    strip = lambda d: {k: v for k, v in d.items() if k != "description"}   # noqa: E731
    assert strip(cloud) == strip(VECTORS), "kopia wektorów rozjechała się z plikiem chmury"
    wrong = _check_vectors(cloud)
    assert not wrong, f"wektory chmury niezgodne z implementacją: {wrong}"


# --- klucz publiczny certyfikatów --------------------------------------------------------------------

def test_single_endorsement_key_is_pinned():
    assert dict(en.PROFILE_ENDORSEMENT_PUBLIC_KEYS) == {
        "endorse-2026-11": "ad139fc29126a322912809f347d013e9df05be29f777a287f495a3c930ada61c"}


def test_endorsement_keys_are_read_only():
    with pytest.raises(TypeError):
        en.PROFILE_ENDORSEMENT_PUBLIC_KEYS["x"] = "00" * 32      # type: ignore[index]


def test_test_key_is_not_a_production_key():
    assert not set(KEYS) & set(en.PROFILE_ENDORSEMENT_PUBLIC_KEYS)
    assert not set(KEYS.values()) & set(en.PROFILE_ENDORSEMENT_PUBLIC_KEYS.values())


def test_vector_certificate_is_unknown_to_the_production_keys():
    profile = dict(VECTORS["profile"], endorsement=VECTORS["endorsement"])
    got = en.verify_endorsement(profile)                 # domyślnie klucze wydania
    assert not got.ok and got.reason == "unknown_key"


# --- ścisły kształt i kolejność sprawdzeń ------------------------------------------------------------

def _valid() -> dict:
    return copy.deepcopy(dict(VECTORS["profile"], endorsement=VECTORS["endorsement"]))


def _with(**fields) -> dict:
    p = _valid()
    p["endorsement"] = dict(p["endorsement"], **fields)
    return p


@pytest.mark.parametrize("profile", [None, [], "x", 1, {}, {"id": "a"}])
def test_missing_certificate(profile):
    got = en.verify_endorsement(profile, KEYS)
    assert not got.ok and got.reason == "missing"


@pytest.mark.parametrize("edit", [
    {"key_id": ""}, {"key_id": "k" * 65}, {"key_id": 1}, {"key_id": None},
    {"digest": VECTORS["digest"].upper()}, {"digest": VECTORS["digest"][:-1]},
    {"digest": VECTORS["digest"] + "\n"}, {"digest": VECTORS["digest"] + "0"},
    {"signature": VECTORS["endorsement"]["signature"] + "\n"},
    {"signature": VECTORS["endorsement"]["signature"][:-2]},
    {"signature": VECTORS["endorsement"]["signature"].replace("+", "-").replace("/", "_")
     if any(c in VECTORS["endorsement"]["signature"] for c in "+/") else "-" * 86 + "=="},
    {"signature": "A" * 88}, {"signature": "A" * 84 + "===="}, {"signature": None},
], ids=["key-empty", "key-long", "key-int", "key-none", "digest-upper", "digest-short", "digest-newline",
        "digest-long", "sig-newline", "sig-unpadded", "sig-urlsafe", "sig-66-bytes", "sig-short", "sig-none"])
def test_malformed_certificate_shapes(edit):
    got = en.verify_endorsement(_with(**edit), KEYS)
    assert not got.ok and got.reason == "malformed"


def test_key_id_of_64_characters_is_well_formed():
    got = en.verify_endorsement(_with(key_id="k" * 64), KEYS)
    assert got.reason == "unknown_key"


@pytest.mark.parametrize("cert", [
    {"key_id": "test-endorse-1", "digest": VECTORS["digest"]},
    dict(VECTORS["endorsement"], extra=1),
    [VECTORS["endorsement"]],
], ids=["missing-field", "extra-field", "list"])
def test_certificate_must_have_exactly_three_fields(cert):
    p = _valid()
    p["endorsement"] = cert
    assert en.verify_endorsement(p, KEYS).reason == "malformed"


@pytest.mark.parametrize("pid", [None, "", 7])
def test_profile_without_a_string_id_is_malformed(pid):
    p = _valid()
    p["id"] = pid
    assert en.verify_endorsement(p, KEYS).reason == "malformed"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 2 ** 60, 1e-9, "\ud800", object()])
def test_profile_outside_the_canonical_contract_is_malformed_and_never_raises(value):
    p = _valid()
    p["limits"] = dict(p["limits"], extra=value)
    assert en.verify_endorsement(p, KEYS).reason == "malformed"


def test_deeply_nested_profile_never_raises():
    p = _valid()
    deep: dict = {}
    cur = deep
    for _ in range(5000):
        cur["x"] = {}
        cur = cur["x"]
    p["deep"] = deep
    assert en.verify_endorsement(p, KEYS).reason == "malformed"


def test_malformed_wins_over_unknown_key():
    assert en.verify_endorsement(_with(key_id="nope", digest="0"), KEYS).reason == "malformed"


def test_unknown_key_wins_over_a_digest_mismatch():
    assert en.verify_endorsement(_with(key_id="nope", digest="0" * 64), KEYS).reason == "unknown_key"


def test_digest_mismatch_wins_over_a_bad_signature():
    sig = VECTORS["endorsement"]["signature"]
    bad = ("B" if sig[0] == "A" else "A") + sig[1:]
    assert en.verify_endorsement(_with(digest="0" * 64, signature=bad), KEYS).reason == "digest_mismatch"


@pytest.mark.parametrize("key", ["", "zz" * 32, "00" * 31, None, 5])
def test_broken_public_key_never_verifies(key):
    got = en.verify_endorsement(_valid(), {"test-endorse-1": key})
    assert not got.ok and got.reason == "bad_signature"


def test_input_profile_is_not_modified():
    p = _valid()
    before = copy.deepcopy(p)
    en.verify_endorsement(p, KEYS)
    en.endorsement_digest(p)
    assert p == before


def test_digest_ignores_cosmetic_fields_only():
    p = _valid()
    base = en.endorsement_digest(p)
    p["label"], p["status_note"], p["sources"] = "other", "other", []
    p["modbus"]["status_note"] = "other"
    p["endorsement"] = {"anything": True}
    assert en.endorsement_digest(p) == base
    p["modbus"]["status"] = "draft" if p["modbus"]["status"] == "verified" else "verified"
    assert en.endorsement_digest(p) != base


# --- bramka prawa zapisu: certyfikat + status ---------------------------------------------------------

def test_write_endorsement_requires_a_verified_status():
    p = _valid()
    assert en.endorsed_for_write(p, KEYS) is True
    p["status"] = "draft"
    assert en.endorsed_for_write(p, KEYS) is False               # digest też się rozjeżdża
    assert en.endorsed_for_write({"id": "x", "status": "verified"}, KEYS) is False
    assert en.endorsed_for_write(None, KEYS) is False


def test_write_endorsement_uses_the_release_keys_by_default(monkeypatch):
    assert en.endorsed_for_write(_valid()) is False
    monkeypatch.setattr(en, "PROFILE_ENDORSEMENT_PUBLIC_KEYS", dict(KEYS))
    assert en.endorsed_for_write(_valid()) is True


def test_const_reexports_the_endorsement_keys():
    from custom_components.volcast import const
    assert const.PROFILE_ENDORSEMENT_PUBLIC_KEYS is en.PROFILE_ENDORSEMENT_PUBLIC_KEYS
