"""Profil solis-hybrid (draft): identyfikacja, odczyty i spójność na syntetycznym obrazie golden."""
import json
import re
from pathlib import Path

import pytest

from custom_components.volcast.core.modbus.identity import identity_info
from custom_components.volcast.core.profile import load_builtin
from custom_components.volcast.core.profile_schema import validate_profile
from custom_components.volcast.core.registers import RegisterImage
from tests.core import profile_golden as pg
from tests.core.modbus.helpers import GOLDEN, golden_image, goodwe_image
from tests.core.test_profile_sofar_hyd import assert_sources_public

PID = "solis-hybrid"
ROOT = Path(__file__).resolve().parents[2]
PROFILE_FILE = ROOT / "custom_components" / "volcast" / "profiles" / f"{PID}.json"
GAPS_FILE = ROOT / "docs" / "profiles" / "engine-gaps.md"
SECTION = "## Solis (`solis-hybrid`)"
# Stan wzorcowy obrazu (README katalogu golden): konwencja rdzenia — bateria dodatnia = rozładowanie,
# sieć dodatnia = pobór. Solis odwrotnie w obu: 33149 i 33134 dodatnie przy ładowaniu, 33130
# dodatnie przy oddawaniu do sieci (stąd sign -1). Liczniki u32 w kWh, starsze słowo pierwsze.
REFERENCE = {
    "soc": 55,
    "pv_power_w": 3200,
    "battery_power_w": -1500,
    "grid_power_w": 400,
    "load_power_w": 2100,
    "active_power_w": 1700,
    "battery_voltage_v": 51.2,
    "battery_current_a": -29.3,
    "pv_energy_total_kwh": 12345,
    "grid_import_total_kwh": 4321,
    "grid_export_total_kwh": 6789,
    "mode_value": 1,
}
# Adres z ramki = dziesiętny numer rejestru z dokumentu producenta (bez przesunięcia o 1).
STORAGE_SWITCH = 43110
SERIAL = 33004
RATED_POWER = 33100
DIRECTION = 33135


def _doc() -> dict:
    return json.loads((GOLDEN / "solis_hybrid" / "registers.json").read_text())


def _image(**input_over: int) -> RegisterImage:
    """Obraz golden z nadpisaniem rejestrów INPUT (FC 4) — tam Solis trzyma serial i pomiary."""
    doc = _doc()
    inputs = {int(a): w for a, w in doc["input_registers"].items()}
    inputs.update({int(a): w for a, w in input_over.items()})
    return RegisterImage({int(a): w for a, w in doc["registers"].items()}, inputs)


def _serial_words(text: str) -> dict[str, int]:
    raw = text.encode("ascii").ljust(16, b"\0")
    return {str(SERIAL + i): (raw[2 * i] << 8) | raw[2 * i + 1] for i in range(8)}


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
    note = profile.raw["status_note"]
    # Konwencja adresów nazwana wprost: adres z ramki = dziesiętny numer producenta, bez -1.
    assert "wire (PDU) addresses" in note and "33139 is sent as 33139" in note
    # Warianty objęte i nieobjęte.
    assert "S6-EH1P" in note and "RHI" in note and "not covered" in note and "S6-GR1P" in note


def test_sources_are_public_and_cover_key_registers(profile):
    raw = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
    assert_sources_public(raw)
    whats = " ".join(s["what"] for s in raw["sources"])
    for reg in ("33004", "33100", "33130", "33134", "33135", "33139", "33149", "43110"):
        assert reg in whats, reg
    # Zapis 43110 = 1 ma źródło, które pisze tę wartość (wtyczka solax_modbus, opcja 1).
    assert any(s["ref"].endswith("/plugin_solis.py") and "43110" in s["what"] for s in raw["sources"])


def test_identify_matches_golden_and_rejects_other_brands(profile, image):
    pg.assert_identify_matches(profile, image)
    for other in ("deye-sg", "huawei-sun2000", "sungrow-sh", "solax-x-hybrid", "sofar-hyd"):
        pg.assert_identify_rejects(profile, golden_image(other))
    pg.assert_identify_rejects(profile, goodwe_image())


def test_identify_reads_serial_and_rated_power_from_input_registers(profile, image):
    info = identity_info(profile, image)
    assert info["model"] == "110F12FAKE000000"
    assert info["rated_power_w"] == 5000
    ident = profile.raw["identify"]
    assert ident["model_register"] == {"addr": SERIAL, "type": "ascii", "len": 8, "fc": 4}
    assert ident["registers"]["serial"] == {"addr": SERIAL, "type": "ascii", "len": 8, "fc": 4}
    assert ident["registers"]["rated_power_w"] == {"addr": RATED_POWER, "type": "i32", "fc": 4}
    assert profile.raw["limits"]["rated_power_register"] == "rated_power_w"
    covered = {a for b in profile.raw["modbus"]["identify_reads"] if b.get("fc") == 4
               for a in range(b["addr"], b["addr"] + b["count"])}
    for spec in (ident["model_register"], *ident["registers"].values()):
        width = spec.get("len", 2 if spec["type"] in ("u32", "i32") else 1)
        assert set(range(spec["addr"], spec["addr"] + width)) <= covered


