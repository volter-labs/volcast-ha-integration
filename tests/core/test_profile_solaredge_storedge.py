"""Profil solaredge-storedge (draft): identyfikacja, odczyty i spójność na syntetycznym obrazie golden."""
import json
import re
import struct
from pathlib import Path

import pytest

from custom_components.volcast.core.control.conflict import CONFLICT_DOMAINS
from custom_components.volcast.core.discovery.known import INVERTER_DOMAINS
from custom_components.volcast.core.modbus.identity import MIN_SALT_BYTES, device_fingerprint, identity_info
from custom_components.volcast.core.profile import load_builtin
from custom_components.volcast.core.profile_schema import validate_profile
from tests.core import profile_golden as pg
from tests.core.modbus.helpers import GOLDEN, golden_image, goodwe_image
from tests.core.test_profile_sofar_hyd import assert_sources_public

PID = "solaredge-storedge"
ROOT = Path(__file__).resolve().parents[2]
PROFILE_FILE = ROOT / "custom_components" / "volcast" / "profiles" / f"{PID}.json"
GAPS_FILE = ROOT / "docs" / "profiles" / "engine-gaps.md"
SECTION = "## SolarEdge (`solaredge-storedge`)"
SALT = b"s" * MIN_SALT_BYTES

# Adresy z ramki (PDU). Blok SunSpec od 40000 ("SunS"), model 1 od 40002, model 101/103 od 40069,
# licznik 1 (model 203) od 40188; blok magazynu i baterii 1 to numery szesnastkowe bez przesunięcia.
MODEL = 40020
SERIAL = 40052
INVERTER_DID = 40069
AC_POWER, AC_POWER_SF = 40083, 40084
DC_POWER, DC_POWER_SF = 40100, 40101
METER_POWER, METER_POWER_SF = 40206, 40210
METER_EXPORTED, METER_IMPORTED, METER_ENERGY_SF = 40226, 40234, 40242
STORAGE_CTRL_MODE = 0xE004          # 57348
REMOTE_CMD_MODE = 0xE00D            # 57357
BATTERY_TEMP_AVG = 0xE16C           # 57708
BATTERY_VOLTAGE = 0xE170            # 57712
BATTERY_POWER = 0xE174              # 57716
BATTERY_SOE = 0xE184                # 57732

# Stan wzorcowy (README katalogu golden). Konwencja rdzenia: bateria dodatnia = rozładowanie
# (SolarEdge 0xE174 odwrotnie: dodatnie = ładowanie), sieć dodatnia = pobór (licznik SolarEdge
# odwrotnie: dodatnie = oddawanie). PV = moc DC falownika + ładowanie baterii (jak evcc).
REFERENCE = {
    "soc": 55,
    "pv_power_w": 3200,
    "battery_power_w": -1500,
    "grid_power_w": 400,
    "load_power_w": 2100,
    "active_power_w": 1700,
    "battery_temp_c": 24,
    "battery_voltage_v": 52,
    "grid_import_total_kwh": 4321.0,
    "grid_export_total_kwh": 6789.0,
    "mode_value": 1,
}


def _text(addr: int, text: str, regs: int = 16) -> dict[str, int]:
    """Napis SunSpec: 2 znaki ASCII na rejestr (starszy bajt pierwszy), dopełniony zerami."""
    raw = text.encode("ascii").ljust(2 * regs, b"\x00")
    return {str(addr + i): (raw[2 * i] << 8) | raw[2 * i + 1] for i in range(regs)}


def _f32_lo_hi(addr: int, value: float) -> dict[str, int]:
    """Float32 w kolejności słów SolarEdge dla bloku 0xE0xx/0xE1xx: młodsze słowo pod niższym adresem."""
    hi, lo = struct.unpack(">HH", struct.pack(">f", value))
    return {str(addr): lo, str(addr + 1): hi}


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
    # Konwencja adresów nazwana wprost: SunSpec od 40000 (numeracja od 1 o jeden wyżej), blok 0xE0xx bez przesunięcia.
    assert "wire (PDU) addresses" in note and "0xE004 is sent as 57348" in note and "40001" in note
    # Objęte i nieobjęte warianty.
    assert "not covered" in note
    for variant in ("battery 2", "PV-only", "SunSpec model 120"):
        assert variant in note, variant
    # Jeden klient Modbus TCP naraz, port 1502, Modbus TCP włączany w SetApp.
    assert "one Modbus TCP client" in note and "1502" in note and "SetApp" in note
    # Dynamiczny współczynnik skali jako luka z listą do próby.
    assert "dynamic" in note and "40084" in note and "40210" in note
    # Moc znamionowa: rejestr zastępczy 40069 i serial jako odcisk.
    assert "40069" in note and "rating is always unknown" in note and "serial" in note


