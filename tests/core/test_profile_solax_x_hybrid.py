"""Profil solax-x-hybrid (draft): identyfikacja, odczyty i spójność na syntetycznym obrazie golden."""
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

PID = "solax-x-hybrid"
ROOT = Path(__file__).resolve().parents[2]
PROFILE_FILE = ROOT / "custom_components" / "volcast" / "profiles" / f"{PID}.json"
GAPS_FILE = ROOT / "docs" / "profiles" / "engine-gaps.md"
# Stan wzorcowy obrazu (README katalogu golden): konwencja rdzenia — bateria dodatnia = rozładowanie,
# sieć dodatnia = pobór. SolaX odwrotnie w obu: 0x0016 dodatnie przy ładowaniu, 0x0046 dodatnie
# przy oddawaniu (stąd sign -1); dom = moc falownika (0x0002) + pobór z sieci.
REFERENCE = {
    "soc": 55,
    "pv_power_w": 3200,
    "battery_power_w": -1500,
    "grid_power_w": 400,
    "load_power_w": 2100,
    "active_power_w": 1700,
    "battery_temp_c": 24,
    "battery_voltage_v": 300,
    "battery_current_a": -5,
    "pv_energy_total_kwh": 12345.6,
    "grid_import_total_kwh": 4321,
    "grid_export_total_kwh": 6789,
    "mode_value": 0,
}
# Adresy z ramki = adresy szesnastkowe dokumentu producenta (bez przesunięcia o 1).
CHARGER_USE_MODE_WRITE = 0x001F
CHARGER_USE_MODE_READ = 0x008B


def _serial_words(text: str) -> dict[str, int]:
    raw = text.encode("ascii").ljust(14, b"\0")
    return {str(i): (raw[2 * i] << 8) | raw[2 * i + 1] for i in range(7)}


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
    # Konwencja adresów musi być nazwana: adres z ramki = adres szesnastkowy producenta.
    assert "wire (PDU) addresses" in profile.raw["status_note"]
    assert "0x001C = 28" in profile.raw["status_note"]


def test_identify_matches_golden_and_rejects_other_brands(profile, image):
    pg.assert_identify_matches(profile, image)
    pg.assert_identify_rejects(profile, golden_image("deye-sg"))
    pg.assert_identify_rejects(profile, golden_image("huawei-sun2000"))
    pg.assert_identify_rejects(profile, golden_image("sungrow-sh"))
    pg.assert_identify_rejects(profile, goodwe_image())


def test_identify_reads_serial_model_and_rated_power(profile, image):
    info = identity_info(profile, image)
    assert info["model"] == "H34A10FAKE0000"
    assert info["rated_power_w"] == 10000
    ident = profile.raw["identify"]
    assert ident["model_register"] == {"addr": 0, "type": "ascii", "len": 7}
    assert ident["registers"]["serial"] == {"addr": 0, "type": "ascii", "len": 7}
    covered = {a for b in profile.raw["modbus"]["identify_reads"] for a in range(b["addr"], b["addr"] + b["count"])}
    for spec in (ident["model_register"], *ident["registers"].values()):
        assert spec.get("fc", 3) == 3
        assert set(range(spec["addr"], spec["addr"] + spec.get("len", 1))) <= covered


@pytest.mark.parametrize("serial", [
    "H1E5F2A0000001", "H1I3F2A0000001", "HCC3F2A0000001", "HUE5F2A0000001", "XRE3F2A0000001",  # X1 G3
    "H3DE10A0000001", "H3E10FA0000001", "H3LE08A0000001", "H3PE15A0000001", "H3UE12A0000001",  # X3 G3
    "H43037A0000001", "H44050A0000001", "H450A0A0000001", "H460A0A0000001", "H475A0A0000001",  # X1 G4
    "H34A15A0000001", "H34B08A0000001",                                                        # X3 G4
])
def test_identify_accepts_gen3_gen4_hybrids(profile, serial):
    pg.assert_identify_matches(profile, golden_image(PID, **_serial_words(serial)))


