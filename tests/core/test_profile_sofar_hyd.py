"""Profil sofar-hyd (draft): identyfikacja, odczyty i spójność na syntetycznym obrazie golden."""
import json
import re
from pathlib import Path

import pytest

from custom_components.volcast.core.modbus.identity import identity_info
from custom_components.volcast.core.profile import load_builtin
from custom_components.volcast.core.profile_schema import validate_profile
from tests.core import profile_golden as pg
from tests.core.modbus.helpers import golden_image, goodwe_image

PID = "sofar-hyd"
ROOT = Path(__file__).resolve().parents[2]
PROFILE_FILE = ROOT / "custom_components" / "volcast" / "profiles" / f"{PID}.json"
GAPS_FILE = ROOT / "docs" / "profiles" / "engine-gaps.md"
SECTION = "## Sofar (`sofar-hyd`)"
# Stan wzorcowy obrazu (README katalogu golden): konwencja rdzenia — bateria dodatnia = rozładowanie,
# sieć dodatnia = pobór. Sofar odwrotnie w obu: 0x0606 dodatnie przy ładowaniu, 0x0488 (PCC)
# dodatnie przy oddawaniu (stąd sign -1). Moce w 0,01 kW, PV w 0,1 kW, liczniki u32 starsze słowo pierwsze.
REFERENCE = {
    "soc": 55,
    "pv_power_w": 3200,
    "battery_power_w": -1500,
    "grid_power_w": 400,
    "load_power_w": 2100,
    "active_power_w": 1700,
    "battery_temp_c": 24,
    "battery_voltage_v": 400,
    "battery_current_a": -3.75,
    "pv_energy_total_kwh": 12345.6,
    "grid_import_total_kwh": 4321,
    "grid_export_total_kwh": 6789,
    "mode_value": 0,
}
# Adres z ramki = adres szesnastkowy dokumentu producenta (bez przesunięcia o 1).
ENERGY_STORAGE_MODE = 0x1110
SERIAL = 0x0445
RATED_POWER = 0x06ED


def _serial_words(text: str) -> dict[str, int]:
    raw = text.encode("ascii").ljust(16, b"\0")
    return {str(SERIAL + i): (raw[2 * i] << 8) | raw[2 * i + 1] for i in range(8)}


# Odnośniki, których profil w publicznym repo nie może cytować: załączniki wrzucane na GitHub
# (user-attachments) i pliki wgrane na fora (iobroker, loxforum) bywają kopiami dokumentów producenta
# z zakazem redystrybucji; linki do dyskusji są dozwolone.
NON_PUBLIC_REF_MARKERS = (
    "user-attachments",
    "forum.iobroker.net/assets",
    "loxforum.com/filedata",
    "/assets/uploads/files",
)


def assert_sources_public(raw: dict) -> None:
    """Każde `sources[].ref` i oba `status_note` bez odnośników do niepublicznych kopii dokumentów."""
    texts = [s["ref"] for s in raw["sources"]] + [s["what"] for s in raw["sources"]]
    texts += [raw.get("status_note", ""), raw.get("modbus", {}).get("status_note", "")]
    for text in texts:
        for marker in NON_PUBLIC_REF_MARKERS:
            assert marker not in text, f"niepubliczny odnośnik ({marker}): {text[:80]}"


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
    # Konwencja adresów musi być nazwana: adres z ramki = adres szesnastkowy producenta.
    assert "wire (PDU) addresses" in note
    assert "0x0608 = 1544" in note
    # Wariant objęty i nieobjęty (jednofazowe HYD-ES/EP mają inną mapę albo nie są sprawdzone).
    assert "HYD 5-20KTL-3PH" in note and "not covered" in note and "HYD 3-6K-ES" in note


def test_sources_are_public_and_carry_rated_power(profile):
    raw = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
    assert_sources_public(raw)
    # Rejestr mocy znamionowej musi mieć własne publiczne źródło.
    assert any("0x06ED" in s["what"] and s["ref"].endswith("/rating.py") for s in raw["sources"])


def test_identify_matches_golden_and_rejects_other_brands(profile, image):
    pg.assert_identify_matches(profile, image)
    for other in ("deye-sg", "huawei-sun2000", "sungrow-sh", "solax-x-hybrid"):
        pg.assert_identify_rejects(profile, golden_image(other))
    pg.assert_identify_rejects(profile, goodwe_image())