def test_rated_power_outside_range_is_unknown_not_a_mismatch(profile):
    # 33100-33101 nie jest na pewno mocą z tabliczki: wartość poza 1–30 kW = brak mocy (onboarding pyta).
    info = identity_info(profile, _image(**{str(RATED_POWER): 0, str(RATED_POWER + 1): 0}))
    assert info["matched"] and info["rated_power_w"] is None


@pytest.mark.parametrize("serial", [
    "0602AB1234567890", "010F12345678901", "110F12345678901", "114F12345678901",  # S5/RHI 1P 48 V
    "134F12345678901", "140C12345678901", "1431234567890AB", "160F312345678901",
    "160F412345678901", "160F512345678901", "6031123456789AB", "6041123456789AB",
    "1031123456789AB", "2051123456789AB",                                          # EO1P
    "103305123456789", "103306123456789", "103314123456789", "103316123456789",    # S6-EH3P HV
    "103330123456789", "110C12345678901", "114C12345678901", "1053123456789AB",
])
def test_identify_accepts_hybrid_serial_prefixes(profile, serial):
    pg.assert_identify_matches(profile, _image(**_serial_words(serial)))


@pytest.mark.parametrize("serial", [
    "180112345678901", "180212345678901",  # S6-GR1P (sieciowy)
    "180512345678901",                     # sieciowy Gen5 3P
    "010212345678901",                     # nieopisany (AC?) w solax_modbus
    "SP1ES110N6000001", "XYZ0000000000000",
])
def test_identify_rejects_grid_tied_and_unknown_serials(profile, serial):
    pg.assert_identify_rejects(profile, _image(**_serial_words(serial)))


def test_reads_decode_reference_state(profile, image):
    values = pg.decode_reads(profile, image)
    assert set(values) == set(profile.raw["read"])
    assert set(values) == set(REFERENCE)  # brak rejestru temperatury baterii w źródłach
    for key, want in REFERENCE.items():
        assert values[key] == pytest.approx(want), key


def test_power_balance_of_reference_state(profile, image):
    v = pg.decode_reads(profile, image)
    assert v["pv_power_w"] + v["battery_power_w"] + v["grid_power_w"] == v["load_power_w"]
    assert v["load_power_w"] - v["grid_power_w"] == v["active_power_w"]


def test_golden_direction_register_agrees_with_battery_sign(image):
    # 33135: 0 = ładowanie, 1 = rozładowanie — obraz musi być spójny z ujemną mocą baterii.
    assert image.words(DIRECTION, 1, 4) == [0]


def test_register_map_input_measurements_and_holding_mode(profile):
    read = profile.raw["read"]
    for key, spec in read.items():
        want_fc = 3 if key == "mode_value" else 4
        assert spec.get("fc", 3) == want_fc, key
        if spec["type"] in ("u32", "i32"):
            assert spec.get("word_order", "hi_lo") == "hi_lo", key
    assert read["soc"] == {"addr": 33139, "type": "u16", "fc": 4}
    assert read["battery_power_w"] == {"addr": 33149, "type": "i32", "sign": -1, "fc": 4}
    assert read["battery_current_a"] == {"addr": 33134, "type": "i16", "scale": 0.1, "sign": -1, "fc": 4}
    assert read["grid_power_w"] == {"addr": 33130, "type": "i32", "sign": -1, "fc": 4}
    assert read["pv_power_w"] == {"addr": 33057, "type": "u32", "fc": 4}
    assert read["load_power_w"] == {"addr": 33147, "type": "u16", "fc": 4}
    assert read["mode_value"] == {"addr": STORAGE_SWITCH, "type": "u16"}


def test_static_consistency(profile):
    pg.assert_intents_consistent(profile)
    pg.assert_ha_regexes_compile(profile)
    pg.assert_capabilities_match_writes(profile)