def test_sources_are_public_and_cover_key_registers(profile):
    raw = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
    assert_sources_public(raw)
    whats = " ".join(s["what"] for s in raw["sources"])
    for reg in ("40000", "40020", "40052", "40069", "40083", "40084", "40100", "40101", "40206", "40210",
                "40226", "40234", "40242", "0xE004", "0xE00D", "0xE00B", "0xE00E", "0xE010", "0xE008",
                "0xE000", "0xE002", "0xE16C", "0xE170", "0xE174", "0xE184", "1502"):
        assert reg in whats, reg
    refs = [s["ref"] for s in raw["sources"]]
    assert any("WillCodeForCats/solaredge-modbus-multi" in r and r.endswith("/components.py") for r in refs)
    assert any("evcc-io/evcc" in r and r.endswith("/solaredge-hybrid.yaml") for r in refs)
    assert any("sunspec/models" in r for r in refs)
    assert any("binsentsu/home-assistant-solaredge-modbus" in r for r in refs)
    # binsentsu (bez licencji) tylko dla własnego interfejsu encji — żadnych adresów rejestrów ani portu.
    for s in raw["sources"]:
        if "binsentsu" in s["ref"]:
            assert not re.search(r"0x[0-9A-Fa-f]{4}|\b4\d{4}\b|\b1502\b", s["what"]), s["what"]
    # Dokumenty producenta nie zostały odczytane (strony zwracają 403) — profil ich nie cytuje.
    assert not any("solaredge.com" in r for r in refs)


def test_identify_matches_golden_and_rejects_other_brands(profile, image):
    pg.assert_identify_matches(profile, image)
    others = sorted(p.name.replace("_", "-") for p in GOLDEN.iterdir()
                    if (p / "registers.json").exists() and p.name != PID.replace("-", "_"))
    assert {"deye-sg", "huawei-sun2000", "sungrow-sh", "foxess-h"} <= set(others)
    for other in others:
        pg.assert_identify_rejects(profile, golden_image(other))
    pg.assert_identify_rejects(profile, goodwe_image())


def test_identity_model_rating_and_fingerprint(profile, image):
    info = identity_info(profile, image)
    assert info["model"] == "SE10K-RWB48BFN4"
    # Brak modelu SunSpec 120 (WRtg): 40069 to identyfikator modelu falownika (101/102/103), nigdy 1–30 kW.
    assert info["rated_power_w"] is None
    ident = profile.raw["identify"]
    assert ident["model_register"] == {"addr": MODEL, "type": "ascii", "len": 16}
    assert ident["registers"]["serial"] == {"addr": SERIAL, "type": "ascii", "len": 16}
    assert ident["registers"]["rated_power_w"] == {"addr": INVERTER_DID, "type": "u16"}
    assert profile.raw["limits"]["rated_power_register"] == "rated_power_w"
    covered = {a for b in profile.raw["modbus"]["identify_reads"] if b.get("fc", 3) == 3
               for a in range(b["addr"], b["addr"] + b["count"])}
    for spec in (ident["model_register"], *ident["registers"].values()):
        width = spec.get("len", 2 if spec["type"] in ("u32", "i32", "f32") else 1)
        assert set(range(spec["addr"], spec["addr"] + width)) <= covered
    # Odcisk z seriala: jest, zależy od seriala, a pusty serial daje tożsamość nieznaną.
    fp = device_fingerprint(SALT, profile, image)
    assert fp is not None
    other = golden_image(PID, **_text(SERIAL, "FAKE0002SE"))
    assert device_fingerprint(SALT, profile, other) not in (None, fp)
    blank = golden_image(PID, **{str(SERIAL + i): 0 for i in range(16)})
    assert device_fingerprint(SALT, profile, blank) is None


@pytest.mark.parametrize("did", [101, 102, 103, 0xFFFF, 0])
def test_inverter_model_id_never_reads_as_a_rating(profile, did):
    info = identity_info(profile, golden_image(PID, **{str(INVERTER_DID): did}))
    assert info["matched"] and info["rated_power_w"] is None


