"""Podpisany feed profili marek: kanoniczny JSON, weryfikacja Ed25519, anty-rollback, łączenie z
profilami pakietu i aktualizacja raz na dobę (rdzeń, bez HA).

Klucze testowe: para generowana w procesie (prywatny nigdy nie trafia do repo) i klucz publiczny
fixture `tests/fixtures/profile_feed/feed_ts*.json` podpisanej podpisem TypeScript chmury (parytet)."""
from __future__ import annotations

import asyncio
import base64
import copy
import dataclasses
import json
import logging
from pathlib import Path
from types import MappingProxyType

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from custom_components.volcast.const import PROFILE_FEED_PUBLIC_KEYS
from custom_components.volcast.control import direct_search as ds
from custom_components.volcast.control.verification import start_rung_for
from custom_components.volcast.core import endorsement as en
from custom_components.volcast.core import profile_feed as pf
from custom_components.volcast.core.control.ladder import RUNG_CONTROL_WRITE, RUNG_IDENTIFY
from custom_components.volcast.core.control.select import control_verified
from custom_components.volcast.core import profile as prof_mod
from custom_components.volcast.core.control.select import InverterHint, select_profile
from custom_components.volcast.core.discovery import identify as identify_mod
from custom_components.volcast.core.profile import (
    PROFILES_DIR, ProfileError, builtin_ids, direct_verified, load_builtin, profile_from_dict)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "profile_feed"
TS_BODY = (FIXTURES / "feed_ts.json").read_bytes()
TS_CANONICAL_FEED = (FIXTURES / "feed_ts_canonical.txt").read_bytes()
_TS_KEY = json.loads((FIXTURES / "feed_ts_key.json").read_text("utf-8"))
TS_KEYS = {_TS_KEY["key_id"]: _TS_KEY["public_raw_hex"]}

KEY_ID = "test-key"
_PRIV = Ed25519PrivateKey.generate()
KEYS = {KEY_ID: _PRIV.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()}
_OTHER = Ed25519PrivateKey.generate()

T0 = "2026-10-14T08:00:00.000Z"
T1 = "2026-10-15T08:00:00.000Z"


def _raw(pid: str) -> dict:
    return json.loads((PROFILES_DIR / f"{pid}.json").read_text("utf-8"))


def _acme(pid="acme-hybrid", **edits) -> dict:
    """Nowa marka: kopia sofar-hyd z własną identyfikacją (nie pokrywa się z żadnym profilem pakietu)."""
    raw = _raw("sofar-hyd")
    raw["id"] = pid
    raw["label"] = "Acme Hybrid"
    raw["identify"]["model_regex"] = ["^ACME-H[0-9]"]
    for integ in raw["ha"]["integrations"]:
        integ["domain"], integ["model_regex"] = "acme_power", ["(?i)\\bacme"]
    raw.update(edits)
    return raw


def _feed(profiles=None, *, generated_at=T0, key_id=KEY_ID, schema_version=1) -> dict:
    profiles = [_acme()] if profiles is None else profiles
    return {"schema_version": schema_version, "generated_at": generated_at, "key_id": key_id,
            "profiles": sorted(profiles, key=lambda p: str(p.get("id")))}


def _doc(feed=None, *, key=_PRIV, key_id=KEY_ID, **kw) -> dict:
    feed = _feed(**kw) if feed is None else feed
    sig = base64.b64encode(key.sign(pf.canonical_bytes(feed))).decode()
    return {"feed": feed, "key_id": key_id, "signature": sig}


def _body(feed=None, **kw) -> bytes:
    return pf.canonical_bytes(_doc(feed, **kw))


@pytest.fixture(autouse=True)
def _clean_registry():
    pf.set_feed_profiles({})
    yield
    pf.set_feed_profiles({})


def _reason(body, keys=KEYS, held=None) -> str:
    with pytest.raises(pf.FeedRejected) as err:
        pf.verify_feed_body(body, keys=keys, held=held)
    return err.value.reason


# --- kanoniczny JSON: wektory kontraktu (`profile-feed-contract.md` §3) ---------------------------

@pytest.mark.parametrize("value, expected", [
    ({"b": 1, "a": {"d": [3, {"z": 1, "y": 2}], "c": "x"}, "e": [True, False, None]},
     '{"a":{"c":"x","d":[3,{"y":2,"z":1}]},"b":1,"e":[true,false,null]}'),
    ([0, -0, 7, -12, 0.5, 0.1, 123.456, 0.0001, -0.25, 9007199254740991],
     "[0,0,7,-12,0.5,0.1,123.456,0.0001,-0.25,9007199254740991]"),
    ({"\U0001F600": 1, "｡": 2}, '{"｡":2,"\U0001F600":1}'),
    ('a"b\\c/d', '"a\\"b\\\\c/d"'),
    ("\n\r\t\b\f", '"\\n\\r\\t\\b\\f"'),
    ("\u0001\u001f", '"\\u0001\\u001f"'),
    ("\u007f ", '"\u007f "'),
    ([1.0, -0.0, 2.5e3], "[1,0,2500]"),
])
def test_canonical_vectors(value, expected):
    assert pf.canonical_bytes(value) == expected.encode("utf-8")


@pytest.mark.parametrize("value", [
    float("nan"), float("inf"), -float("inf"), 1e-5, -0.00009, 2 ** 53, -(2 ** 53), 1e16,
    "\ud800", {"k\udc00": 1}, {1: "int key"}, {b"x"}, b"bytes", object(),
])
def test_canonical_rejects_values_outside_the_contract(value):
    with pytest.raises(pf.CanonicalError):
        pf.canonical_bytes(value)


def test_ts_fixture_canonical_bytes_match_python():
    doc = json.loads(TS_BODY.decode("utf-8"))
    assert pf.canonical_bytes(doc["feed"]) == TS_CANONICAL_FEED
    assert pf.canonical_bytes(doc) == TS_BODY            # treść 200 jest już kanoniczna


def test_ts_fixture_signature_verifies_in_python():
    v = pf.verify_feed_body(TS_BODY, keys=TS_KEYS, held=None)
    assert sorted(v.profiles) == ["acme-hybrid"]                  # id z pakietu pominięte
    assert v.ignored == ("goodwe-et", "sofar-hyd")
    assert v.generated_at.isoformat() == "2026-10-14T08:00:00+00:00"
    assert v.etag.startswith('"') and len(v.etag) == 66


def test_ts_fixture_rejected_with_the_wrong_public_key():
    assert _reason(TS_BODY, keys={_TS_KEY["key_id"]: KEYS[KEY_ID]}) == "signature"


# --- weryfikacja: każdy błąd = odrzucenie całego feedu --------------------------------------------

def test_valid_feed_verifies():
    v = pf.verify_feed_body(_body(), keys=KEYS, held=None)
    assert list(v.profiles) == ["acme-hybrid"] and v.key_id == KEY_ID


def test_bad_signature_rejected():
    doc = _doc()
    doc["feed"]["profiles"][0]["label"] = "Tampered"
    assert _reason(pf.canonical_bytes(doc)) == "signature"


def test_signature_from_another_key_rejected():
    assert _reason(_body(key=_OTHER)) == "signature"


def test_unknown_key_id_rejected():
    assert _reason(_body(key_id="other-key", feed=_feed(key_id="other-key"))) == "unknown_key"


# --- zestaw kluczy wbudowanych: poprzedni, produkcyjny, stagingowy --------------------------------
# Prywatnych kluczy wbudowanych nie ma w repo: każdy identyfikator dostaje parę testową (stand-in).

