"""Profil huawei-sun2000 (draft): identyfikacja, odczyty i spójność na syntetycznym obrazie golden."""
import json
from pathlib import Path

import pytest

from custom_components.volcast.core.profile import load_builtin
from custom_components.volcast.core.profile_schema import validate_profile
from tests.core import profile_golden as pg
from tests.core.modbus.helpers import golden_image, goodwe_image

PID = "huawei-sun2000"
PROFILE_FILE = Path(__file__).resolve().parents[2] / "custom_components" / "volcast" / "profiles" / f"{PID}.json"
# Stan wzorcowy obrazu (README katalogu golden): konwencja rdzenia — bateria dodatnia = rozładowanie,
# sieć dodatnia = pobór. Huawei raportuje odwrotnie w obu rejestrach (ładowanie > 0, oddawanie > 0).
REFERENCE = {
    "soc": 55,
    "pv_power_w": 3200,
    "battery_power_w": -1500,
    "grid_power_w": 400,
    "active_power_w": 1700,
    "load_power_w": 2100,
    "battery_temp_c": 24,
    "battery_voltage_v": 450,
    "grid_export_total_kwh": 1234.56,
    "grid_import_total_kwh": 2345.67,
    "mode_value": 2,
}


@pytest.fixture(scope="module")
def profile():
    return load_builtin(PID)


@pytest.fixture(scope="module")
def image():
    return golden_image(PID)


def test_profile_validates_and_is_draft(profile):
    assert validate_profile(json.loads(PROFILE_FILE.read_text(encoding="utf-8"))) == []
    assert profile.raw["status"] == "draft"
    assert profile.raw["modbus"]["status"] == "draft"
    assert profile.raw["status_note"] and profile.raw["modbus"]["status_note"]
    assert all(s["ref"].startswith("https://") for s in profile.raw["sources"])


def test_identify_matches_golden_and_rejects_other_brands(profile, image):
    pg.assert_identify_matches(profile, image)
    pg.assert_identify_rejects(profile, golden_image("deye-sg"))
    pg.assert_identify_rejects(profile, goodwe_image())


def test_identify_reads_model_and_rated_power(profile, image):
    from custom_components.volcast.core.modbus.identity import identity_info
    info = identity_info(profile, image)
    assert info["model"] == "SUN2000-5KTL-L1"
    assert info["rated_power_w"] == 5000


@pytest.mark.parametrize("model", ["SUN2000-10KTL-M1", "SUN2000-3.68KTL-L1"])
def test_identify_accepts_hybrid_l1_m1_family(profile, model):
    over = {str(30000 + i): w for i, w in enumerate(_ascii_words(model, 15))}
    pg.assert_identify_matches(profile, _with(PID, over))


@pytest.mark.parametrize("model", ["SUN2000-10KTL-M0", "SUN2000-100KTL-M1", "SUN2000-5KTL-USL0"])
def test_identify_rejects_non_hybrid_variants(profile, model):
    over = {str(30000 + i): w for i, w in enumerate(_ascii_words(model, 15))}
    pg.assert_identify_rejects(profile, _with(PID, over))


def test_reads_decode_reference_state(profile, image):
    values = pg.decode_reads(profile, image)
    assert set(values) == set(profile.raw["read"])
    for key, want in REFERENCE.items():
        assert values[key] == pytest.approx(want), key


def test_load_is_inverter_output_plus_grid_import(profile, image):
    values = pg.decode_reads(profile, image)
    assert values["load_power_w"] == values["active_power_w"] + values["grid_power_w"]


def test_static_consistency(profile):
    pg.assert_intents_consistent(profile)
    pg.assert_ha_regexes_compile(profile)
    pg.assert_capabilities_match_writes(profile)


def test_control_limited_to_working_mode_register(profile):
    raw = profile.raw
    # Wymuszone ładowanie/rozładowanie wymaga rejestrów warunkowych i mocy u32 — luka silnika.
    # mode_setpoint wymaga wszystkich sześciu intentów: nieobsługiwany = tryb neutralny bez mocy
    # i zdolność false (to samo, co zrobiłby silnik, schodząc do trybu neutralnego).
    assert set(raw["write"]) == {"mode"}
    assert raw["write"]["mode"]["addr"] == 47086
    neutral = {"mode": raw["neutral_mode"], "power": "none"}
    for intent in ("charge_grid", "discharge_forced", "sell", "standby", "self_consume", "charge_pv"):
        assert raw["intents"][intent] == neutral, intent
    caps = raw["capabilities"]
    for cap in ("force_charge_from_grid", "sell_from_battery", "force_discharge", "standby",
                "set_power_w", "limit_export", "set_soc_floor", "set_soc_ceiling"):
        assert caps[cap] is False, cap
    assert raw["neutral_mode"] == raw["baseline"]["mode"] == "maximise_self_consumption"


def test_verify_blocks_cover_every_write_address(profile):
    m = profile.raw["modbus"]
    addrs = {spec["addr"] for spec in profile.raw["write"].values()}
    covered = {a for b in m["verify_blocks"] for a in range(b["addr"], b["addr"] + b["count"])}
    assert addrs <= covered
    assert set(m["probe_keys"]) == set(profile.raw["write"])
    assert m["transport_options"]["modbus_tcp"]["port"] == 502


def test_ha_entity_map_matches_integration_unique_ids(profile):
    integ = profile.raw["ha"]["integrations"][0]
    assert integ["domain"] == "huawei_solar" and integ["ems"] is False
    ents = integ["entities"]
    import re
    serial = "HV2310123456"
    samples = {
        "soc": "storage_state_of_capacity",
        "battery_power_w": "storage_charge_discharge_power",
        "pv_power_w": "input_power",
        "active_power_w": "active_power",
        "grid_power_w": "power_meter_active_power",
        "battery_temp_c": "storage_unit_1_battery_temperature",
        "mode": "storage_working_mode_settings",
    }
    for key, suffix in samples.items():
        assert re.search(ents[key]["unique_id_regex"], f"{serial}_{suffix}"), key
    # Klucz nie może złapać sąsiedniej encji o wspólnym sufiksie.
    assert not re.search(ents["active_power_w"]["unique_id_regex"], f"{serial}_power_meter_active_power")
    assert not re.search(ents["soc"]["unique_id_regex"], f"{serial}_storage_unit_1_state_of_capacity")
    assert ents["battery_power_w"].get("transform") == "negate"
    assert ents["grid_power_w"].get("transform") == "negate"
    options = {m["ha_option"] for m in profile.raw["modes"].values()}
    assert options == {"maximise_self_consumption"}


def _ascii_words(text: str, length: int) -> list[int]:
    raw = text.encode("ascii").ljust(2 * length, b"\x00")
    return [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw), 2)]


def _with(pid: str, over: dict):
    from tests.core.modbus.helpers import _golden_doc, _image_from_doc
    doc = _golden_doc(pid)
    doc = {**doc, "registers": {**doc["registers"], **over}}
    return _image_from_doc(doc)
