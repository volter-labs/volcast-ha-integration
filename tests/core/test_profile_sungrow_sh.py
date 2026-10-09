"""Profil sungrow-sh (draft): identyfikacja, odczyty i spójność na syntetycznym obrazie golden."""
import json
import re
from pathlib import Path

import pytest

from custom_components.volcast.core.modbus.identity import identity_info
from custom_components.volcast.core.profile import load_builtin
from custom_components.volcast.core.profile_schema import validate_profile
from custom_components.volcast.core.registers import RegisterImage
from tests.core import profile_golden as pg
from tests.core.modbus.helpers import _golden_doc, golden_image, goodwe_image

PID = "sungrow-sh"
ROOT = Path(__file__).resolve().parents[2]
PROFILE_FILE = ROOT / "custom_components" / "volcast" / "profiles" / f"{PID}.json"
GAPS_FILE = ROOT / "docs" / "profiles" / "engine-gaps.md"
# Stan wzorcowy obrazu (README katalogu golden): konwencja rdzenia — bateria dodatnia = rozładowanie,
# sieć dodatnia = pobór. Sungrow: 5214-5215 ujemne przy ładowaniu (zgodnie z rdzeniem),
# 13010-13011 dodatnie przy oddawaniu (odwrotnie, stąd sign -1).
REFERENCE = {
    "soc": 55,
    "pv_power_w": 3200,
    "battery_power_w": -1500,
    "grid_power_w": 400,
    "load_power_w": 2100,
    "battery_temp_c": 24,
    "battery_voltage_v": 400,
    "pv_energy_total_kwh": 12345.6,
    "grid_import_total_kwh": 4321,
    "grid_export_total_kwh": 6789,
    "mode_value": 0,
}
# Kody typu urządzenia (rejestr 5000 dokumentacji = adres 4999 na łączu).
SH10RT = 0x0E03


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
    # Konwencja adresów musi być nazwana: profil używa adresu z ramki (numer rejestru producenta − 1).
    assert "register number minus 1" in profile.raw["status_note"]


def test_identify_matches_golden_and_rejects_other_brands(profile, image):
    pg.assert_identify_matches(profile, image)
    pg.assert_identify_rejects(profile, golden_image("deye-sg"))
    pg.assert_identify_rejects(profile, golden_image("huawei-sun2000"))
    pg.assert_identify_rejects(profile, goodwe_image())


def test_identify_reads_device_type_and_rated_power(profile, image):
    info = identity_info(profile, image)
    assert info["model"] == str(SH10RT)
    assert info["rated_power_w"] == 10000


def test_identification_uses_input_registers_only(profile):
    # Te same słowa w przestrzeni holding (FC 3) nie mogą dać trafienia: identyfikacja czyta FC 4.
    doc = _golden_doc(PID)
    holding_only = RegisterImage({int(a): w for a, w in doc["input_registers"].items()}, {})
    pg.assert_identify_rejects(profile, holding_only)
    for block in profile.raw["modbus"]["identify_reads"]:
        assert block.get("fc") == 4
    covered = {a for b in profile.raw["modbus"]["identify_reads"] for a in range(b["addr"], b["addr"] + b["count"])}
    for spec in profile.raw["identify"]["registers"].values():
        assert spec["fc"] == 4
        width = spec.get("len", 1)
        assert set(range(spec["addr"], spec["addr"] + width)) <= covered


@pytest.mark.parametrize("code", [0x0D0F, 0x0D17, 0x0D1B, 0x0E00, 0x0E0F, 0x0E0B, 0x0E12])
def test_identify_accepts_sh_rs_rt_family(profile, code):
    pg.assert_identify_matches(profile, _with_input({"4999": code}))


@pytest.mark.parametrize("code", [0x0E28, 0x0E20, 0x0D27, 0x0D09, 0x2432, 0x0000, 0xFFFF])
def test_identify_rejects_other_sungrow_lines(profile, code):
    # SH*T, MG*RL, starsza seria SH*K i falowniki sieciowe SG nie są objęte tym profilem.
    pg.assert_identify_rejects(profile, _with_input({"4999": code}))


def test_reads_decode_reference_state(profile, image):
    values = pg.decode_reads(profile, image)
    assert set(values) == set(profile.raw["read"])
    for key, want in REFERENCE.items():
        assert values[key] == pytest.approx(want), key


def test_power_balance_of_reference_state(profile, image):
    v = pg.decode_reads(profile, image)
    assert v["pv_power_w"] + v["battery_power_w"] + v["grid_power_w"] == v["load_power_w"]


def test_measurements_are_input_registers_and_settings_holding(profile):
    read = profile.raw["read"]
    for key, spec in read.items():
        want = 3 if key == "mode_value" else 4
        assert spec.get("fc", 3) == want, key
        if spec["type"] in ("u32", "i32"):
            assert spec.get("word_order") == "lo_hi", key  # Sungrow: młodsze słowo pierwsze