def test_control_limited_to_self_use_bit(profile):
    raw = profile.raw
    # 43110 to pole bitowe: 1 = sam bit 0 (Self-Use), bez ładowania z sieci i bez slotów czasowych.
    # Ładowanie z sieci = sloty godz./min + prąd w A, wymuszenie = drugi rejestr 43135 z mocą x10 W
    # i podtrzymaniem — luki silnika. Zostaje powrót do samego Self-Use.
    assert set(raw["write"]) == {"mode"}
    assert raw["write"]["mode"] == {"addr": STORAGE_SWITCH, "type": "u16", "encode": "mode"}
    assert raw["modes"] == {
        "self_use": {"value": 1, "direction": "neutral", "ha_option": "Self-Use - No Grid Charging"}}
    neutral = {"mode": raw["neutral_mode"], "power": "none"}
    for intent in ("charge_grid", "discharge_forced", "sell", "standby", "self_consume", "charge_pv"):
        assert raw["intents"][intent] == neutral, intent
    caps = raw["capabilities"]
    for cap in ("force_charge_from_grid", "sell_from_battery", "force_discharge", "standby",
                "set_power_w", "limit_export", "set_soc_floor", "set_soc_ceiling"):
        assert caps[cap] is False, cap
    assert caps["time_windows"] == 0
    assert raw["neutral_mode"] == raw["baseline"]["mode"] == "self_use"
    assert raw["write_policy"]["nvm"] is True


def test_write_read_back_at_same_address_and_verify_blocks_cover_writes(profile):
    m = profile.raw["modbus"]
    addrs = {spec["addr"] for spec in profile.raw["write"].values()}
    covered = {a for b in m["verify_blocks"] if b.get("fc", 3) == 3
               for a in range(b["addr"], b["addr"] + b["count"])}
    assert addrs <= covered
    assert set(m["probe_keys"]) == set(profile.raw["write"])
    # 43110 jest RW (holding) i czytany pod tym samym adresem — nie echo-only.
    assert "echo_only" not in m
    assert m["write_function"] == 6
    assert profile.raw["unit_id"] == 1
    assert profile.raw["transports"] == ["solarman_v5", "modbus_rtu"]
    assert m["transport_options"]["solarman_v5"]["port"] == 8899
    assert set(m["transport_options"]) == {"solarman_v5", "modbus_rtu"}
    assert "re-writing" in m["status_note"] and "43110" in m["status_note"]


def _ents(profile, domain):
    integ = next(i for i in profile.raw["ha"]["integrations"] if i["domain"] == domain)
    assert integ["ems"] is False and integ["status"] == "draft"
    return integ["entities"]


def _check(ents, samples, near_misses):
    assert set(ents) == set(samples)
    for key, uid in samples.items():
        assert re.search(ents[key]["unique_id_regex"], uid), (key, uid)
    for key, uids in near_misses.items():
        for uid in uids:
            assert not re.search(ents[key]["unique_id_regex"], uid), (key, uid)


SIGNED_NEGATED = ("battery_power_w", "battery_current_a", "grid_power_w")


def test_ha_entity_map_matches_solarman_unique_ids(profile):
    ents = _ents(profile, "solarman")
    # davidrapan/ha-solarman: "<entry_id>_<slug nazwy>_<platforma>".
    entry = "01j9q4zt6v2m8k3x7b5n0c1d2e"
    dr = {
        "soc": f"{entry}_battery_sensor",
        "battery_power_w": f"{entry}_battery_power_sensor",
        "battery_voltage_v": f"{entry}_battery_voltage_sensor",
        "battery_current_a": f"{entry}_battery_current_sensor",
        "pv_power_w": f"{entry}_inverter_dc_power_sensor",
        "grid_power_w": f"{entry}_meter_active_power_sensor",
        "load_power_w": f"{entry}_house_load_power_sensor",
        "active_power_w": f"{entry}_inverter_active_power_sensor",
        "pv_energy_total_kwh": f"{entry}_total_production_sensor",
        "grid_import_total_kwh": f"{entry}_total_energy_import_sensor",
        "grid_export_total_kwh": f"{entry}_total_energy_export_sensor",
    }
    # StephanJoubert/home_assistant_solarman: "<nazwa>_<numer loggera>_<nazwa pola>".
    sj = {
        "soc": "Inverter_2712345678_Battery SOC",
        "battery_power_w": "Inverter_2712345678_Battery Power",
        "battery_voltage_v": "Inverter_2712345678_Battery Voltage",
        "battery_current_a": "Inverter_2712345678_Battery Current",
        "pv_power_w": "Inverter_2712345678_Inverter DC Power",
        "grid_power_w": "Inverter_2712345678_Meter Active Power",
        "load_power_w": "Inverter_2712345678_House Load Power",
        "active_power_w": "Inverter_2712345678_Inverter Active Power",
        "pv_energy_total_kwh": "Inverter_2712345678_Total Generation",
        "grid_import_total_kwh": "Inverter_2712345678_Total Energy Imported",
        "grid_export_total_kwh": "Inverter_2712345678_Total Energy Exported",
    }
    near = {
        "soc": (f"{entry}_battery_soh_sensor", f"{entry}_battery_state_sensor",
                "Inverter_2712345678_Battery SOH"),
        "battery_power_w": (f"{entry}_battery_power_losses_sensor",),
        "pv_power_w": (f"{entry}_pv_power_sensor", f"{entry}_inverter_ac_power_sensor"),
        "grid_power_w": (f"{entry}_meter_reactive_power_sensor",),
        "load_power_w": (f"{entry}_backup_load_power_sensor", "Inverter_2712345678_Backup Load Power"),
    }
    _check(ents, dr, near)
    for key, uid in sj.items():
        assert re.search(ents[key]["unique_id_regex"], uid), (key, uid)
    # Obie integracje zostawiają znaki producenta (bateria/prąd + przy ładowaniu, sieć + przy oddawaniu).
    for key in SIGNED_NEGATED:
        assert ents[key].get("transform") == "negate", key
    # Select davidrapan nie ma opcji dla wartości 1 (jego "Self Use" = 0x21) — brak klucza `mode`.
    assert "mode" not in ents