SHIPPED_IDS = ("feed-2026-10", "feed-prod-2026-11", "feed-staging-2026-11")
_STANDIN_PAIRS = {kid: Ed25519PrivateKey.generate() for kid in SHIPPED_IDS}
_STANDIN_KEYS = {kid: k.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
                 for kid, k in _STANDIN_PAIRS.items()}


@pytest.mark.parametrize("key_id", SHIPPED_IDS)
def test_feed_signed_by_each_shipped_key_id_verifies(key_id):
    body = pf.canonical_bytes(_doc(_feed(key_id=key_id), key=_STANDIN_PAIRS[key_id], key_id=key_id))
    v = pf.verify_feed_body(body, keys=_STANDIN_KEYS, held=None)
    assert v.key_id == key_id


@pytest.mark.parametrize("key_id", SHIPPED_IDS)
def test_shipped_key_id_with_a_foreign_signature_rejected(key_id):
    body = pf.canonical_bytes(_doc(_feed(key_id=key_id), key=_OTHER, key_id=key_id))
    assert _reason(body, keys=_STANDIN_KEYS) == "signature"


def test_feed_signed_by_a_key_outside_the_shipped_set_rejected():
    body = pf.canonical_bytes(_doc(_feed(key_id="feed-prod-2026-11"), key=_OTHER,
                                   key_id="feed-prod-2026-11"))
    assert _reason(body, keys=PROFILE_FEED_PUBLIC_KEYS) == "signature"
    unknown = pf.canonical_bytes(_doc(_feed(key_id="feed-dev-2026-11"), key=_STANDIN_PAIRS["feed-2026-10"],
                                      key_id="feed-dev-2026-11"))
    assert _reason(unknown, keys=PROFILE_FEED_PUBLIC_KEYS) == "unknown_key"


def test_outer_key_id_must_equal_feed_key_id():
    doc = _doc(feed=_feed(key_id="other-key"))
    assert _reason(pf.canonical_bytes(doc), keys={**KEYS, "other-key": KEYS[KEY_ID]}) == "key_mismatch"


@pytest.mark.parametrize("sig", ["", "not base64!", base64.b64encode(b"x" * 63).decode(), 5])
def test_malformed_signature_rejected(sig):
    doc = _doc()
    doc["signature"] = sig
    assert _reason(pf.canonical_bytes(doc)) == "signature"


@pytest.mark.parametrize("version", [2, 0, True, "1", 1.5])
def test_schema_version_other_than_1_rejected(version):
    assert _reason(_body(schema_version=version)) == "schema_version"


def test_older_generated_at_rejected():
    held = pf.verify_feed_body(_body(generated_at=T1), keys=KEYS, held=None)
    assert _reason(_body(generated_at=T0), held=held) == "rollback"


def test_equal_generated_at_with_a_different_signature_rejected():
    held = pf.verify_feed_body(_body(generated_at=T0), keys=KEYS, held=None)
    other = _body(feed=_feed([_acme(label="Acme Hybrid 2")], generated_at=T0))
    assert _reason(other, held=held) == "replay"


def test_equal_generated_at_with_the_same_signature_is_the_same_feed():
    body = _body(generated_at=T0)
    held = pf.verify_feed_body(body, keys=KEYS, held=None)
    again = pf.verify_feed_body(body, keys=KEYS, held=held)
    assert again.signature == held.signature


def test_newer_generated_at_accepted():
    held = pf.verify_feed_body(_body(generated_at=T0), keys=KEYS, held=None)
    assert pf.verify_feed_body(_body(generated_at=T1), keys=KEYS, held=held).generated_at > held.generated_at


@pytest.mark.parametrize("value", ["yesterday", "2026-10-14", "2026-10-14T08:00:00", 1760428800, None])
def test_generated_at_must_be_utc_iso(value):
    assert _reason(_body(generated_at=value)) == "generated_at"


def test_oversize_body_rejected_before_parsing():
    assert _reason(b" " * (pf.FEED_MAX_BYTES + 1)) == "too_large"


@pytest.mark.parametrize("body", [
    b"{", b"[]", b"null", b"\xff\xfe{}", b'{"feed":{},"feed":{},"key_id":"k","signature":"s"}',
    b'{"feed": NaN, "key_id": "test-key", "signature": "x"}', b'{"key_id":"test-key"}',
    b"[" * 100_000,
], ids=["truncated", "array", "null", "not-utf8", "duplicate-key", "nan", "missing-fields", "deep"])
def test_malformed_body_rejected(body):
    assert _reason(body) == "malformed"


def _verify(*profiles):
    return pf.verify_feed_body(_body(feed=_feed(list(profiles))), keys=KEYS, held=None)


def test_profile_with_a_schema_error_is_skipped_and_the_rest_accepted():
    bad = _acme("broken-profile")
    del bad["write_policy"]
    v = _verify(_acme(), bad)
    assert list(v.profiles) == ["acme-hybrid"]
    assert v.skipped == (("broken-profile", "schema"),)


@pytest.mark.parametrize("edit", [
    lambda p: p.update(future_field={"x": 1}),                       # nieznane pole na szczycie
    lambda p: p["write_policy"].update(power_by_direction=True),     # nieznane pole w sekcji
    lambda p: p["read"].update(future_reading={"addr": 1, "type": "u16"}),
], ids=["top-level", "nested", "unknown-read-key"])
def test_profile_with_an_unknown_field_is_skipped_and_the_rest_accepted(edit):
    future = _acme("future-hybrid")
    edit(future)
    v = _verify(_acme(), future)
    assert list(v.profiles) == ["acme-hybrid"]
    assert v.skipped == (("future-hybrid", "schema"),)


def test_profile_requiring_a_newer_engine_is_skipped_and_the_rest_accepted():
    v = _verify(_acme(), _acme("next-hybrid", min_engine=pf.PROFILE_ENGINE + 1))
    assert list(v.profiles) == ["acme-hybrid"]
    assert v.skipped == (("next-hybrid", "engine"),)


def test_newer_engine_wins_over_a_schema_error_in_the_skip_code():
    nxt = _acme("next-hybrid", min_engine=3, future_field=True)
    assert _verify(_acme(), nxt).skipped == (("next-hybrid", "engine"),)


@pytest.mark.parametrize("engine", [1, 2])
def test_profile_within_the_engine_version_is_accepted(engine):
    v = _verify(_acme(min_engine=engine))
    assert list(v.profiles) == ["acme-hybrid"] and v.skipped == ()
    assert v.profiles["acme-hybrid"]["min_engine"] == engine


@pytest.mark.parametrize("engine", [0, -1, True, "3", 2.5, None])
def test_malformed_min_engine_is_a_schema_skip(engine):
    v = _verify(_acme(), _acme("odd-hybrid", min_engine=engine))
    assert list(v.profiles) == ["acme-hybrid"]
    assert v.skipped == (("odd-hybrid", "schema"),)


def test_engine_version_constant():
    from custom_components.volcast import const
    assert pf.PROFILE_ENGINE == const.PROFILE_ENGINE == 2


def test_every_profile_skipped_still_accepts_an_empty_feed():
    bad = _acme()
    del bad["limits"]
    v = _verify(bad)
    assert dict(v.profiles) == {} and v.skipped == (("acme-hybrid", "schema"),)


def test_skipped_profiles_never_reach_the_registry():
    bad = _acme("broken-profile", future_field=1)
    pf.set_feed_profiles(_verify(_acme(), bad).profiles)
    assert pf.feed_ids() == frozenset({"acme-hybrid"})


@pytest.mark.parametrize("edit", [
    lambda p: p.update(future_field=1),
    lambda p: p.update(min_engine=9),
    lambda p: p.pop("limits"),
], ids=["unknown-field", "newer-engine", "schema-error"])
def test_unsafe_regex_rejects_the_whole_feed_even_in_a_skippable_profile(edit):
    raw = _acme("future-hybrid")
    edit(raw)
    raw["ha"]["integrations"][0]["model_regex"] = ["^(a+)+$"]
    assert _reason(_body(feed=_feed([_acme(), raw]))) == "profile_invalid"


