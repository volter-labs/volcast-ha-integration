"""Profil „tylko encje” (`access: entities_only`): odczyt z encji HA, bez sekcji rejestrowych i bez zapisu."""
import base64
import copy
import json
from pathlib import Path
from types import MappingProxyType

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from custom_components.volcast.control import direct_search as ds
from custom_components.volcast.core import endorsement as en
from custom_components.volcast.core import profile_feed as pf
from custom_components.volcast.core.control.select import control_verified
from custom_components.volcast.core.discovery.identify import _groups
from custom_components.volcast.core.profile import (
    ORIGIN_FEED, ORIGIN_FEED_ENDORSED, ORIGIN_PACKAGE, ProfileError, direct_verified, entities_only,
    load_builtin, load_profile, packaged_ids, profile_from_dict, writes_allowed,
)
from custom_components.volcast.core.profile_schema import (
    ACCESS_ENTITIES_ONLY, CAPABILITY_KEYS, PROFILE_ENGINE, REGISTER_SECTIONS, TRANSPORTS, validate_profile,
)
from tests.core.profile_fixtures import eo_profile, ms_profile

ENT = "$.ha.integrations[0].entities"
KEY_MSG = "klucz nastawy, trybu albo zapisu niedozwolony w profilu entities_only"
DOMAIN_MSG = "spoza ['sensor'] (profil entities_only tylko do odczytu)"
FORBIDDEN_MSG = "niedozwolone w profilu entities_only (rejestry, tryb albo encja zapisu)"
CAP_MSG = "oczekiwano false (profil entities_only bez sterowania)"


def _errs(p):
    return "\n".join(validate_profile(p))


def _only(p, error):
    """Dokładnie jeden błąd — ten oczekiwany (nie „gdzieś w liście”)."""
    assert validate_profile(p) == [error]


def _all_under(p, prefix):
    """Co najmniej jeden błąd i każdy dotyczy pola `prefix`."""
    errs = validate_profile(p)
    assert errs and all(e.startswith(prefix) for e in errs), errs


# --- schemat ------------------------------------------------------------------------------------------

def test_minimal_entities_only_profile_validates():
    assert validate_profile(eo_profile()) == []


def test_minimal_entities_only_profile_loads_from_file(tmp_path):
    raw = eo_profile()
    f = tmp_path / f"{raw['id']}.json"
    f.write_text(json.dumps(raw), encoding="utf-8")
    p = load_profile(f)
    assert p.id == "test-eo" and p.access == ACCESS_ENTITIES_ONLY and entities_only(p)


def test_full_profile_is_not_entities_only():
    p = profile_from_dict(ms_profile())
    assert p.access is None and not entities_only(p)


@pytest.mark.parametrize("value", ["entities", "full", "", None, True, ["entities_only"]])
def test_access_accepts_only_entities_only(value):
    p = eo_profile()
    p["access"] = value
    _only(p, f"$.access: {value!r} spoza ['entities_only']")


def test_access_entities_only_on_a_register_profile_still_needs_the_entities_only_rules():
    # Pełny profil z dopisanym `access` przestaje być pełnym profilem: sekcje rejestrowe, tryby i encje
    # zapisu są błędem.
    p = ms_profile()
    p.update(access="entities_only", min_engine=3)
    errs = validate_profile(p)
    assert f"$.write: {FORBIDDEN_MSG}" in errs and f"$.modes: {FORBIDDEN_MSG}" in errs
    assert f"{ENT}.soc_min: {KEY_MSG}" in errs


@pytest.mark.parametrize("domain", ["number", "select", "switch", "button", "time", "light", "binary_sensor"])
def test_entity_outside_the_sensor_domain_is_rejected(domain):
    p = eo_profile()
    p["ha"]["integrations"][0]["entities"]["soc"]["domain"] = domain
    _only(p, f"{ENT}.soc.domain: {domain!r} {DOMAIN_MSG}")


def test_binary_sensor_domain_stays_rejected_in_a_register_profile():
    p = ms_profile()
    p["ha"]["integrations"][0]["entities"]["soc"]["domain"] = "binary_sensor"
    assert f"{ENT}.soc.domain: 'binary_sensor' spoza" in _errs(p)