def test_ha_entity_map_matches_solax_modbus_solis_unique_ids(profile):
    ents = _ents(profile, "solax_modbus")
    # Wtyczka Solis integracji solax_modbus: "<nazwa huba>_<klucz>".
    samples = {
        "soc": "Solis_battery_soc",
        "battery_power_w": "Solis_battery_power",
        "battery_voltage_v": "Solis_battery_voltage",
        "battery_current_a": "Solis_battery_current",
        "pv_power_w": "Solis_pv_total_power",
        "grid_power_w": "Solis_meter_active_power",
        "load_power_w": "Solis_house_load",
        "active_power_w": "Solis_active_power",
        "pv_energy_total_kwh": "Solis_power_generation_total",
        "grid_import_total_kwh": "Solis_grid_import_total",
        "grid_export_total_kwh": "Solis_grid_export_total",
        "mode": "Solis_energy_storage_control_switch",
    }
    near = {
        "soc": ("Solis_battery_soh",),
        "battery_power_w": ("Solis_battery_power_charge",),
        "grid_power_w": ("Solis_meter_active_power_total", "Solis_meter_active_power_l1"),
        "pv_power_w": ("Solis_pv_power_1",),
        "active_power_w": ("Solis_meter_active_power",),
    }
    _check(ents, samples, near)
    assert ents["mode"]["domain"] == "select"
    for key in SIGNED_NEGATED:
        assert ents[key].get("transform") == "negate", key


def test_ha_entity_map_matches_solis_modbus_unique_ids(profile):
    ents = _ents(profile, "solis_modbus")
    # Pho3niX90/solis_modbus: "solis_modbus_<serial>_<klucz>".
    p = "solis_modbus_110F12345678901_solis_modbus_inverter"
    samples = {
        "soc": f"{p}_battery_soc",
        "battery_power_w": f"{p}_battery_power",
        "battery_voltage_v": f"{p}_battery_voltage",
        "battery_current_a": f"{p}_battery_current",
        "pv_power_w": f"{p}_total_dc_output",
        "grid_power_w": f"{p}_meter_active_power",
        "load_power_w": f"{p}_household_load_power",
        "active_power_w": f"{p}_active_power",
        "pv_energy_total_kwh": f"{p}_pv_total_generation",
        "grid_import_total_kwh": f"{p}_total_energy_imported_from_grid",
        "grid_export_total_kwh": f"{p}_total_energy_fed_into_grid",
    }
    near = {
        "soc": (f"{p}_battery_soh",),
        "battery_power_w": (f"{p}_battery_power_combined", f"{p}_battery_charge_power"),
        "grid_power_w": (f"{p}_meter_active_power_a", f"{p}_meter_total_active_power"),
        "active_power_w": (f"{p}_meter_active_power",),
    }
    _check(ents, samples, near)
    for key in SIGNED_NEGATED:
        assert ents[key].get("transform") == "negate", key
    # Select 43110 tej integracji robi odczyt-zmianę-zapis z innymi nazwami opcji — brak `mode`.
    assert "mode" not in ents


def test_engine_gaps_document_solis():
    text = GAPS_FILE.read_text(encoding="utf-8")
    assert SECTION in text
    section = text.split(SECTION, 1)[1].split("\n## ", 1)[0]
    for needle in ("43110", "43141", "43143", "43135", "43136", "43129", "43114", "43024", "43011",
                   "43010", "33135", "33149", "33100", "solarman", "solis_modbus"):
        assert needle in section, needle
    assert text.index(SECTION) < text.index("## Automatic verification ladder")
    ladder = text.split("## Automatic verification ladder", 1)[1]
    assert "solis-hybrid" in ladder