def test_unsafe_regex_in_an_unknown_field_rejects_the_whole_feed():
    raw = _acme("future-hybrid", future={"label_regex": "^(.*)*x"})
    assert _reason(_body(feed=_feed([_acme(), raw]))) == "profile_invalid"


def test_overbroad_identify_rejects_the_whole_feed_next_to_a_good_profile():
    raw = _acme("broad-hybrid", future_field=1)
    raw["identify"]["model_regex"] = [".*"]
    assert _reason(_body(feed=_feed([_acme(), raw]))) == "profile_invalid"


def test_duplicate_id_rejects_the_feed_even_when_one_copy_is_skippable():
    assert _reason(_body(feed=_feed([_acme(), _acme(future_field=1)]))) == "profile_invalid"


@pytest.mark.parametrize("entry", [5, "acme-hybrid", None, ["x"]])
def test_non_object_entry_still_rejects_the_whole_feed(entry):
    feed = _feed()
    feed["profiles"] = [_acme(), entry]
    assert _reason(_body(feed=feed)) == "profile_invalid"


@pytest.mark.parametrize("pid", ["Acme-Hybrid", "acme_hybrid", "../etc", "", "acme--x", "acme\n", "acme-x\n"])
def test_profile_id_must_be_kebab_case(pid):
    assert _reason(_body(feed=_feed([_acme(pid)]))) == "profile_invalid"


def test_duplicate_profile_ids_reject_the_feed():
    assert _reason(_body(feed=_feed([_acme(), _acme(label="again")]))) == "profile_invalid"


@pytest.mark.parametrize("regex", ["^.", ".*", "(?i)acme", "^(", "^.+"])
def test_overbroad_or_broken_identify_regex_rejects_the_feed(regex):
    raw = _acme()
    raw["identify"]["model_regex"] = [regex]
    assert _reason(_body(feed=_feed([raw]))) == "profile_invalid"


@pytest.mark.parametrize("identify", [
    ["^ACME"], {"model_regex": "^ACME"}, {"model_regex": [5]},
    {"model_regex": ["^ACME"], "registers": {"device_type": {"addr": 1, "expect": 5}}},
], ids=["not-object", "regex-not-list", "regex-not-string", "expect-not-list"])
def test_malformed_identify_shape_rejects_the_whole_feed(identify):
    # Zachowawczo: kształt identyfikacji sprawdzany przed schematem (jak reguła bram chmury).
    raw = _acme("odd-hybrid")
    raw["identify"] = identify
    assert _reason(_body(feed=_feed([_acme(), raw]))) == "profile_invalid"


def test_profiles_must_be_a_list_of_objects():
    feed = _feed()
    feed["profiles"] = {"acme-hybrid": _acme()}
    assert _reason(_body(feed=feed)) == "profile_invalid"


def test_feed_profile_statuses_forced_to_draft_without_a_certificate():
    raw = _acme(status="verified")
    raw["modbus"]["status"] = "verified"
    for integ in raw["ha"]["integrations"]:
        integ["status"] = "verified"
    v = pf.verify_feed_body(_body(feed=_feed([raw])), keys=KEYS, held=None)
    got = v.profiles["acme-hybrid"]
    assert got["status"] == "draft" and got["modbus"]["status"] == "draft"
    assert {i["status"] for i in got["ha"]["integrations"]} == {"draft"}


# --- łączenie z pakietem (`profile.py`) ----------------------------------------------------------

def _activate(*profiles):
    v = pf.verify_feed_body(_body(feed=_feed(list(profiles))), keys=KEYS, held=None)
    pf.set_feed_profiles(v.profiles)
    return v


def test_new_feed_draft_visible_in_builtin_ids_and_loadable():
    before = builtin_ids()
    _activate(_acme())
    assert "acme-hybrid" in builtin_ids() and set(before) < set(builtin_ids())
    p = load_builtin("acme-hybrid")
    assert p.id == "acme-hybrid" and p.status == "draft"
    assert "acme-hybrid" in {p.id for p in ds.load_profiles()}


def test_packaged_verified_profile_never_overridden():
    raw = _raw("goodwe-et")
    raw["label"] = "GoodWe ET (feed)"
    raw["status"] = "draft"
    _activate(raw)
    p = load_builtin("goodwe-et")
    assert p.raw["label"] == _raw("goodwe-et")["label"] and direct_verified(p)


@pytest.mark.parametrize("pid", ["sofar-hyd", "goodwe-et"])
def test_feed_entry_with_a_packaged_id_is_ignored_and_the_rest_accepted(pid):
    raw = _raw(pid)
    raw["label"] = "From the feed"
    v = _activate(raw, _acme())
    assert v.ignored == (pid,) and list(v.profiles) == ["acme-hybrid"]
    p = load_builtin(pid)
    assert p.raw["label"] == _raw(pid)["label"] and p.origin == "package"
    assert load_builtin("acme-hybrid").origin == "feed"


def test_ts_fixture_merge_rules():
    pf.set_feed_profiles(pf.verify_feed_body(TS_BODY, keys=TS_KEYS, held=None).profiles)
    assert load_builtin("goodwe-et").raw["label"] == _raw("goodwe-et")["label"]
    assert load_builtin("sofar-hyd").raw["label"] == _raw("sofar-hyd")["label"]
    assert load_builtin("acme-hybrid").status == "draft"


def test_feed_profile_claiming_verified_without_a_certificate_never_enables_control():
    raw = _acme(status="verified")
    raw["modbus"]["status"] = "verified"
    for integ in raw["ha"]["integrations"]:
        integ["status"] = "verified"
    _activate(raw)
    p = load_builtin("acme-hybrid")
    assert p.status == "draft" and p.modbus.status == "draft"
    assert not direct_verified(p)
    assert not any(control_verified(p, i["domain"]) for i in raw["ha"]["integrations"])
    # Drabina weryfikacji startuje od identyfikacji (jak każdy draft), nigdy od zapisu kontrolnego.
    assert start_rung_for(p, None, direct=True) == RUNG_IDENTIFY != RUNG_CONTROL_WRITE
    assert start_rung_for(p, raw["ha"]["integrations"][0]["domain"], direct=False) == RUNG_IDENTIFY
    assert ds.label(_probe("acme-hybrid"), [p]).endswith("test only")


def _probe(pid):
    class _Ident:
        profile_id = pid
        model = "X1"

    class _Report:
        identity = _Ident()
    return _Report()


def test_unknown_and_unsafe_ids_still_rejected_with_feed_active():
    _activate(_acme())
    for pid in ("nope-nope", "../goodwe-et", "ACME-HYBRID"):
        with pytest.raises(ProfileError):
            load_builtin(pid)


def test_profiles_dir_env_still_ignored_with_feed_active(monkeypatch, tmp_path):
    _activate(_acme())
    (tmp_path / "zz-env.json").write_text(json.dumps(_acme("zz-env")))
    monkeypatch.setenv("VOLCAST_PROFILES_DIR", str(tmp_path))
    assert "zz-env" not in builtin_ids()


def test_registry_holds_copies():
    v = _activate(_acme())
    v.profiles["acme-hybrid"]["label"] = "changed after activation"
    assert load_builtin("acme-hybrid").raw["label"] == "Acme Hybrid"


# --- certyfikat zapisu: jedyna droga do prawa zapisu profilu z feedu ---------------------------------