@pytest.mark.parametrize("serial", [
    "H35A10A0000001", "P35A10A0000001", "H3BC15A0000001", "H3BD20A0000001",  # X3-IES / X3-Ultra (G5)
    "H55050A0000001", "H58080A0000001",                                      # X1-IES (G5)
    "10K0000A000001", "10M0000A000001",                                      # G6 (X3 Pro / X1-VAST)
    "H31000A0000001", "631100A0000001", "631500A0000001",                    # OEM TIGO TSI
    "F34A10A0000001", "F450A0A0000001", "PRE000A0000001", "PRI000A0000001",  # RetroFit / FIT (AC)
    "L50000A0000001", "U37000A0000001", "XAC000A0000001", "XB3000A0000001",  # G2, X1-AC, X1-Boost
    "H3VC83A0000001",                                                        # dwuznaczny wpis wtyczki
])
def test_identify_rejects_other_solax_lines(profile, serial):
    pg.assert_identify_rejects(profile, golden_image(PID, **_serial_words(serial)))


def test_reads_decode_reference_state(profile, image):
    values = pg.decode_reads(profile, image)
    assert set(values) == set(profile.raw["read"])
    for key, want in REFERENCE.items():
        assert values[key] == pytest.approx(want), key


def test_power_balance_of_reference_state(profile, image):
    v = pg.decode_reads(profile, image)
    assert v["pv_power_w"] + v["battery_power_w"] + v["grid_power_w"] == v["load_power_w"]


def test_measurements_are_input_registers_and_settings_holding(profile):
    for key, spec in profile.raw["read"].items():
        if "sum" in spec:
            for part in spec["sum"]:
                if "ref" not in part:
                    assert part.get("fc") == 4, key
            continue
        want = 3 if key == "mode_value" else 4
        assert spec.get("fc", 3) == want, key
        if spec["type"] in ("u32", "i32"):
            assert spec.get("word_order") == "lo_hi", key  # SolaX: młodsze słowo pierwsze


def test_measurements_not_readable_from_holding_space(profile, image):
    # Te same słowa w przestrzeni holding (FC 3) nie mogą dać odczytu: pomiary czyta FC 4.
    doc = _golden_doc(PID)
    swapped = RegisterImage({int(a): w for a, w in doc["input_registers"].items()}, {})
    values = pg.decode_reads(profile, swapped)
    for key in ("soc", "battery_power_w", "grid_power_w", "pv_power_w"):
        assert values[key] is None, key


def test_static_consistency(profile):
    pg.assert_intents_consistent(profile)
    pg.assert_ha_regexes_compile(profile)
    pg.assert_capabilities_match_writes(profile)


def test_control_limited_to_charger_use_mode(profile):
    raw = profile.raw
    # Wymuszone ładowanie/rozładowanie = para rejestrów (0x001F = 3 i 0x0020), a sterowanie zdalne
    # (0x007C…) to zapis wielu rejestrów z czasem życia — luki silnika. Zostaje powrót do Self Use (0).
    assert set(raw["write"]) == {"mode"}
    assert raw["write"]["mode"] == {"addr": CHARGER_USE_MODE_WRITE, "type": "u16", "encode": "mode"}
    assert raw["modes"] == {"self_use": {"value": 0, "direction": "neutral", "ha_option": "Self Use Mode"}}
    neutral = {"mode": raw["neutral_mode"], "power": "none"}
    for intent in ("charge_grid", "discharge_forced", "sell", "standby", "self_consume", "charge_pv"):
        assert raw["intents"][intent] == neutral, intent
    caps = raw["capabilities"]
    for cap in ("force_charge_from_grid", "sell_from_battery", "force_discharge", "standby",
                "set_power_w", "limit_export", "set_soc_floor", "set_soc_ceiling"):
        assert caps[cap] is False, cap
    assert caps["time_windows"] == 0
    assert raw["neutral_mode"] == raw["baseline"]["mode"] == "self_use"
    # Odczyt trybu ma INNY adres niż zapis (przestrzeń odczytu holding 0x008B).
    assert raw["read"]["mode_value"] == {"addr": CHARGER_USE_MODE_READ, "type": "u16"}
    assert raw["write_policy"]["nvm"] is True  # nastawy idą do EEPROM