@pytest.mark.parametrize("spec", [
    {"unique_id_regex": "_x$"},                                 # bez domeny
    {"domain": None, "unique_id_regex": "_x$"},
    {"domain": ["sensor"], "unique_id_regex": "_x$"},
    {"domain": 1, "unique_id_regex": "_x$"},
    {"domain": "Sensor", "unique_id_regex": "_x$"},
], ids=["missing", "null", "list", "int", "case"])
def test_entity_domain_of_a_wrong_shape_is_rejected(spec):
    p = eo_profile()
    p["ha"]["integrations"][0]["entities"]["soc"] = spec
    _all_under(p, f"{ENT}.soc.domain")


@pytest.mark.parametrize("spec", ["sensor", None, ["sensor"], 1])
def test_entity_spec_that_is_not_an_object_is_rejected(spec):
    p = eo_profile()
    p["ha"]["integrations"][0]["entities"]["soc"] = spec
    _only(p, f"{ENT}.soc: oczekiwano obiektu")


@pytest.mark.parametrize("key", ["mode", "mode_value", "power_w", "soc_min", "soc_max", "export_limit_w",
                                 "export_limit_enabled", "charge_power_w", "discharge_power_w", "enable",
                                 "tou_1_start"])
def test_setpoint_and_write_keys_are_rejected(key):
    p = eo_profile()
    p["ha"]["integrations"][0]["entities"][key] = {"domain": "sensor", "unique_id_regex": "_x$"}
    _only(p, f"{ENT}.{key}: {KEY_MSG}")


def test_unknown_entity_key_is_rejected():
    p = eo_profile()
    p["ha"]["integrations"][0]["entities"]["socket"] = {"domain": "sensor", "unique_id_regex": "_x$"}
    _only(p, f"{ENT}.socket: nieznany klucz encji")


def test_profile_without_integrations_is_rejected():
    p = eo_profile()
    p["ha"]["integrations"] = []
    _only(p, "$.ha.integrations: oczekiwano niepustej listy (profil entities_only)")


def test_integration_without_entities_is_rejected():
    p = eo_profile()
    p["ha"]["integrations"][0]["entities"] = {}
    _only(p, f"{ENT}: oczekiwano co najmniej jednej encji (profil entities_only)")


@pytest.mark.parametrize("cap", CAPABILITY_KEYS)
@pytest.mark.parametrize("value", [True, 0, 1, "false", None])
def test_capability_other_than_literal_false_is_rejected(cap, value):
    p = eo_profile()
    p["capabilities"][cap] = value
    _only(p, f"$.capabilities.{cap}: {CAP_MSG}")


@pytest.mark.parametrize("cap", CAPABILITY_KEYS + ("time_windows",))
def test_missing_capability_key_is_rejected(cap):
    p = eo_profile()
    del p["capabilities"][cap]
    _only(p, f"$.capabilities.{cap}: brak wymaganego pola")


def test_extra_capability_key_is_rejected():
    p = eo_profile()
    p["capabilities"]["fly"] = False
    _only(p, "$.capabilities.fly: nieznane pole")


@pytest.mark.parametrize("value", [6, 1, False, 0.0, "0", None])
def test_time_windows_must_be_integer_zero(value):
    p = eo_profile()
    p["capabilities"]["time_windows"] = value
    _only(p, "$.capabilities.time_windows: oczekiwano 0")


def test_capabilities_still_required():
    p = eo_profile()
    del p["capabilities"]
    _all_under(p, "$.capabilities")


@pytest.mark.parametrize("field,value", [
    ("modes", {"auto": {"value": 1, "direction": "neutral", "ha_option": "auto"}}),
    ("neutral_mode", "auto"),
    ("work_mode", {"read": {"addr": 1, "type": "u16"}, "neutral_values": [1], "poll_s": 300,
                   "ha": {"unique_id_regex": "_work_mode$", "neutral_options": ["General"]}}),
])
def test_writable_entity_constructs_are_rejected(field, value):
    p = eo_profile()
    p[field] = value
    _only(p, f"$.{field}: {FORBIDDEN_MSG}")


@pytest.mark.parametrize("section", REGISTER_SECTIONS)
def test_register_sections_are_rejected(section):
    # Profil „tylko encje” nie niesie rejestrów — pozostawiona sekcja (np. z szablonu) to błąd pliku.
    src = ms_profile()
    p = eo_profile()
    p[section] = src.get(section, {"programs": 1})
    _only(p, f"$.{section}: {FORBIDDEN_MSG}")


def test_unsafe_regex_in_a_register_section_is_also_reported():
    p = eo_profile()
    p["identify"] = {"model_regex": ["(a+)+$"]}
    errs = validate_profile(p)
    assert f"$.identify: {FORBIDDEN_MSG}" in errs
    assert any(e.startswith("$.identify.model_regex[0]: niebezpieczny regex") for e in errs)