_VECTORS = json.loads((FIXTURES / "endorsement-vectors.json").read_text("utf-8"))
_EKEY, _FKEY = _VECTORS["test_keys"][0], _VECTORS["test_keys"][1]
_EPRIV = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(_EKEY["seed_hex"]))
_FPRIV = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(_FKEY["seed_hex"]))


@pytest.fixture
def endorse_keys(monkeypatch):
    """Klucz certyfikatów wydania podmieniony na testowy (prywatny testowy jest w wektorach)."""
    monkeypatch.setattr(en, "PROFILE_ENDORSEMENT_PUBLIC_KEYS",
                        MappingProxyType({_EKEY["key_id"]: _EKEY["public_hex"]}))


def _endorse(raw: dict, *, key=_EPRIV, key_id=_EKEY["key_id"]) -> dict:
    out = copy.deepcopy(raw)
    out.pop("endorsement", None)
    digest = en.endorsement_digest(out)
    sig = base64.b64encode(key.sign(en.endorsement_message(out["id"], digest, key_id))).decode()
    out["endorsement"] = {"key_id": key_id, "digest": digest, "signature": sig}
    return out


def _all_verified(raw: dict) -> dict:
    raw = copy.deepcopy(raw)
    raw["status"] = raw["modbus"]["status"] = "verified"
    for integ in raw["ha"]["integrations"]:
        integ["status"] = "verified"
    return raw


def _statuses(raw) -> tuple:
    return (raw["status"], raw["modbus"]["status"], tuple(i["status"] for i in raw["ha"]["integrations"]))


def test_endorsed_verified_feed_profile_keeps_its_statuses(endorse_keys):
    signed = _endorse(_all_verified(_acme()))
    v = pf.verify_feed_body(_body(feed=_feed([signed])), keys=KEYS, held=None)
    got = v.profiles["acme-hybrid"]
    assert _statuses(got) == ("verified", "verified", ("verified",) * len(got["ha"]["integrations"]))
    assert got == signed and v.degraded == () and v.skipped == ()


def test_endorsed_feed_profile_loads_with_write_rights(endorse_keys):
    raw = _endorse(_all_verified(_acme()))
    _activate(raw)
    p = load_builtin("acme-hybrid")
    assert p.origin == prof_mod.ORIGIN_FEED_ENDORSED == "feed_endorsed"
    assert prof_mod.writes_allowed(p) and direct_verified(p) and not prof_mod.is_packaged(p)
    assert all(control_verified(p, i["domain"]) for i in raw["ha"]["integrations"])
    assert start_rung_for(p, None, direct=True) == RUNG_CONTROL_WRITE
    assert "acme-hybrid" in {q.id for q in ds.load_profiles() if prof_mod.writes_allowed(q)}


def test_cosmetic_change_after_signing_keeps_the_write_right(endorse_keys):
    signed = _endorse(_all_verified(_acme()))
    _activate(signed)
    base = load_builtin("acme-hybrid")
    raw = copy.deepcopy(signed)
    raw["label"], raw["status_note"] = "Acme Hybrid (renamed)", "new note"
    raw["sources"] = [{"what": "other", "ref": "https://example.invalid/other"}]
    raw["modbus"]["status_note"] = "new modbus note"
    pf.set_feed_profiles({})
    _activate(raw)
    p = load_builtin("acme-hybrid")
    assert p.origin == prof_mod.ORIGIN_FEED_ENDORSED and direct_verified(p)
    assert p.fingerprint == base.fingerprint and p.raw["label"] == "Acme Hybrid (renamed)"


@pytest.mark.parametrize("edit", [
    lambda r: r["write_policy"].update(min_interval_s=float(r["write_policy"]["min_interval_s"]) + 1),
    lambda r: r.update(unit_id=(r["unit_id"] % 200) + 1),
    lambda r: r["ha"]["integrations"][0].update(model_regex=["(?i)\\bacme2"]),
    lambda r: r.update(min_engine=1),
], ids=["write-policy", "unit-id", "ha-regex", "min-engine"])
def test_behaviour_change_after_signing_degrades_to_a_read_only_draft(endorse_keys, edit):
    signed = _endorse(_all_verified(_acme()))
    endorsed_fp = profile_from_dict(signed).fingerprint
    raw = copy.deepcopy(signed)
    edit(raw)
    v = _activate(raw)
    assert _statuses(v.profiles["acme-hybrid"])[0:2] == ("draft", "draft")
    assert v.degraded == (("acme-hybrid", "digest_mismatch"),) and v.skipped == ()
    p = load_builtin("acme-hybrid")
    assert p.origin == prof_mod.ORIGIN_FEED and not prof_mod.writes_allowed(p) and not direct_verified(p)
    assert p.fingerprint != endorsed_fp                     # drabina związana z innym odciskiem → od nowa
    assert start_rung_for(p, None, direct=True) == RUNG_IDENTIFY


def test_draft_profile_with_a_valid_certificate_stays_a_draft(endorse_keys):
    raw = _all_verified(_acme())
    raw["status"] = "draft"
    v = _activate(_endorse(raw))
    assert _statuses(v.profiles["acme-hybrid"])[0:2] == ("draft", "draft")
    assert v.degraded == (("acme-hybrid", "not_verified"),)
    p = load_builtin("acme-hybrid")
    assert p.origin == prof_mod.ORIGIN_FEED and not prof_mod.writes_allowed(p)


@pytest.mark.parametrize("key, key_id, reason", [
    (_FPRIV, _FKEY["key_id"], "unknown_key"),
    (_FPRIV, _EKEY["key_id"], "bad_signature"),
], ids=["unknown-key", "foreign-key-known-id"])
def test_certificate_from_a_foreign_key_degrades_to_a_draft(endorse_keys, key, key_id, reason):
    v = _activate(_endorse(_all_verified(_acme()), key=key, key_id=key_id))
    assert v.profiles["acme-hybrid"]["status"] == "draft" and v.degraded == (("acme-hybrid", reason),)
    assert load_builtin("acme-hybrid").origin == prof_mod.ORIGIN_FEED


def test_test_signed_certificate_is_unknown_to_the_release_keys():
    v = _activate(_endorse(_all_verified(_acme())))
    assert v.degraded == (("acme-hybrid", "unknown_key"),)
    assert not prof_mod.writes_allowed(load_builtin("acme-hybrid"))


@pytest.mark.parametrize("cert", ["x", 5, None, [], {}, {"key_id": "k", "digest": "0" * 64, "signature": "x"}],
                         ids=["string", "int", "null", "list", "empty", "bad-signature-shape"])
def test_invalid_certificate_never_rejects_or_skips_the_profile(endorse_keys, cert):
    raw = _all_verified(_acme())
    raw["endorsement"] = cert
    v = _activate(raw, _acme("acme-second"))
    assert set(v.profiles) == {"acme-hybrid", "acme-second"} and v.skipped == ()
    assert v.profiles["acme-hybrid"]["status"] == "draft"
    assert v.degraded == (("acme-hybrid", "malformed"),)
    assert not prof_mod.writes_allowed(load_builtin("acme-hybrid"))


def test_profile_without_a_certificate_is_not_reported_as_degraded(endorse_keys):
    v = _activate(_all_verified(_acme()))
    assert v.degraded == () and v.profiles["acme-hybrid"]["status"] == "draft"


def test_degraded_certificate_logged_with_id_and_code_only(endorse_keys, caplog):
    raw = _endorse(_all_verified(_acme(label="SECRET-LABEL")))
    raw["limits"]["battery_temp_c"]["max"] = raw["limits"]["battery_temp_c"]["max"] - 1
    with caplog.at_level(logging.WARNING):
        assert _run(_updater(MemStore()).async_check(Fetch(_ok(_body(feed=_feed([raw])))))) == "updated"
    assert "acme-hybrid: digest_mismatch" in caplog.text and "SECRET-LABEL" not in caplog.text