def test_static_consistency(profile):
    pg.assert_intents_consistent(profile)
    pg.assert_ha_regexes_compile(profile)
    pg.assert_capabilities_match_writes(profile)


def test_control_limited_to_ems_mode_register(profile):
    raw = profile.raw
    # Tryb wymuszony wymaga pary rejestrów (EMS mode = 2 i komenda 0xAA/0xBB/0xCC), a SoC i limity
    # skalowanego zapisu — luki silnika. Zostaje jeden zapis: powrót do autokonsumpcji (0).
    assert set(raw["write"]) == {"mode"}
    assert raw["write"]["mode"] == {"addr": 13049, "type": "u16", "encode": "mode"}
    assert raw["modes"] == {"self_consumption": {"value": 0, "direction": "neutral",
                                                 "ha_option": "Self-consumption mode (default)"}}
    neutral = {"mode": raw["neutral_mode"], "power": "none"}
    for intent in ("charge_grid", "discharge_forced", "sell", "standby", "self_consume", "charge_pv"):
        assert raw["intents"][intent] == neutral, intent
    caps = raw["capabilities"]
    for cap in ("force_charge_from_grid", "sell_from_battery", "force_discharge", "standby",
                "set_power_w", "limit_export", "set_soc_floor", "set_soc_ceiling"):
        assert caps[cap] is False, cap
    assert caps["time_windows"] == 0
    assert raw["neutral_mode"] == raw["baseline"]["mode"] == "self_consumption"
    assert raw["read"]["mode_value"]["addr"] == raw["write"]["mode"]["addr"]


def test_verify_blocks_cover_every_write_address(profile):
    m = profile.raw["modbus"]
    addrs = {spec["addr"] for spec in profile.raw["write"].values()}
    covered = {a for b in m["verify_blocks"] for a in range(b["addr"], b["addr"] + b["count"])}
    assert addrs <= covered
    assert set(m["probe_keys"]) == set(profile.raw["write"])
    assert m["write_function"] == 6
    assert m["transport_options"]["modbus_tcp"]["port"] == 502
    assert profile.raw["unit_id"] == 1


def test_ha_entity_map_matches_yaml_package_unique_ids(profile):
    integ = profile.raw["ha"]["integrations"][0]
    assert integ["domain"] == "modbus" and integ["ems"] is False and integ["status"] == "draft"
    ents = integ["entities"]
    samples = {
        "soc": "sg_battery_level",
        "battery_power_w": "sg_battery_power",
        "battery_temp_c": "sg_battery_temperature",
        "battery_voltage_v": "sg_battery_voltage",
        "pv_power_w": "sg_total_dc_power",
        "load_power_w": "sg_load_power",
        "grid_power_w": "sg_battery_export_power_raw",
        "pv_energy_total_kwh": "sg_total_pv_generation",
        "grid_import_total_kwh": "sg_total_imported_energy",
        "grid_export_total_kwh": "sg_total_exported_energy",
        "mode_value": "sg_ems_mode_selection_raw",
    }
    assert set(ents) == set(samples)
    for key, uid in samples.items():
        assert re.search(ents[key]["unique_id_regex"], uid), key
    # Sąsiednie encje pakietu nie mogą trafić do złego klucza.
    assert not re.search(ents["battery_power_w"]["unique_id_regex"], "sg_battery_power_raw")
    assert not re.search(ents["battery_power_w"]["unique_id_regex"], "sg_battery_charging_power_signed")
    assert not re.search(ents["load_power_w"]["unique_id_regex"], "sg_positive_load_power")
    assert not re.search(ents["pv_energy_total_kwh"]["unique_id_regex"], "sg_daily_pv_generation")
    assert ents["grid_power_w"].get("transform") == "negate"
    assert "transform" not in ents["battery_power_w"]
    # Select trybu EMS to encja platformy `template`, nie `modbus` — nie ma jej w tym wpisie.
    assert "mode" not in ents


def test_engine_gaps_document_sungrow():
    text = GAPS_FILE.read_text(encoding="utf-8")
    assert "## Sungrow (`sungrow-sh`)" in text
    section = text.split("## Sungrow (`sungrow-sh`)", 1)[1].split("\n## ", 1)[0]
    for needle in ("13049", "13050", "13051", "13057", "13058", "13073", "13086"):
        assert needle in section, needle


def _with_input(over: dict) -> RegisterImage:
    doc = _golden_doc(PID)
    words = {int(a): w for a, w in doc["registers"].items()}
    inputs = {int(a): w for a, w in {**doc["input_registers"], **over}.items()}
    return RegisterImage(words, inputs)