def test_min_engine_missing_is_rejected():
    p = eo_profile()
    del p["min_engine"]
    _only(p, "$.min_engine: brak wymaganego pola")


@pytest.mark.parametrize("engine", [1, 2])
def test_min_engine_below_three_is_rejected(engine):
    p = eo_profile()
    p["min_engine"] = engine
    _only(p, "$.min_engine: profil entities_only wymaga min_engine >= 3")


@pytest.mark.parametrize("engine", [True, False, "3", 3.0, None, [3]])
def test_min_engine_of_a_wrong_type_is_rejected(engine):
    p = eo_profile()
    p["min_engine"] = engine
    _all_under(p, "$.min_engine: oczekiwano liczby całkowitej")


@pytest.mark.parametrize("engine", [3, 4, 99])
def test_min_engine_of_three_or_more_is_accepted(engine):
    p = eo_profile()
    p["min_engine"] = engine
    assert validate_profile(p) == []


@pytest.mark.parametrize("field", ["schema_version", "id", "label", "status", "sources", "ha"])
def test_identity_fields_stay_required(field):
    p = eo_profile()
    del p[field]
    _all_under(p, f"$.{field}")


def test_unknown_top_level_field_still_an_error():
    p = eo_profile()
    p["extra"] = 1
    _only(p, "$.extra: nieznane pole")


# --- obiekt profilu: brak sekcji = puste wartości domyślne --------------------------------------------

def test_loaded_profile_has_empty_register_sections():
    p = profile_from_dict(eo_profile())
    for key in ("identify", "read", "write", "intents", "baseline", "write_policy", "limits"):
        assert dict(p.raw[key]) == {}, key
    assert list(p.raw["transports"]) == []
    assert p.control_model == "none" and p.modes == {} and p.neutral_mode is None
    assert p.write_order == () and p.work_mode is None and p.nvm_budget is None
    assert p.power_by_direction is False and p.baseline_enable is None
    assert p.modbus.status == "draft" and p.modbus.transport_options == {}
    assert p.modbus.identify_reads == () and p.modbus.probe_keys == ()
    assert p.tou_programs == 0 and p.tou_field_order == ()
    assert p.power_kind("sell") is None


def test_loaded_profile_keeps_ha_section():
    p = profile_from_dict(eo_profile())
    assert p.raw["ha"]["integrations"][0]["domain"] == "test_entities"


@pytest.mark.parametrize("kind", TRANSPORTS)
def test_never_probed_directly(kind):
    assert _groups(kind, [profile_from_dict(eo_profile())]) == []


# --- prawo zapisu ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("origin", [ORIGIN_PACKAGE, ORIGIN_FEED_ENDORSED, ORIGIN_FEED])
def test_never_writes_even_verified(origin):
    raw = eo_profile()
    raw["status"] = "verified"
    raw["ha"]["integrations"][0]["status"] = "verified"
    p = profile_from_dict(raw, origin=origin)
    assert writes_allowed(p) is False
    assert direct_verified(p) is False
    assert control_verified(p, "test_entities") is False


def test_full_packaged_profile_still_writes():
    assert writes_allowed(profile_from_dict(ms_profile(), origin=ORIGIN_PACKAGE)) is True


def test_direct_not_offered_when_its_integration_holds_the_port():
    p = profile_from_dict(eo_profile(), origin=ORIGIN_PACKAGE)
    opt = ds.direct_option(None, [p], held_by=p)
    assert opt["available"] is False and opt["description_code"] == ds.DIRECT_UNAVAILABLE


def test_direct_still_offered_for_a_register_profile_held_by_its_integration():
    p = profile_from_dict(ms_profile(), origin=ORIGIN_PACKAGE)
    assert ds.direct_option(None, [p], held_by=p)["description_code"] == ds.DIRECT_REQUIRES_DISABLE


# --- silnik i feed --------------------------------------------------------------------------------------

def test_engine_three_loads_entities_only_profiles():
    assert PROFILE_ENGINE == 3


def test_packaged_profiles_load_unchanged_and_none_is_entities_only():
    for pid in packaged_ids():
        p = load_builtin(pid)
        assert not entities_only(p) and p.raw.get("access") is None


_PRIV = Ed25519PrivateKey.generate()
_KEYS = {"test-key": _PRIV.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()}