def test_identify_reads_serial_model_and_rated_power(profile, image):
    info = identity_info(profile, image)
    assert info["model"] == "SP1ES110FAKE0000"
    assert info["rated_power_w"] == 10000
    ident = profile.raw["identify"]
    assert ident["model_register"] == {"addr": SERIAL, "type": "ascii", "len": 8}
    assert ident["registers"]["serial"] == {"addr": SERIAL, "type": "ascii", "len": 8}
    assert ident["registers"]["rated_power_w"] == {"addr": RATED_POWER, "type": "u16", "scale": 100}
    covered = {a for b in profile.raw["modbus"]["identify_reads"] for a in range(b["addr"], b["addr"] + b["count"])}
    for spec in (ident["model_register"], *ident["registers"].values()):
        assert spec.get("fc", 3) == 3
        assert set(range(spec["addr"], spec["addr"] + spec.get("len", 1))) <= covered


@pytest.mark.parametrize("serial", [
    "SP1ES110N6000001", "SP1ES120N6000001", "SP2ES108N6000001",  # HYD xxKTL-3PH
    "SH1ES105N6000001",                                          # HYD 5-8KTL-3PH
    "ZP1ES110N6000001", "ZP2ES110N6000001",                      # ZCS Azzurro 3PH HYD ZSS
])
def test_identify_accepts_three_phase_hybrids(profile, serial):
    pg.assert_identify_matches(profile, golden_image(PID, **_serial_words(serial)))


@pytest.mark.parametrize("serial", [
    "SM2ES136N6000001", "SM2ES436N6000001", "ZM2ES160N6000001",  # jednofazowe HYD-ES/EP (nieobjęte)
    "SM1ES136N6000001", "ZM1ES136N6000001",                      # stary protokół HYD-ES
    "SH3ES146N6000001", "SS2ES150N6000001", "ZS2ES112N6000001",  # falowniki sieciowe G3
    "SQ1ES1A0N6000001", "SA1ES136N6000001", "SC1ES136N6000001",  # sieciowe starszych linii
    "SF4ES136N6000001", "SL1ES136N6000001", "SJ2ES136N6000001", "SS1ES136N6000001",
])
def test_identify_rejects_other_sofar_lines(profile, serial):
    pg.assert_identify_rejects(profile, golden_image(PID, **_serial_words(serial)))


def test_reads_decode_reference_state(profile, image):
    values = pg.decode_reads(profile, image)
    assert set(values) == set(profile.raw["read"])
    for key, want in REFERENCE.items():
        assert values[key] == pytest.approx(want), key


def test_power_balance_of_reference_state(profile, image):
    v = pg.decode_reads(profile, image)
    assert v["pv_power_w"] + v["battery_power_w"] + v["grid_power_w"] == v["load_power_w"]
    assert v["load_power_w"] - v["grid_power_w"] == v["active_power_w"]


def test_register_map_is_holding_only_with_vendor_scales(profile):
    read = profile.raw["read"]
    for key, spec in read.items():
        assert spec.get("fc", 3) == 3, key  # Sofar: wszystko w rejestrach holding (FC 3)
        if spec["type"] in ("u32", "i32"):
            assert spec.get("word_order", "hi_lo") == "hi_lo", key
    assert read["soc"] == {"addr": 0x0608, "type": "u16"}
    assert read["battery_power_w"] == {"addr": 0x0606, "type": "i16", "scale": 10, "sign": -1}
    assert read["grid_power_w"] == {"addr": 0x0488, "type": "i16", "scale": 10, "sign": -1}
    assert read["pv_power_w"] == {"addr": 0x05C4, "type": "u16", "scale": 100}
    assert read["load_power_w"] == {"addr": 0x04AF, "type": "u16", "scale": 10}
    assert read["battery_temp_c"] == {"addr": 0x0607, "type": "i16"}
    assert read["mode_value"] == {"addr": ENERGY_STORAGE_MODE, "type": "u16"}


def test_static_consistency(profile):
    pg.assert_intents_consistent(profile)
    pg.assert_ha_regexes_compile(profile)
    pg.assert_capabilities_match_writes(profile)