def test_registry_never_yields_a_verified_profile_without_a_valid_certificate(endorse_keys, monkeypatch):
    pf.set_feed_profiles({"acme-hybrid": _all_verified(_acme())})
    with pytest.raises(ProfileError):
        load_builtin("acme-hybrid")
    pf.set_feed_profiles({"acme-hybrid": _endorse(_all_verified(_acme()))})
    assert load_builtin("acme-hybrid").origin == prof_mod.ORIGIN_FEED_ENDORSED
    # Certyfikat sprawdzany przy KAŻDYM wczytaniu: klucz wycofany w wydaniu = koniec prawa zapisu.
    monkeypatch.setattr(en, "PROFILE_ENDORSEMENT_PUBLIC_KEYS", MappingProxyType({}))
    with pytest.raises(ProfileError):
        load_builtin("acme-hybrid")


def test_registry_draft_with_a_certificate_loads_read_only(endorse_keys):
    raw = _all_verified(_acme())
    raw["status"] = "draft"
    pf.set_feed_profiles({"acme-hybrid": _endorse(raw)})
    with pytest.raises(ProfileError):                     # modbus `verified` w drafcie bez prawa zapisu
        load_builtin("acme-hybrid")
    pf.set_feed_profiles({"acme-hybrid": pf._as_draft(_endorse(raw))})
    assert load_builtin("acme-hybrid").origin == prof_mod.ORIGIN_FEED


@pytest.mark.parametrize("pid", ["sofar-hyd", "goodwe-et"])
def test_endorsed_entry_with_a_packaged_id_is_still_ignored(endorse_keys, pid):
    raw = _all_verified(_raw(pid))
    raw["label"] = "From the feed"
    v = _activate(_endorse(raw), _acme())
    assert v.ignored == (pid,) and list(v.profiles) == ["acme-hybrid"]
    p = load_builtin(pid)
    assert p.origin == prof_mod.ORIGIN_PACKAGE and p.raw["label"] == _raw(pid)["label"]


def test_endorsed_entry_overlapping_a_packaged_profile_is_still_ignored(endorse_keys):
    v = _activate(_endorse(_all_verified(_copy_as("goodwe-et", "goodwe-et-g2"))), _acme())
    assert v.overlapping == ("goodwe-et-g2",) and list(v.profiles) == ["acme-hybrid"]


def _endorsed_profile(raw: dict):
    """Profil certyfikowany (pochodzenie nadawane wyłącznie przez `load_builtin` z rejestru)."""
    pf.set_feed_profiles({raw["id"]: _endorse(_all_verified(raw))})
    p = load_builtin(raw["id"])
    assert p.origin == prof_mod.ORIGIN_FEED_ENDORSED
    return p


@pytest.mark.parametrize("model_rx, ha_rx", [
    ("^GW10K", "(?i)goodwe gw10k"), ("^GW", "(?i)goodwe"),
    ("^GW[0-9.]+K[A-Z]*-(ET|EH|BT|BH)", "(?i)goodwe gw"),
], ids=["series", "superset", "same-regex"])
def test_packaged_profile_beats_an_endorsed_feed_profile(endorse_keys, model_rx, ha_rx):
    raw = _copy_as("goodwe-et", "goodwe-gw10k")
    raw["identify"]["model_regex"] = [model_rx]
    for integ in raw["ha"]["integrations"]:
        integ["model_regex"] = [ha_rx]
    feed = _endorsed_profile(raw)
    hint = InverterHint("goodwe", "GoodWe", "GW10K-ET")
    choice = select_profile([hint], [load_builtin("goodwe-et"), feed])
    assert choice.profile.id == "goodwe-et" and choice.integration_domain == "goodwe"


def test_packaged_draft_beats_an_endorsed_verified_feed_profile(endorse_keys):
    package = load_builtin("sofar-hyd")
    assert package.status == "draft" and package.origin == prof_mod.ORIGIN_PACKAGE
    # Ta sama marka w id, te same wzorce: tekst urządzenia nie rozstrzyga, decyduje pierwszeństwo pakietu.
    feed = _endorsed_profile(_copy_as("sofar-hyd", "sofar-hyd-g2"))
    hint = InverterHint("solarman", "SOFAR", "HYD 6000-EP")
    for order in ([package, feed], [feed, package]):
        assert select_profile([hint], order).profile.id == "sofar-hyd"


def test_endorsed_feed_profile_selected_when_no_packaged_profile_matches(endorse_keys):
    feed = _endorsed_profile(_acme())
    domain = feed.raw["ha"]["integrations"][0]["domain"]
    choice = select_profile([InverterHint(domain, "Acme", "ACME-H5")], [load_builtin("goodwe-et"), feed])
    assert choice.profile.id == "acme-hybrid" and choice.integration_domain == domain


def test_probe_order_keeps_endorsed_feed_profiles_after_the_package(endorse_keys):
    feed = _endorsed_profile(_acme("aaa-first"))
    profiles = [feed, load_builtin("deye-sg"), load_builtin("goodwe-et")]
    assert [p.id for p in identify_mod.probe_order(profiles)] == ["goodwe-et", "deye-sg", "aaa-first"]


def test_only_the_registry_certificate_grants_the_endorsed_origin():
    # Domyślne pochodzenie `profile_from_dict` = feed; dla certyfikowanego treść nie wystarcza.
    p = profile_from_dict(_endorse(_all_verified(_acme())))
    assert p.origin == prof_mod.ORIGIN_FEED and not prof_mod.writes_allowed(p)
    assert prof_mod.writes_allowed(dataclasses.replace(p, origin=prof_mod.ORIGIN_FEED_ENDORSED))
    assert not prof_mod.writes_allowed(dataclasses.replace(p, origin="FEED_ENDORSED"))


# --- aktualizacja: Store, raz na dobę, ETag, zachowanie poprzedniego -----------------------------

class MemStore:
    def __init__(self, data=None, *, fail_save=False):
        self.data = data
        self.fail_save = fail_save
        self.loads = 0
        self.saves = 0

    async def async_load(self):
        self.loads += 1
        return copy.deepcopy(self.data)

    async def async_save(self, data):
        if self.fail_save:
            raise OSError("disk")
        self.saves += 1
        self.data = json.loads(json.dumps(data))


class Fetch:
    def __init__(self, *results):
        self.results = list(results)
        self.etags: list = []

    async def __call__(self, etag):
        self.etags.append(etag)
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


def _ok(body):
    return pf.FeedFetch("ok", body)


class Clock:
    def __init__(self, t=1_792_454_400.0):                       # 2026-10-20 UTC
        self.t = t

    def __call__(self):
        return self.t


def _updater(store, *, keys=KEYS, clock=None, on_new=None):
    return pf.ProfileFeedUpdater(store, keys=keys, clock=clock or Clock(),
                                 on_new_profiles=on_new)


def _run(coro):
    return asyncio.run(coro)


def test_inert_without_public_keys():
    store = MemStore({"document": _doc(), "checked_at": 0})
    fetch = Fetch(_ok(_body()))
    clock = Clock()
    upd = _updater(store, keys={}, clock=clock)
    _run(upd.async_load())
    clock.t += 10 * 86400
    assert store.loads == 0 and not upd.due()
    assert _run(upd.async_check(fetch)) == "inert"
    assert fetch.etags == [] and "acme-hybrid" not in builtin_ids()