@pytest.mark.parametrize("model", [
    "SE10K-RWB48BFN4", "SE5K-RWB48BFN4", "SE8K-RWB48", "SE3500H-RW000BNN4", "SE7600H-US",
])
def test_identify_accepts_solaredge_models(profile, model):
    # Model nie odróżnia falownika z baterią od falownika bez baterii (status_note).
    pg.assert_identify_matches(profile, golden_image(PID, **_text(MODEL, model)))


@pytest.mark.parametrize("model", ["Primo GEN24 6.0 Plus", "SUN2000-10KTL-M1", "SMA STP 10.0", "se10k", ""])
def test_identify_rejects_other_sunspec_models(profile, model):
    pg.assert_identify_rejects(profile, golden_image(PID, **_text(MODEL, model)))


def test_reads_decode_reference_state(profile, image):
    values = pg.decode_reads(profile, image)
    assert set(values) == set(profile.raw["read"])
    assert set(values) == set(REFERENCE)
    for key, want in REFERENCE.items():
        assert values[key] == pytest.approx(want), key


def test_power_balance_of_reference_state(profile, image):
    v = pg.decode_reads(profile, image)
    assert v["pv_power_w"] + v["battery_power_w"] + v["grid_power_w"] == pytest.approx(v["load_power_w"])
    assert v["load_power_w"] - v["grid_power_w"] == pytest.approx(v["active_power_w"])


def test_register_map(profile):
    read = profile.raw["read"]
    f32 = {"type": "f32", "word_order": "lo_hi"}
    assert read["soc"] == {"addr": BATTERY_SOE, **f32}
    assert read["battery_power_w"] == {"addr": BATTERY_POWER, **f32, "sign": -1}
    assert read["battery_temp_c"] == {"addr": BATTERY_TEMP_AVG, **f32}
    assert read["battery_voltage_v"] == {"addr": BATTERY_VOLTAGE, **f32}
    # SunSpec: wartość + dynamiczny SF; profil przyjmuje stały SF (luka w engine-gaps.md).
    assert read["active_power_w"] == {"addr": AC_POWER, "type": "i16", "scale": 0.1}
    assert read["grid_power_w"] == {"addr": METER_POWER, "type": "i16", "scale": 0.1, "sign": -1}
    assert read["pv_power_w"] == {"sum": [{"addr": DC_POWER, "type": "i16", "scale": 0.1},
                                          {"ref": "battery_power_w", "sign": -1}]}
    assert read["load_power_w"] == {"sum": [{"ref": "active_power_w"}, {"ref": "grid_power_w"}]}
    assert read["grid_export_total_kwh"] == {"addr": METER_EXPORTED, "type": "u32", "scale": 0.001}
    assert read["grid_import_total_kwh"] == {"addr": METER_IMPORTED, "type": "u32", "scale": 0.001}
    assert read["mode_value"] == {"addr": STORAGE_CTRL_MODE, "type": "u16"}
    for key, spec in read.items():
        for part in spec.get("sum", [spec]):
            assert part.get("fc", 3) == 3, key
    # Prąd baterii pominięty: znak nieopisany w źródłach.
    assert "battery_current_a" not in read


def test_fixed_scale_ignores_the_scale_factor_register(profile, image):
    # Dokumentuje lukę: rdzeń nie czyta SF, więc zmiana SF z -1 na 0 nie zmienia wyniku (powinien 10×).
    assert image.words(AC_POWER_SF, 1)[0] == 0xFFFF and image.words(METER_POWER_SF, 1)[0] == 0xFFFF
    assert image.words(DC_POWER_SF, 1)[0] == 0xFFFF and image.words(METER_ENERGY_SF, 1)[0] == 0
    changed = golden_image(PID, **{str(AC_POWER_SF): 0})
    assert pg.decode_reads(profile, changed)["active_power_w"] == pytest.approx(1700)


def test_battery_words_are_low_word_first(profile):
    over = _f32_lo_hi(BATTERY_SOE, 80.0) | _f32_lo_hi(BATTERY_POWER, 2000.0)
    values = pg.decode_reads(profile, golden_image(PID, **over))
    assert values["soc"] == pytest.approx(80.0)
    assert values["battery_power_w"] == pytest.approx(-2000.0)      # ładowanie w konwencji rdzenia