def test_control_limited_to_energy_storage_mode(profile):
    raw = profile.raw
    # Tryb pasywny = blok trzech i32 (0x1187–0x118C) pisany razem; tryb czasowy = sloty start/koniec
    # z mocą u32 i rejestrem zatwierdzenia — luki silnika. Zostaje powrót do Self Use (0).
    assert set(raw["write"]) == {"mode"}
    assert raw["write"]["mode"] == {"addr": ENERGY_STORAGE_MODE, "type": "u16", "encode": "mode"}
    assert raw["modes"] == {"self_use": {"value": 0, "direction": "neutral", "ha_option": "Self Use"}}
    neutral = {"mode": raw["neutral_mode"], "power": "none"}
    for intent in ("charge_grid", "discharge_forced", "sell", "standby", "self_consume", "charge_pv"):
        assert raw["intents"][intent] == neutral, intent
    caps = raw["capabilities"]
    for cap in ("force_charge_from_grid", "sell_from_battery", "force_discharge", "standby",
                "set_power_w", "limit_export", "set_soc_floor", "set_soc_ceiling"):
        assert caps[cap] is False, cap
    assert caps["time_windows"] == 0
    assert raw["neutral_mode"] == raw["baseline"]["mode"] == "self_use"
    assert raw["write_policy"]["nvm"] is True  # 0x1110 to nastawa RW (nie „V”), ostrzeżenie o EEPROM


def test_write_read_back_at_same_address_and_verify_blocks_cover_writes(profile):
    m = profile.raw["modbus"]
    addrs = {spec["addr"] for spec in profile.raw["write"].values()}
    covered = {a for b in m["verify_blocks"] for a in range(b["addr"], b["addr"] + b["count"])}
    assert addrs <= covered
    assert set(m["probe_keys"]) == set(profile.raw["write"])
    # 0x1110 jest RW: odczyt zwrotny pod adresem zapisu potwierdza tryb, więc nie echo-only.
    assert "echo_only" not in m
    assert m["write_function"] == 16
    assert profile.raw["unit_id"] == 1
    assert profile.raw["transports"] == ["solarman_v5", "modbus_rtu"]
    assert m["transport_options"]["solarman_v5"]["port"] == 8899
    assert set(m["transport_options"]) == {"solarman_v5", "modbus_rtu"}


def _ents(profile, domain):
    integ = next(i for i in profile.raw["ha"]["integrations"] if i["domain"] == domain)
    assert integ["ems"] is False and integ["status"] == "draft"
    return integ["entities"]


def test_ha_entity_map_matches_solarman_unique_ids(profile):
    ents = _ents(profile, "solarman")
    # davidrapan/ha-solarman: "<entry_id>_<slug nazwy>_<platforma>" (małe litery).
    dr = {
        "soc": "01j9q4zt6v2m8k3x7b5n0c1d2e_battery_sensor",
        "battery_power_w": "01j9q4zt6v2m8k3x7b5n0c1d2e_battery_power_sensor",
        "battery_temp_c": "01j9q4zt6v2m8k3x7b5n0c1d2e_battery_temperature_sensor",
        "battery_voltage_v": "01j9q4zt6v2m8k3x7b5n0c1d2e_battery_voltage_sensor",
        "battery_current_a": "01j9q4zt6v2m8k3x7b5n0c1d2e_battery_current_sensor",
        "pv_power_w": "01j9q4zt6v2m8k3x7b5n0c1d2e_pv_power_sensor",
        "grid_power_w": "01j9q4zt6v2m8k3x7b5n0c1d2e_activepower_pcc_total_sensor",
        "load_power_w": "01j9q4zt6v2m8k3x7b5n0c1d2e_activepower_load_sys_sensor",
        "active_power_w": "01j9q4zt6v2m8k3x7b5n0c1d2e_activepower_output_total_sensor",
        "pv_energy_total_kwh": "01j9q4zt6v2m8k3x7b5n0c1d2e_total_production_sensor",
        "grid_import_total_kwh": "01j9q4zt6v2m8k3x7b5n0c1d2e_total_energy_import_sensor",
        "grid_export_total_kwh": "01j9q4zt6v2m8k3x7b5n0c1d2e_total_energy_export_sensor",
        "mode": "01j9q4zt6v2m8k3x7b5n0c1d2e_storage_control_mode_select",
    }
    # StephanJoubert/home_assistant_solarman: "<nazwa>_<numer loggera>_<nazwa pola>".
    sj = {
        "soc": "Inverter_2712345678_Battery 1 SOC",
        "battery_temp_c": "Inverter_2712345678_Battery 1 Temperature",
        "battery_voltage_v": "Inverter_2712345678_Battery 1 Voltage",
        "battery_current_a": "Inverter_2712345678_Battery 1 Current",
        "load_power_w": "Inverter_2712345678_ActivePower_Load_Sys",
        "active_power_w": "Inverter_2712345678_ActivePower_Output_Total",
        "pv_energy_total_kwh": "Inverter_2712345678_Total PV Generation",
        "grid_import_total_kwh": "Inverter_2712345678_Total Energy Bought",
        "grid_export_total_kwh": "Inverter_2712345678_Total Energy Sold",
    }
    assert set(ents) == set(dr)
    for key, uid in dr.items():
        assert re.search(ents[key]["unique_id_regex"], uid), key
    for key, uid in sj.items():
        assert re.search(ents[key]["unique_id_regex"], uid), key
    # Odwrotne znaki w obu integracjach: davidrapan odwraca baterię i sieć do konwencji rdzenia,
    # StephanJoubert nie — jeden wpis na domenę, więc te klucze trafiają tylko encje davidrapan.
    for uid in ("Inverter_2712345678_Battery 1 Power", "Inverter_2712345678_ActivePower_PCC_Total"):
        for key in ("battery_power_w", "grid_power_w"):
            assert not re.search(ents[key]["unique_id_regex"], uid), (key, uid)
    for key in ("battery_power_w", "grid_power_w"):
        assert "transform" not in ents[key], key
    assert ents["battery_current_a"].get("transform") == "negate"  # obie: prąd ładowania dodatni
    assert ents["mode"]["domain"] == "select"
    near_misses = {
        "soc": ("01j9q4zt6v2m8k3x7b5n0c1d2e_battery_soh_sensor", "Inverter_2712345678_Battery 1 SOH",
                "Inverter_2712345678_Battery 2 SOC"),
        "pv_power_w": ("01j9q4zt6v2m8k3x7b5n0c1d2e_pv1_power_sensor",),
        "grid_power_w": ("01j9q4zt6v2m8k3x7b5n0c1d2e_activepower_pcc_r_sensor",),
        "load_power_w": ("01j9q4zt6v2m8k3x7b5n0c1d2e_activepower_load_total_eps_sensor",),
    }
    for key, uids in near_misses.items():
        for uid in uids:
            assert not re.search(ents[key]["unique_id_regex"], uid), (key, uid)