def test_first_check_five_minutes_after_start_then_daily():
    clock = Clock()
    upd = _updater(MemStore(), clock=clock)
    _run(upd.async_load())
    assert not upd.due()
    clock.t += pf.FEED_FIRST_CHECK_DELAY_S
    assert upd.due()
    assert _run(upd.async_check(Fetch(_ok(_body())))) == "updated"
    clock.t += pf.FEED_CHECK_INTERVAL_S - 1
    assert not upd.due()
    clock.t += 1
    assert upd.due()


def test_failed_check_also_waits_a_day():
    clock = Clock()
    upd = _updater(MemStore(), clock=clock)
    _run(upd.async_load())
    clock.t += pf.FEED_FIRST_CHECK_DELAY_S
    assert _run(upd.async_check(Fetch(pf.FeedFetch("error")))) == "error"
    assert not upd.due()


def test_accepted_feed_stored_and_applied():
    store = MemStore()
    upd = _updater(store)
    assert _run(upd.async_check(Fetch(_ok(_body())))) == "updated"
    assert store.saves == 1 and store.data["document"] == _doc_from(_body())
    assert store.data["etag"] == upd.held.etag
    assert "acme-hybrid" in builtin_ids()


def _doc_from(body):
    return json.loads(body.decode("utf-8"))


@pytest.mark.parametrize("bad", [
    _body(key=_OTHER), _body(schema_version=2), b"{", b" " * (pf.FEED_MAX_BYTES + 1),
], ids=["signature", "schema-2", "malformed", "oversize"])
def test_store_written_only_after_verification(bad):
    store = MemStore()
    upd = _updater(store)
    assert _run(upd.async_check(Fetch(_ok(bad)))) == "rejected"
    assert store.saves == 0 and store.data is None
    assert "acme-hybrid" not in builtin_ids()


def test_rejected_feed_keeps_the_previous_one(caplog):
    store = MemStore()
    upd = _updater(store)
    _run(upd.async_check(Fetch(_ok(_body(generated_at=T1)))))
    saved = copy.deepcopy(store.data)
    tampered = _doc(feed=_feed([_acme(label="SECRET-LABEL")], generated_at="2026-10-16T08:00:00.000Z"))
    tampered["feed"]["profiles"][0]["label"] = "SECRET-LABEL-2"
    for body in (pf.canonical_bytes(tampered), _body(generated_at=T0),
                 _body(feed=_feed([_acme(label="other")], generated_at=T1))):
        with caplog.at_level(logging.WARNING):
            assert _run(upd.async_check(Fetch(_ok(body)))) == "rejected"
        assert store.data == saved and store.saves == 1
        assert load_builtin("acme-hybrid").raw["label"] == "Acme Hybrid"
    assert "SECRET-LABEL" not in caplog.text and "rejected" in caplog.text


@pytest.mark.parametrize("result", [pf.FeedFetch("error"), pf.FeedFetch("ok", None)])
def test_network_error_keeps_the_previous_feed(result):
    store = MemStore()
    upd = _updater(store)
    _run(upd.async_check(Fetch(_ok(_body()))))
    assert _run(upd.async_check(Fetch(result))) == "error"
    assert store.saves == 1 and "acme-hybrid" in builtin_ids()


def test_not_modified_sends_stored_etag_and_keeps_feed():
    store = MemStore()
    upd = _updater(store)
    fetch = Fetch(_ok(_body()), pf.FeedFetch("not_modified"))
    _run(upd.async_check(fetch))
    assert _run(upd.async_check(fetch)) == "unchanged"
    assert fetch.etags == [None, upd.held.etag] and "acme-hybrid" in builtin_ids()


def test_same_feed_again_is_unchanged_without_notification():
    seen = []
    upd = _updater(MemStore(), on_new=lambda ids, profiles: seen.append(set(ids)))
    body = _body()
    _run(upd.async_check(Fetch(_ok(body))))
    assert _run(upd.async_check(Fetch(_ok(body)))) == "unchanged"
    assert seen == [{"acme-hybrid"}]


def test_stored_feed_reverified_and_applied_on_load():
    store = MemStore()
    _run(_updater(store).async_check(Fetch(_ok(_body()))))
    pf.set_feed_profiles({})
    seen = []
    upd = _updater(store, on_new=lambda ids, profiles: seen.append(ids))
    _run(upd.async_load())
    assert "acme-hybrid" in builtin_ids() and seen == []      # start z magazynu: bez Naprawy
    assert upd.held is not None


@pytest.mark.parametrize("edit", ["label", "signature", "key"])
def test_tampered_or_unknown_stored_feed_not_applied(edit):
    store = MemStore()
    _run(_updater(store).async_check(Fetch(_ok(_body()))))
    pf.set_feed_profiles({})
    keys = KEYS
    if edit == "label":
        store.data["document"]["feed"]["profiles"][0]["label"] = "x"
    elif edit == "signature":
        store.data["document"]["signature"] = base64.b64encode(b"\0" * 64).decode()
    else:
        keys = {"rotated-key": KEYS[KEY_ID]}                      # klucz wycofany w nowym wydaniu
    upd = _updater(store, keys=keys)
    _run(upd.async_load())
    assert "acme-hybrid" not in builtin_ids() and upd.held is None


def test_restart_respects_the_daily_limit():
    clock = Clock()
    store = MemStore()
    _run(_updater(store, clock=clock).async_check(Fetch(_ok(_body()))))
    clock.t += 3600                                               # restart godzinę później
    upd = _updater(store, clock=clock)
    _run(upd.async_load())
    clock.t += pf.FEED_FIRST_CHECK_DELAY_S
    assert not upd.due()
    clock.t += pf.FEED_CHECK_INTERVAL_S
    assert upd.due()


def test_new_profiles_reported_only_for_new_non_packaged_ids():
    seen = []
    upd = _updater(MemStore(), on_new=lambda ids, profiles: seen.append(sorted(ids)))
    sofar = _raw("sofar-hyd")
    _run(upd.async_check(Fetch(_ok(_body(feed=_feed([_acme(), sofar], generated_at=T0))))))
    _run(upd.async_check(Fetch(_ok(_body(feed=_feed([_acme(), _acme("acme-mini"), sofar],
                                                     generated_at=T1))))))
    assert seen == [["acme-hybrid"], ["acme-mini"]]


def test_rejected_feed_never_reports_new_profiles():
    seen = []
    upd = _updater(MemStore(), on_new=lambda ids, profiles: seen.append(ids))
    _run(upd.async_check(Fetch(_ok(_body(key=_OTHER)))))
    assert seen == []


def test_notification_error_does_not_undo_the_update():
    def boom(ids, profiles):
        raise RuntimeError("x")
    upd = _updater(MemStore(), on_new=boom)
    assert _run(upd.async_check(Fetch(_ok(_body())))) == "updated"
    assert "acme-hybrid" in builtin_ids()


def test_store_save_failure_still_logged_and_feed_not_lost():
    upd = _updater(MemStore(fail_save=True))
    assert _run(upd.async_check(Fetch(_ok(_body())))) == "updated"
    assert "acme-hybrid" in builtin_ids()


def test_fetch_exception_is_an_error_not_a_crash():
    async def boom(etag):
        raise RuntimeError("x")
    assert _run(_updater(MemStore()).async_check(boom)) == "error"


def test_one_check_in_flight():
    gate = asyncio.Event()

    async def go():
        async def slow(etag):
            await gate.wait()
            return _ok(_body())
        upd = _updater(MemStore())
        first = asyncio.create_task(upd.async_check(slow))
        await asyncio.sleep(0)
        assert await upd.async_check(slow) == "busy"
        gate.set()
        assert await first == "updated"
    _run(go())