def test_static_consistency(profile):
    pg.assert_intents_consistent(profile)
    pg.assert_ha_regexes_compile(profile)
    pg.assert_capabilities_match_writes(profile)


def test_control_storage_mode_only(profile):
    raw = profile.raw
    assert raw["write"] == {"mode": {"addr": STORAGE_CTRL_MODE, "type": "u16", "encode": "mode"}}
    assert raw["modes"] == {"max_self_consumption": {"value": 1, "direction": "neutral",
                                                     "ha_option": "Maximize Self Consumption"}}
    assert raw["neutral_mode"] == raw["baseline"]["mode"] == "max_self_consumption"
    neutral = {"mode": "max_self_consumption", "power": "none"}
    for intent in ("charge_grid", "charge_pv", "discharge_forced", "sell", "self_consume", "standby"):
        assert raw["intents"][intent] == neutral, intent
    caps = raw["capabilities"]
    for cap in ("force_charge_from_grid", "sell_from_battery", "force_discharge", "standby", "set_power_w",
                "limit_export", "set_soc_floor", "set_soc_ceiling"):
        assert caps[cap] is False, cap
    assert caps["time_windows"] == 0
    assert raw["write_policy"]["order"] == ["mode"]
    # Rejestr zdalnego polecenia 0xE00D nie jest pisany (wymaga 0xE004 = 4 — luka).
    assert all(spec["addr"] != REMOTE_CMD_MODE for spec in raw["write"].values())


def test_modbus_block(profile):
    m = profile.raw["modbus"]
    assert m["verify_blocks"] == [{"addr": STORAGE_CTRL_MODE, "count": 1}]
    assert m["probe_keys"] == ["mode"]
    assert "echo_only" not in m
    assert m["write_function"] == 6
    assert profile.raw["unit_id"] == 1
    assert profile.raw["transports"] == ["modbus_tcp"]
    assert m["transport_options"]["modbus_tcp"]["port"] == 1502
    note = m["status_note"]
    assert "re-writing" in note and "57348" in note and "one client" in note


def _ents(profile, domain):
    integ = next(i for i in profile.raw["ha"]["integrations"] if i["domain"] == domain)
    assert integ["ems"] is False and integ["status"] == "draft"
    return integ["entities"]


def _check(ents, keys, uid, near):
    assert set(ents) == set(keys)
    for key, suffix in keys.items():
        assert re.search(ents[key]["unique_id_regex"], uid(suffix)), (key, uid(suffix))
    for key, suffixes in near.items():
        for suffix in suffixes:
            assert not re.search(ents[key]["unique_id_regex"], uid(suffix)), (key, suffix)


def test_ha_entity_map_solaredge_modbus(profile):
    assert [i["domain"] for i in profile.raw["ha"]["integrations"]] == ["solaredge_modbus",
                                                                        "solaredge_modbus_multi"]
    ents = _ents(profile, "solaredge_modbus")
    # binsentsu: "<nazwa huba>_<klucz>", domyślna nazwa "solaredge".
    keys = {
        "soc": "battery1_state_of_charge",
        "battery_power_w": "battery1_power",
        "battery_temp_c": "battery1_temp_avg",
        "battery_voltage_v": "battery1_voltage",
        "grid_power_w": "m1_acpower",
        "active_power_w": "acpower",
        "grid_import_total_kwh": "m1_imported",
        "grid_export_total_kwh": "m1_exported",
        "mode": "storage_contol_mode",
    }
    near = {
        "soc": ("battery1_state_of_health", "battery2_state_of_charge"),
        "battery_power_w": ("battery2_power",),
        "battery_temp_c": ("battery1_temp_max",),
        "grid_power_w": ("m1_acpowera", "m2_acpower", "acpower"),
        "active_power_w": ("m1_acpower", "m2_acpower", "m1_acpowera", "dcpower"),
        "grid_import_total_kwh": ("m1_importeda", "m1_importedva"),
        "grid_export_total_kwh": ("m1_exporteda", "m1_exportedva"),
        "mode": ("storage_default_mode", "storage_remote_command_mode", "export_control_mode"),
    }
    for hub in ("solaredge", "SolarEdge Garage"):
        _check(ents, keys, lambda s: f"{hub}_{s}", near)
    # Pisownia poprawiona w przyszłej wersji też trafia.
    assert re.search(ents["mode"]["unique_id_regex"], "solaredge_storage_control_mode")
    assert ents["grid_power_w"]["transform"] == ents["battery_power_w"]["transform"] == "negate"
    assert ents["mode"]["domain"] == "select"
    assert "pv_power_w" not in ents and "load_power_w" not in ents