def test_ha_entity_map_matches_solax_modbus_sofar_unique_ids(profile):
    ents = _ents(profile, "solax_modbus")
    # Wtyczka Sofar integracji solax_modbus: "<nazwa huba>_<klucz>".
    samples = {
        "soc": "Sofar_battery_capacity_1",
        "battery_power_w": "Sofar_battery_power_1",
        "battery_temp_c": "Sofar_battery_temperature_1",
        "battery_voltage_v": "Sofar_battery_voltage_1",
        "battery_current_a": "Sofar_battery_current_1",
        "pv_power_w": "Sofar_pv_power_total",
        "grid_power_w": "Sofar_active_power_pcc_total",
        "load_power_w": "Sofar_active_power_load_sys",
        "active_power_w": "Sofar_active_power_output_total",
        "pv_energy_total_kwh": "Sofar_solar_generation_total",
        "grid_import_total_kwh": "Sofar_import_energy_total",
        "grid_export_total_kwh": "Sofar_export_energy_total",
        "mode": "Sofar_charger_use_mode",
    }
    assert set(ents) == set(samples)
    for key, uid in samples.items():
        assert re.search(ents[key]["unique_id_regex"], uid), key
    assert ents["mode"]["domain"] == "select"
    for key in ("battery_power_w", "grid_power_w", "battery_current_a"):
        assert ents[key].get("transform") == "negate", key
    near_misses = {
        "soc": ("Sofar_battery_capacity_2",),
        "battery_power_w": ("Sofar_battery_power_2",),
        "grid_power_w": ("Sofar_active_power_pcc_l1",),
        "pv_power_w": ("Sofar_pv_power_1",),
    }
    for key, uids in near_misses.items():
        for uid in uids:
            assert not re.search(ents[key]["unique_id_regex"], uid), (key, uid)


def test_engine_gaps_document_sofar():
    text = GAPS_FILE.read_text(encoding="utf-8")
    assert SECTION in text
    section = text.split(SECTION, 1)[1].split("\n## ", 1)[0]
    for needle in ("0x1110", "0x1187", "0x1184", "0x1111", "0x111F", "0x112F", "0x1023", "0x1024",
                   "0x104D", "0x1053", "solarman"):
        assert needle in section, needle
    assert text.index(SECTION) < text.index("## Automatic verification ladder")
    ladder = text.split("## Automatic verification ladder", 1)[1]
    assert "sofar-hyd" in ladder