def test_bounded_check_times_out_and_swallows_errors(monkeypatch):
    monkeypatch.setattr(pf, "FEED_CHECK_TIMEOUT_S", 0.05)

    async def go():
        async def hang(etag):
            await asyncio.Event().wait()
        clock = Clock()
        upd = _updater(MemStore(), clock=clock)
        clock.t += pf.FEED_FIRST_CHECK_DELAY_S
        await asyncio.wait_for(pf.async_check_bounded(upd, hang), 1)
        assert "acme-hybrid" not in builtin_ids()
        assert not upd.due()                     # wiszący serwer = próba na dziś, nie co 5 min
    _run(go())


def test_verification_runs_in_the_job_runner():
    calls = []

    async def job(fn, *args):
        calls.append(fn.__name__)
        return fn(*args)
    upd = pf.ProfileFeedUpdater(MemStore(), keys=KEYS, clock=Clock(), run_job=job)
    assert _run(upd.async_check(Fetch(_ok(_body())))) == "updated"
    assert calls and all(c.startswith("_verify") or c.startswith("verify") for c in calls)


def test_missing_crypto_library_rejects_instead_of_accepting(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "cryptography.hazmat.primitives.asymmetric.ed25519", None)
    assert _reason(_body()) == "crypto_unavailable"


# --- etap 1: feed tylko DODAJE marki; nakładanie się z pakietem odrzuca feed ------------------------

def _copy_as(pid_from: str, pid_to: str) -> dict:
    raw = _raw(pid_from)
    raw["id"] = pid_to
    return raw


@pytest.mark.parametrize("source, new_id", [("goodwe-et", "goodwe-et-g2"), ("sofar-hyd", "aaa-sofar"),
                                            ("deye-sg", "deye-sg-clone")])
def test_feed_profile_identifying_a_packaged_device_is_skipped(source, new_id):
    v = pf.verify_feed_body(_body(feed=_feed([_acme(), _copy_as(source, new_id)])), keys=KEYS, held=None)
    assert list(v.profiles) == ["acme-hybrid"] and v.overlapping == (new_id,)


def test_feed_profile_claiming_a_packaged_model_for_the_same_ha_domain_is_skipped():
    raw = _acme()
    raw["ha"]["integrations"][0]["domain"] = "solarman"            # domena sofar-hyd (pakiet)
    raw["ha"]["integrations"][0]["model_regex"] = ["^SP"]           # model z obrazu wzorcowego sofar-hyd
    v = pf.verify_feed_body(_body(feed=_feed([raw, _acme("acme-mini")])), keys=KEYS, held=None)
    assert list(v.profiles) == ["acme-mini"] and v.overlapping == ("acme-hybrid",)


def test_shipped_identity_refs_cover_every_packaged_profile():
    refs = pf.packaged_refs()
    assert set(refs) == set(prof_mod.packaged_ids())
    for pid, ref in refs.items():
        assert pf.overlaps_packaged(_raw(pid), only=pid), pid       # każdy profil pakietu rozpoznaje swój obraz


@pytest.mark.parametrize("regex", ["^(a+)+$", "^(.*)*x", "^([a-z]+)+[0-9]", "^" + "A" * 300])
def test_unsafe_regex_rejects_the_feed(regex):
    raw = _acme()
    raw["ha"]["integrations"][0]["model_regex"] = [regex]
    assert _reason(_body(feed=_feed([raw]))) == "profile_invalid"


@pytest.mark.parametrize("bad", [["x"], "x", 5, {"integrations": "x"}, {"integrations": [5]}])
def test_malformed_sections_are_a_skip_not_a_crash(bad):
    raw = _acme("broken-hybrid")
    raw["ha"] = bad
    v = _verify(_acme(), raw)
    assert list(v.profiles) == ["acme-hybrid"] and v.skipped == (("broken-hybrid", "schema"),)


def test_skipped_profiles_logged_with_id_and_code_only(caplog):
    raw = _acme("broken-hybrid", label="SECRET-LABEL", future_field="SECRET-VALUE")
    with caplog.at_level(logging.WARNING):
        assert _run(_updater(MemStore()).async_check(Fetch(_ok(_body(feed=_feed([_acme(), raw])))))) == "updated"
    assert "broken-hybrid" in caplog.text and "schema" in caplog.text
    assert "SECRET" not in caplog.text


def test_skipped_profiles_logged_again_when_the_stored_feed_is_loaded(caplog):
    store = MemStore()
    body = _body(feed=_feed([_acme(), _acme("next-hybrid", min_engine=7)]))
    _run(_updater(store).async_check(Fetch(_ok(body))))
    pf.set_feed_profiles({})
    upd = _updater(store)
    with caplog.at_level(logging.WARNING):
        _run(upd.async_load())
    assert upd.held.skipped == (("next-hybrid", "engine"),)
    assert pf.feed_ids() == frozenset({"acme-hybrid"})
    assert "next-hybrid" in caplog.text and "engine" in caplog.text


def test_rollback_floor_survives_an_unverifiable_store(caplog):
    store = MemStore()
    _run(_updater(store).async_check(Fetch(_ok(_body(generated_at=T1)))))
    store.data["document"]["signature"] = base64.b64encode(b"\0" * 64).decode()   # np. rotacja klucza
    pf.set_feed_profiles({})
    upd = _updater(store)
    _run(upd.async_load())
    assert upd.held is None
    assert _run(upd.async_check(Fetch(_ok(_body(generated_at=T0))))) == "rejected"
    assert _run(upd.async_check(Fetch(_ok(_body(generated_at="2026-10-16T08:00:00.000Z"))))) == "updated"


def test_far_future_floor_is_ignored():
    store = MemStore({"document": None, "floor": "2999-01-01T00:00:00.000Z", "checked_at": 0})
    upd = _updater(store)
    assert _run(upd.async_check(Fetch(_ok(_body(generated_at=T0))))) == "updated"


def test_ignored_packaged_ids_logged_with_ids_only(caplog):
    raw = _raw("sofar-hyd")
    raw["label"] = "SECRET-LABEL"
    with caplog.at_level(logging.WARNING):
        assert _run(_updater(MemStore()).async_check(Fetch(_ok(_body(feed=_feed([_acme(), raw])))))) == "updated"
    assert "sofar-hyd" in caplog.text and "SECRET-LABEL" not in caplog.text


# --- pierwszeństwo profili pakietu przy wyborze i sondzie -------------------------------------------

def _feed_draft(pid="goodwe-et-g2", source="goodwe-et"):
    p = profile_from_dict(pf._as_draft(_copy_as(source, pid)))
    return dataclasses.replace(p, origin="feed")


def test_select_prefers_the_verified_profile_over_a_shadowing_draft():
    hint = InverterHint("goodwe", "GoodWe", "GW8KN-ET")
    profiles = [load_builtin("goodwe-et"), _feed_draft()]
    assert select_profile([hint], profiles).profile.id == "goodwe-et"
    assert select_profile([hint], [load_builtin("goodwe-et")]).profile.id == "goodwe-et"


def test_probe_order_puts_packaged_verified_profiles_first():
    profiles = [_feed_draft("aaa-first"), load_builtin("deye-sg"), load_builtin("goodwe-et")]
    assert [p.id for p in identify_mod.probe_order(profiles)] == ["goodwe-et", "deye-sg", "aaa-first"]


def test_load_profiles_lists_packaged_verified_first_and_feed_last():
    _activate(_acme("aaa-acme"))
    ids = [p.id for p in ds.load_profiles()]
    assert ids[0] == "goodwe-et" and ids[-1] == "aaa-acme"


