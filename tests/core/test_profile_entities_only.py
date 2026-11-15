"""Profil „tylko encje” (`access: entities_only`): odczyt z encji HA, bez sekcji rejestrowych i bez zapisu."""
import base64
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from custom_components.volcast.control import direct_search as ds
from custom_components.volcast.core import profile_feed as pf
from custom_components.volcast.core.control.select import control_verified
from custom_components.volcast.core.discovery.identify import _groups
from custom_components.volcast.core.profile import (
    ORIGIN_FEED, ORIGIN_FEED_ENDORSED, ORIGIN_PACKAGE, ProfileError, direct_verified, entities_only,
    load_builtin, load_profile, packaged_ids, profile_from_dict, writes_allowed,
)
from custom_components.volcast.core.profile_schema import (
    ACCESS_ENTITIES_ONLY, CAPABILITY_KEYS, PROFILE_ENGINE, TRANSPORTS, validate_profile,
)
from tests.core.profile_fixtures import eo_profile, ms_profile

# Sekcje rejestrowe — w profilu „tylko encje” opcjonalne i ignorowane.
REGISTER_SECTIONS = ("transports", "identify", "read", "write", "intents", "baseline", "write_policy",
                     "limits", "unit_id", "control_model", "modbus", "tou")


def _errs(p):
    return "\n".join(validate_profile(p))


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
    assert "$.access" in _errs(p)


def test_access_entities_only_on_a_register_profile_still_needs_the_entities_only_rules():
    # Pełny profil z dopisanym `access` przestaje być pełnym profilem: tryby i encje zapisu są błędem.
    p = ms_profile()
    p.update(access="entities_only", min_engine=3)
    errs = _errs(p)
    assert "$.modes" in errs and "$.ha.integrations[0].entities.soc_min" in errs


@pytest.mark.parametrize("domain", ["number", "select", "switch", "button", "time", "light"])
def test_entity_outside_sensor_domains_is_rejected(domain):
    p = eo_profile()
    p["ha"]["integrations"][0]["entities"]["soc"]["domain"] = domain
    errs = _errs(p)
    assert "$.ha.integrations[0].entities.soc.domain" in errs
    assert "entities_only" in errs


def test_binary_sensor_domain_is_allowed():
    p = eo_profile()
    p["ha"]["integrations"][0]["entities"]["soc"]["domain"] = "binary_sensor"
    assert validate_profile(p) == []


def test_binary_sensor_domain_stays_rejected_in_a_register_profile():
    p = ms_profile()
    p["ha"]["integrations"][0]["entities"]["soc"]["domain"] = "binary_sensor"
    assert "$.ha.integrations[0].entities.soc.domain" in _errs(p)


@pytest.mark.parametrize("key", ["mode", "mode_value", "power_w", "soc_min", "soc_max", "export_limit_w",
                                 "export_limit_enabled", "charge_power_w", "discharge_power_w", "enable",
                                 "tou_1_start"])
def test_setpoint_and_write_keys_are_rejected(key):
    p = eo_profile()
    p["ha"]["integrations"][0]["entities"][key] = {"domain": "sensor", "unique_id_regex": "_x$"}
    assert f"$.ha.integrations[0].entities.{key}" in _errs(p)


@pytest.mark.parametrize("cap", CAPABILITY_KEYS)
def test_true_capability_is_rejected(cap):
    p = eo_profile()
    p["capabilities"][cap] = True
    errs = _errs(p)
    assert f"$.capabilities.{cap}" in errs and "entities_only" in errs


def test_time_windows_must_be_zero():
    p = eo_profile()
    p["capabilities"]["time_windows"] = 6
    assert "$.capabilities.time_windows" in _errs(p)


def test_capabilities_still_required():
    p = eo_profile()
    del p["capabilities"]
    assert "$.capabilities: brak wymaganego pola" in _errs(p)


@pytest.mark.parametrize("field,value", [
    ("modes", {"auto": {"value": 1, "direction": "neutral", "ha_option": "auto"}}),
    ("neutral_mode", "auto"),
    ("work_mode", {"read": {"addr": 1, "type": "u16"}, "neutral_values": [1], "poll_s": 300,
                   "ha": {"unique_id_regex": "_work_mode$", "neutral_options": ["General"]}}),
])
def test_writable_entity_constructs_are_rejected(field, value):
    p = eo_profile()
    p[field] = value
    assert f"$.{field}" in _errs(p)


@pytest.mark.parametrize("engine", [None, 1, 2])
def test_min_engine_of_at_least_three_is_required(engine):
    p = eo_profile()
    if engine is None:
        del p["min_engine"]
    else:
        p["min_engine"] = engine
    assert "$.min_engine" in _errs(p)


@pytest.mark.parametrize("field", ["schema_version", "id", "label", "status", "sources", "ha"])
def test_identity_fields_stay_required(field):
    p = eo_profile()
    del p[field]
    assert f"$.{field}" in _errs(p)


def test_unknown_top_level_field_still_an_error():
    p = eo_profile()
    p["extra"] = 1
    assert "$.extra: nieznane pole" in _errs(p)


def test_register_sections_present_are_ignored():
    # Sekcje rejestrowe wolno zostawić (np. szablon) — nie są sprawdzane i nie trafiają do profilu.
    src = ms_profile()
    p = eo_profile()
    for key in REGISTER_SECTIONS:
        if key in src:
            p[key] = src[key]
    p["tou"] = {"programs": 99}
    assert validate_profile(p) == []
    loaded = profile_from_dict(p)
    assert loaded.raw["write"] == {} and loaded.raw["read"] == {} and loaded.raw["identify"] == {}
    assert loaded.control_model != "mode_setpoint" and loaded.modbus.transport_options == {}


def test_unsafe_regex_in_an_ignored_section_is_still_an_error():
    p = eo_profile()
    p["identify"] = {"model_regex": ["(a+)+$"]}
    assert "$.identify.model_regex[0]" in _errs(p)


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
    # Strażnik temperatury bez zakresu niczego nie przepuszcza; każdy odczyt nieświeży dla zapisu.
    assert not p.temp_min_c < 20 < p.temp_max_c and p.max_state_age_s == 0
    assert p.power_kind("sell") is None


def test_loaded_profile_keeps_ha_section():
    p = profile_from_dict(eo_profile())
    assert p.raw["ha"]["integrations"][0]["domain"] == "test_entities"


def test_fingerprint_ignores_register_sections():
    a = profile_from_dict(eo_profile())
    raw = eo_profile()
    raw["read"] = {"soc": {"addr": 1, "type": "u16"}}
    assert profile_from_dict(raw).fingerprint == a.fingerprint


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


def test_entities_only_profile_load_errors_are_profile_errors():
    raw = eo_profile()
    raw["capabilities"]["standby"] = True
    with pytest.raises(ProfileError):
        profile_from_dict(raw)