def _body(*profiles) -> bytes:
    feed = {"schema_version": 1, "generated_at": "2026-10-14T08:00:00.000Z", "key_id": "test-key",
            "profiles": sorted(profiles, key=lambda p: p["id"])}
    sig = base64.b64encode(_PRIV.sign(pf.canonical_bytes(feed))).decode()
    return pf.canonical_bytes({"feed": feed, "key_id": "test-key", "signature": sig})


@pytest.fixture
def _clean_registry():
    pf.set_feed_profiles({})
    yield
    pf.set_feed_profiles({})


def test_feed_accepts_entities_only_profile(_clean_registry):
    v = pf.verify_feed_body(_body(eo_profile()), keys=_KEYS, held=None)
    assert list(v.profiles) == ["test-eo"] and v.skipped == ()
    pf.set_feed_profiles(v.profiles)
    p = load_builtin("test-eo")
    assert p.origin == ORIGIN_FEED and entities_only(p) and writes_allowed(p) is False


def test_feed_skips_entities_only_profile_with_a_write_entity(_clean_registry):
    bad = eo_profile()
    bad["ha"]["integrations"][0]["entities"]["soc"]["domain"] = "number"
    v = pf.verify_feed_body(_body(bad), keys=_KEYS, held=None)
    assert v.profiles == {} and v.skipped == (("test-eo", "schema"),)


def test_feed_skips_profile_for_a_newer_engine(_clean_registry):
    nxt = eo_profile()
    nxt["min_engine"] = PROFILE_ENGINE + 1
    v = pf.verify_feed_body(_body(nxt), keys=_KEYS, held=None)
    assert v.skipped == (("test-eo", "engine"),)


# Certyfikat zapisu (klucz testowy z wektorów): nawet ważny certyfikat nie daje zapisu profilowi „tylko encje”.
_VECTORS = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "profile_feed"
                       / "endorsement-vectors.json").read_text("utf-8"))
_EKEY = _VECTORS["test_keys"][0]
_EPRIV = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(_EKEY["seed_hex"]))


def _endorse(raw: dict) -> dict:
    out = copy.deepcopy(raw)
    out.pop("endorsement", None)
    digest = en.endorsement_digest(out)
    sig = base64.b64encode(_EPRIV.sign(en.endorsement_message(out["id"], digest, _EKEY["key_id"]))).decode()
    out["endorsement"] = {"key_id": _EKEY["key_id"], "digest": digest, "signature": sig}
    return out


def test_endorsed_verified_feed_profile_still_never_writes(_clean_registry, monkeypatch):
    monkeypatch.setattr(en, "PROFILE_ENDORSEMENT_PUBLIC_KEYS",
                        MappingProxyType({_EKEY["key_id"]: _EKEY["public_hex"]}))
    raw = eo_profile()
    raw["status"] = "verified"
    raw["ha"]["integrations"][0]["status"] = "verified"
    v = pf.verify_feed_body(_body(_endorse(raw)), keys=_KEYS, held=None)
    assert list(v.profiles) == ["test-eo"] and v.degraded == () and v.skipped == ()
    pf.set_feed_profiles(v.profiles)
    p = load_builtin("test-eo")
    assert p.origin == ORIGIN_FEED_ENDORSED and p.status == "verified"   # certyfikat przyjęty...
    assert writes_allowed(p) is False and direct_verified(p) is False    # ...a zapisu i tak nie ma
    assert control_verified(p, "test_entities") is False


def test_entities_only_profile_load_errors_are_profile_errors():
    raw = eo_profile()
    raw["capabilities"]["standby"] = True
    with pytest.raises(ProfileError):
        profile_from_dict(raw)


def test_entities_option_for_entities_only_profile_is_read_only_not_unverified():
    # Profil „tylko encje” nie ma encji zapisu z założenia — kod „brak encji zapisu”, nie „niezweryfikowany”
    # (ten sugerowałby, że weryfikacja włączy sterowanie). Kod z zamkniętego zbioru protokołu parowania.
    from custom_components.volcast.core.control.select import ProfileChoice
    for status in ("draft", "verified"):
        raw = eo_profile()
        raw["status"] = status
        raw["ha"]["integrations"][0]["status"] = status
        choice = ProfileChoice(profile_from_dict(raw, origin=ORIGIN_PACKAGE), "test_entities", None)
        opt = ds.entities_option(choice, {"soc": "sensor.soc"})
        assert opt == {"id": "entities", "available": False, "description_code": ds.ENTITIES_NO_WRITE}
        assert opt["description_code"] in ds.DESCRIPTION_CODES