@pytest.mark.asyncio
async def test_register_probe_tries_profiles_in_priority_order(monkeypatch):
    tried = []

    async def fake_read(transport, prof, left, errors):
        tried.append(prof.id)
        return None, "fail"

    class _T:
        stats = None

        async def close(self):
            return None
    monkeypatch.setattr(identify_mod, "_read_identify", fake_read)
    profiles = [_feed_draft("aaa-first"), load_builtin("goodwe-et")]
    cand = identify_mod.Candidate(host="192.0.2.1", source="manual", transports=("goodwe_udp",))
    await identify_mod.identify_detailed(cand, profiles, transport_factory=lambda cfg: _T(), salt=b"s" * 16)
    assert tried == ["goodwe-et", "aaa-first"]


# --- runda 2: ścisłe pierwszeństwo pakietu, nakładanie się = pominięcie wpisu -----------------------

def _gw_series(pid="goodwe-gw10k", model_rx="^GW10K", ha_rx="(?i)goodwe gw10k"):
    raw = _copy_as("goodwe-et", pid)
    raw["identify"]["model_regex"] = [model_rx]
    for integ in raw["ha"]["integrations"]:
        integ["model_regex"] = [ha_rx]
    return dataclasses.replace(profile_from_dict(pf._as_draft(raw)), origin="feed")


@pytest.mark.parametrize("feed", [
    _gw_series(),
    _gw_series("goodwe-any", "^GW", "(?i)goodwe"),                      # nadzbiór wzorców pakietu
    _gw_series("goodwe-et-g2", "^GW[0-9.]+K[A-Z]*-(ET|EH|BT|BH)", "(?i)goodwe gw"),
], ids=["series", "superset", "same-regex"])
def test_packaged_profile_matching_the_device_always_beats_a_feed_profile(feed):
    hint = InverterHint("goodwe", "GoodWe", "GW10K-ET")
    choice = select_profile([hint], [load_builtin("goodwe-et"), feed])
    assert choice.profile.id == "goodwe-et" and choice.integration_domain == "goodwe"


def test_feed_profile_selected_when_no_packaged_profile_matches():
    feed = _gw_series("acme-hyb", "^ACME", "(?i)acme")
    raw = _thaw_raw(feed)
    for integ in raw["ha"]["integrations"]:
        integ["domain"] = "acme_power"
    feed = dataclasses.replace(profile_from_dict(raw), origin="feed")
    choice = select_profile([InverterHint("acme_power", "Acme", "ACME-H5")], [load_builtin("goodwe-et"), feed])
    assert choice is not None and choice.profile.id == "acme-hyb"


def _thaw_raw(profile):
    return json.loads(json.dumps(dict(profile.raw)))


@pytest.mark.asyncio
async def test_register_probe_never_returns_a_feed_profile_when_a_packaged_one_matches(monkeypatch):
    tried = []

    async def fake_read(transport, prof, left, errors):
        tried.append(prof.id)
        return object(), "ok"

    class _T:
        stats = None

        async def close(self):
            return None
    monkeypatch.setattr(identify_mod, "_read_identify", fake_read)
    monkeypatch.setattr(identify_mod, "identity_info", lambda prof, image: {
        "matched": True, "model": "M", "rated_power_w": None})
    monkeypatch.setattr(identify_mod, "device_fingerprint", lambda salt, prof, image: "f" * 16)
    monkeypatch.setattr(identify_mod, "RegisterClient", lambda *a, **k: object())
    feed_raw = _copy_as("goodwe-et", "aaa-broad")
    # Feed obsługuje łącze, które sonda próbuje PIERWSZE, a profil pakietu nie.
    feed_raw["transports"] = ["solarman_v5"]
    feed_raw["modbus"]["transport_options"] = {"solarman_v5": {"port": 8899, "timeout_ms": 3000, "gap_ms": 200}}
    feed = dataclasses.replace(profile_from_dict(pf._as_draft(feed_raw)), origin="feed")
    cand = identify_mod.Candidate(host="192.0.2.1", source="manual", logger_serial=1234567890,
                                  transports=("solarman_v5", "goodwe_udp"))
    ident, _client, _used, _err = await identify_mod.identify_detailed(
        cand, [feed, load_builtin("goodwe-et")], transport_factory=lambda cfg: _T(), salt=b"s" * 16)
    assert ident.profile_id == "goodwe-et" and tried == ["goodwe-et"]


def test_overlapping_entry_is_skipped_and_the_rest_accepted():
    v = pf.verify_feed_body(_body(feed=_feed([_acme(), _copy_as("goodwe-et", "goodwe-et-g2")])),
                            keys=KEYS, held=None)
    assert list(v.profiles) == ["acme-hybrid"] and v.overlapping == ("goodwe-et-g2",)


def test_stored_feed_survives_a_release_whose_packaged_set_overlaps_an_entry(monkeypatch):
    store = MemStore()
    body = _body(feed=_feed([_acme(), _acme("zeta-hybrid", label="Zeta")]))
    _run(_updater(store).async_check(Fetch(_ok(body))))
    pf.set_feed_profiles({})
    real = pf.overlaps_packaged
    monkeypatch.setattr(pf, "overlaps_packaged",                      # nowy profil pakietu = acme
                        lambda raw, only=None: raw["id"] == "acme-hybrid" or real(raw, only=only))
    upd = _updater(store)
    _run(upd.async_load())
    assert upd.held is not None and upd.held.overlapping == ("acme-hybrid",)
    assert "zeta-hybrid" in builtin_ids() and "acme-hybrid" not in builtin_ids()


# --- runda 3: każda połowa „profil pakietu pasuje do urządzenia” osobno ---------------------------

def _feed_entity(pid, domain, model_rx, ha_rx):
    """Szkic z feedu dopasowany przez encje (domena + model), z własną identyfikacją."""
    raw = _copy_as("solis-hybrid", pid)
    raw["identify"]["model_regex"] = [model_rx]
    raw["ha"]["integrations"] = [dict(raw["ha"]["integrations"][0], domain=domain, model_regex=[ha_rx])]
    return dataclasses.replace(profile_from_dict(pf._as_draft(raw)), origin="feed")


@pytest.mark.parametrize("hint, feed, expected", [
    # tylko `ha.integrations[].model_regex` pakietu (bez producenta albo producent spoza id profilu)
    (InverterHint("solarman", "Ginlong", "S6-EH1P5K-L"),
     _feed_entity("ginlong-s6", "solarman", "^S6", "(?i)ginlong s6"), "solis-hybrid"),
    (InverterHint("solax_modbus", None, "HYD 6KTL-3PH"),
     _feed_entity("acme-hydx", "solax_modbus", "^HYD", "(?i)hyd"), "sofar-hyd"),
    # tylko domena + model identyfikacji (bez producenta); wzorzec HA feedu zawęziłby do feedu
    (InverterHint("goodwe", None, "GW10K-ET"), _gw_series(ha_rx="(?i)gw10k"), "goodwe-et"),
    # tylko marka w producencie (domena feedu, nie pakietu); szkic pakietu przegrałby z dopasowaniem encji
    (InverterHint("acme_power", "Sofar", "FOO-5"),
     _feed_entity("acme-foo", "acme_power", "^FOO", "(?i)foo"), "sofar-hyd"),
], ids=["ha-text-ginlong", "ha-text-no-maker", "entity-no-maker", "maker-only"])
def test_each_packaged_match_kind_excludes_feed_profiles(hint, feed, expected):
    packaged = [p for p in ds.load_profiles() if p.origin == "package"]
    assert select_profile([hint], packaged).profile.id == expected
    assert select_profile([hint], packaged + [feed]).profile.id == expected


def test_feed_limits_match_the_cloud_builder():
    # Budowa feedu w chmurze (`feed_too_large`) pilnuje tych samych progów — powyżej klient odrzuca cały feed.
    assert pf.FEED_MAX_PROFILES == 500
    assert pf.FEED_MAX_BYTES == 2 * 1024 * 1024