def test_write_confirmed_by_echo_only_and_verify_blocks_cover_writes(profile):
    m = profile.raw["modbus"]
    addrs = {spec["addr"] for spec in profile.raw["write"].values()}
    covered = {a for b in m["verify_blocks"] for a in range(b["addr"], b["addr"] + b["count"])}
    assert addrs <= covered
    assert set(m["probe_keys"]) == set(profile.raw["write"])
    # 0x001F w przestrzeni odczytu to nie tryb: odczyt zwrotny pod adresem zapisu nic nie potwierdza.
    assert m["echo_only"] == ["mode"]
    assert m["write_function"] == 6
    assert m["transport_options"]["modbus_tcp"]["port"] == 502
    assert profile.raw["unit_id"] == 1
    assert set(profile.raw["transports"]) == {"modbus_tcp", "modbus_rtu"}


def test_ha_entity_map_matches_solax_modbus_unique_ids(profile):
    integ = profile.raw["ha"]["integrations"][0]
    assert integ["domain"] == "solax_modbus" and integ["ems"] is False and integ["status"] == "draft"
    ents = integ["entities"]
    # unique_id integracji = "<nazwa huba>_<klucz>", domyślna nazwa huba "SolaX".
    samples = {
        "soc": "SolaX_battery_capacity",
        "battery_power_w": "SolaX_battery_power_charge",
        "battery_temp_c": "SolaX_battery_temperature",
        "battery_voltage_v": "SolaX_battery_voltage_charge",
        "battery_current_a": "SolaX_battery_current_charge",
        "pv_power_w": "SolaX_pv_power_total",
        "grid_power_w": "SolaX_measured_power",
        "load_power_w": "SolaX_house_load",
        "active_power_w": "SolaX_inverter_power",
        "pv_energy_total_kwh": "SolaX_total_solar_energy",
        "grid_import_total_kwh": "SolaX_grid_import_total",
        "grid_export_total_kwh": "SolaX_grid_export_total",
        "mode": "SolaX_charger_use_mode",
        "soc_min": "SolaX_selfuse_discharge_min_soc",
        "soc_max": "SolaX_battery_charge_upper_soc",
        "export_limit_w": "SolaX_export_control_user_limit",
    }
    assert set(ents) == set(samples)
    for key, uid in samples.items():
        assert re.search(ents[key]["unique_id_regex"], uid), key
    assert re.search(ents["soc_min"]["unique_id_regex"], "SolaX_battery_minimum_capacity")  # G3
    assert ents["mode"]["domain"] == "select"
    for key in ("soc_min", "soc_max", "export_limit_w"):
        assert ents[key]["domain"] == "number", key
    for key in ("battery_power_w", "grid_power_w", "battery_current_a"):
        assert ents[key].get("transform") == "negate", key
    # Sąsiednie encje integracji nie mogą trafić do złego klucza.
    near_misses = {
        "soc": ("SolaX_remaining_battery_capacity", "SolaX_bms_battery_capacity",
                "SolaX_chargeable_battery_capacity", "SolaX_battery_1_capacity_charge"),
        "battery_power_w": ("SolaX_pm_battery_power_charge", "SolaX_battery_1_power_charge"),
        "grid_power_w": ("SolaX_meter_2_measured_power", "SolaX_measured_power_l1"),
        "active_power_w": ("SolaX_pm_total_inverter_power", "SolaX_inverter_power_l1"),
        "pv_power_w": ("SolaX_pm_total_pv_power",),
        "load_power_w": ("SolaX_pm_total_house_load", "SolaX_house_load_alt"),
        "mode": ("SolaX_manual_mode_select",),
    }
    for key, uids in near_misses.items():
        for uid in uids:
            assert not re.search(ents[key]["unique_id_regex"], uid), (key, uid)
    # Odczyt trybu jest wewnętrznym sensorem integracji (bez encji) — stan niesie select.
    assert "mode_value" not in ents


def test_engine_gaps_document_solax():
    text = GAPS_FILE.read_text(encoding="utf-8")
    assert "## SolaX (`solax-x-hybrid`)" in text
    section = text.split("## SolaX (`solax-x-hybrid`)", 1)[1].split("\n## ", 1)[0]
    for needle in ("0x001F", "0x008B", "0x0020", "0x008C", "0x007C", "0x00A0", "0x0061", "0x00BA"):
        assert needle in section, needle
    # Sekcja SolaX stoi przed drabiną weryfikacji, a drabina ma wiersz SolaX.
    assert text.index("## SolaX (`solax-x-hybrid`)") < text.index("## Automatic verification ladder")
    ladder = text.split("## Automatic verification ladder", 1)[1]
    assert "solax-x-hybrid" in ladder