def test_ha_entity_map_solaredge_modbus_multi(profile):
    ents = _ents(profile, "solaredge_modbus_multi")
    # WillCodeForCats: "<C_Model>_<C_SerialNumber>" + "_B<n>" (bateria) / "_M<n>" (licznik).
    keys = {
        "soc": "B1_battery_soe",
        "battery_power_w": "B1_dc_power",
        "battery_temp_c": "B1_avg_temp",
        "battery_voltage_v": "B1_dc_voltage",
        "grid_power_w": "M1_ac_power",
        "active_power_w": "ac_power",
        "grid_import_total_kwh": "M1_imported_kwh",
        "grid_export_total_kwh": "M1_exported_kwh",
        "mode": "storage_control_mode",
    }
    near = {
        "soc": ("DERB1_battery_soe", "B2_battery_soe"),
        "battery_power_w": ("B1_dc_power_inverted", "dc_power", "B1_max_charge_power"),
        "battery_voltage_v": ("dc_voltage",),
        "grid_power_w": ("M1_ac_power_inverted", "M1_ac_power_a", "ac_power", "M2_ac_power"),
        "active_power_w": ("M1_ac_power", "M2_ac_power", "ac_power_a", "M1_ac_power_inverted"),
        "grid_import_total_kwh": ("M1_imported_a_kwh",),
        "grid_export_total_kwh": ("M1_exported_a_kwh",),
        "mode": ("storage_command_mode", "storage_default_mode"),
    }
    _check(ents, keys, lambda s: f"SE10K-RWB48BFN4_FAKE0001SE_{s}", near)
    assert ents["grid_power_w"]["transform"] == ents["battery_power_w"]["transform"] == "negate"
    for key in ("active_power_w", "soc"):
        assert "transform" not in ents[key], key
    assert "pv_power_w" not in ents and "load_power_w" not in ents


def test_engine_gaps_document_solaredge():
    text = GAPS_FILE.read_text(encoding="utf-8")
    assert SECTION in text
    assert text.index(SECTION) < text.index("## Automatic verification ladder")
    section = text.split(SECTION, 1)[1].split("\n## ", 1)[0]
    for needle in ("0xE004", "0xE00D", "0xE00A", "0xE00B", "0xE00E", "0xE010", "0xE008", "0xE000", "0xE002",
                   "0xE005", "40084", "40210", "40242", "0x7FC00000", "0xE200", "40069"):
        assert needle in section, needle
    core = text.split("## Core", 1)[1].split("\n## ", 1)[0]
    assert any("solaredge-storedge" in line and "scale factor" in line.lower() for line in core.splitlines())
    rated_rows = [line for line in core.splitlines() if "rated" in line.lower()]
    assert any("solaredge-storedge" in line for line in rated_rows)
    ladder = text.split("## Automatic verification ladder", 1)[1]
    assert any("solaredge-storedge" in line and "forced test window" in line for line in ladder.splitlines())
    assert not any("solaredge-storedge" in line and "exclusive" in line.lower() for line in ladder.splitlines())
    # Domeny integracji: brak solaredge_modbus w INVERTER_DOMAINS / CONFLICT_DOMAINS jako wiersze rdzenia.
    rows = [line for line in core.splitlines() if "solaredge_modbus`" in line]
    assert any("INVERTER_DOMAINS" in line for line in rows)
    assert any("CONFLICT_DOMAINS" in line and "one Modbus TCP client" in line for line in rows)
    assert "`undef` applies to integer sums only" not in text


def test_entity_mode_domains_match_the_documented_gap(profile):
    # Strażnik: gdy rdzeń dopisze solaredge_modbus do INVERTER_DOMAINS, wiersz luki i status_note do poprawy.
    assert "solaredge_modbus_multi" in INVERTER_DOMAINS and "solaredge_modbus_multi" in CONFLICT_DOMAINS
    assert "solaredge_modbus" not in INVERTER_DOMAINS and "solaredge_modbus" not in CONFLICT_DOMAINS
    note = profile.raw["status_note"]
    assert "INVERTER_DOMAINS" in note and "conflict" in note and "manual pick" in note
