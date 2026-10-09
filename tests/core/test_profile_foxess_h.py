"""Profil foxess-h (draft): identyfikacja, odczyty i spójność na syntetycznym obrazie golden."""
import json
import re
from pathlib import Path

import pytest

from custom_components.volcast.core.modbus.identity import MIN_SALT_BYTES, device_fingerprint, identity_info
from custom_components.volcast.core.profile import load_builtin
from custom_components.volcast.core.profile_schema import validate_profile
from tests.core import profile_golden as pg
from tests.core.modbus.helpers import golden_image, goodwe_image
from tests.core.test_profile_sofar_hyd import assert_sources_public

PID = "foxess-h"
ROOT = Path(__file__).resolve().parents[2]
PROFILE_FILE = ROOT / "custom_components" / "volcast" / "profiles" / f"{PID}.json"
GAPS_FILE = ROOT / "docs" / "profiles" / "engine-gaps.md"
SECTION = "## FoxESS (`foxess-h`)"
# Stan wzorcowy obrazu (README katalogu golden). Konwencja rdzenia: bateria dodatnia = rozładowanie
# (FoxESS 31022 tak samo), sieć dodatnia = pobór (FoxESS 31014 odwrotnie: dodatnie = oddawanie,
# stąd sign -1). Liczniki u32 w 0,1 kWh, starsze słowo pod niższym adresem.
REFERENCE = {
    "soc": 55,
    "pv_power_w": 3200,
    "battery_power_w": -1500,
    "grid_power_w": 400,
    "load_power_w": 2100,
    "active_power_w": 1700,
    "battery_temp_c": 24,
    "pv_energy_total_kwh": 12345.6,
    "grid_import_total_kwh": 4321.0,
    "grid_export_total_kwh": 6789.0,
    "mode_value": 0,
    "soc_min": 15,
    "soc_max": 100,
}
# Adres z ramki = dziesiętny numer rejestru (foxess_modbus wysyła go bez przesunięcia).
MODEL = 30000
WORK_MODE = 41000
MAX_SOC = 41010
MIN_SOC_ON_GRID = 41011
BMS_CHARGE_RATE = 31025


def _packed(text: str, regs: int = 15) -> dict[str, int]:
    """Model jako 2 znaki ASCII na rejestr (H1-G2: starszy bajt pierwszy), dopełniony spacjami."""
    raw = text.encode("ascii").ljust(2 * regs, b" ")
    return {str(MODEL + i): (raw[2 * i] << 8) | raw[2 * i + 1] for i in range(regs)}


def _one_char_per_register(text: str, regs: int = 15) -> dict[str, int]:
    """Układ H1 G1/H3: jeden znak na rejestr (młodszy bajt) — tego rdzeń dziś nie dekoduje."""
    raw = text.encode("ascii").ljust(regs, b" ")
    return {str(MODEL + i): raw[i] for i in range(regs)}


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
    # Konwencja adresów nazwana wprost.
    assert "wire (PDU) addresses" in note and "31024 is sent as 31024" in note
    # Wariant objęty (H1-G2 przez RS485) i nieobjęte (LAN, H1 G1, H3, KH).
    assert "H1-G2" in note and "not covered" in note
    for variant in ("LAN", "H1 G1", "H3", "KH"):
        assert variant in note, variant
    # Brak rejestru mocy i seriala: tryb bezpośredni nie zidentyfikuje urządzenia (R5).
    assert "31025" in note and "identity_unknown" in note and "only entity mode" in note
    assert "onboarding asks" not in note


def test_sources_are_public_and_cover_key_registers(profile):
    raw = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
    assert_sources_public(raw)
    # Dokumenty producenta podlinkowane z wiki integracji (wgrane pliki PDF) nie są źródłem.
    texts = [s["ref"] + s["what"] for s in raw["sources"]] + [raw["status_note"], raw["modbus"]["status_note"]]
    for text in texts:
        assert ".pdf" not in text.lower() and "Protocol-Documents" not in text and "/files/" not in text
    whats = " ".join(s["what"] for s in raw["sources"])
    for reg in ("30000", "31008", "31014", "31016", "31022", "31023", "31024", "31025", "32000",
                "32009", "32012", "39280", "39282", "41000", "41009", "41010", "41011"):
        assert reg in whats, reg
    refs = [s["ref"] for s in raw["sources"]]
    assert any(r.endswith("/entities/entity_descriptions.py") for r in refs)
    assert any(r.endswith("/wiki/Supported-Features") for r in refs)
    assert any(r.endswith("/wiki/Direct-Ethernet-Connection-to-Inverter") for r in refs)
    assert any(r.endswith("/discussions/553") and "Back-up" in s["what"] for r, s in zip(refs, raw["sources"]))
    note = raw["status_note"]
    # Lista kontrolna próby dla Back-up: wersja firmware, Back-up w menu, moc baterii pod obciążeniem.
    assert "firmware" in note and "front-panel menu" in note and "31022" in note


def test_identify_matches_golden_and_rejects_other_brands(profile, image):
    pg.assert_identify_matches(profile, image)
    for other in ("deye-sg", "huawei-sun2000", "sungrow-sh", "solax-x-hybrid", "sofar-hyd", "solis-hybrid"):
        pg.assert_identify_rejects(profile, golden_image(other))
    pg.assert_identify_rejects(profile, goodwe_image())


def test_identify_reads_packed_model_and_rating_is_unknown(profile, image):
    info = identity_info(profile, image)
    assert info["model"] == "H1-5.0-E-G2"
    # Brak rejestru mocy znamionowej: 31025 (limit prądu ładowania z BMS, 0,1 A) zawsze poza
    # 1–30 kW, więc moc jest nieznana i onboarding pyta użytkownika.
    assert info["rated_power_w"] is None
    ident = profile.raw["identify"]
    assert ident["model_register"] == {"addr": MODEL, "type": "ascii", "len": 15}
    assert ident["registers"]["rated_power_w"] == {"addr": BMS_CHARGE_RATE, "type": "i16", "scale": 0.1}
    assert "serial" not in ident["registers"]
    assert profile.raw["limits"]["rated_power_register"] == "rated_power_w"
    covered = {a for b in profile.raw["modbus"]["identify_reads"] if b.get("fc", 3) == 3
               for a in range(b["addr"], b["addr"] + b["count"])}
    for spec in (ident["model_register"], *ident["registers"].values()):
        width = spec.get("len", 2 if spec["type"] in ("u32", "i32") else 1)
        assert set(range(spec["addr"], spec["addr"] + width)) <= covered


def test_direct_path_has_no_fingerprint(profile, image):
    # Bez seriala i bez mocy znamionowej rdzeń nie liczy odcisku — wykrywanie zgłasza
    # identity_unknown, więc ścieżka bezpośrednia nie zidentyfikuje FoxESS (tylko tryb encji).
    assert identity_info(profile, image)["matched"]
    assert device_fingerprint(b"s" * MIN_SALT_BYTES, profile, image) is None


def test_bms_charge_rate_never_reads_as_a_rating(profile):
    # Nawet 100 A (1000 surowo) daje 100 „W” po skali, a 0xFFFF/0x8000 to wartości ujemne (i16).
    for raw in (0, 250, 500, 1000, 0xFFFF, 0x8000):
        info = identity_info(profile, golden_image(PID, **{str(BMS_CHARGE_RATE): raw}))
        assert info["matched"] and info["rated_power_w"] is None, raw


@pytest.mark.parametrize("model", [
    "H1-3.7-E-G2", "H1-5.0-E-G2", "H1-6.0-E1-G2", "AC1-3.0-E-G2", "AC1-5.0-E-G2", "P1-5.0-E", "P1-6.0-E",
])
def test_identify_accepts_h1_g2_family(profile, model):
    pg.assert_identify_matches(profile, golden_image(PID, **{a: w for a, w in _packed(model).items()}))


@pytest.mark.parametrize("model", [
    "H1-5.0-E",          # H1 G1 (inna mapa: LAN 31xxx bez nastaw, RS485 input 11xxx)
    "AC1-3.6",           # AC1 G1
    "AIO-H1-5.0",        # AIO-H1 (mapa G1)
    "H3-10.0-E",         # trójfazowy H3
    "H3-8.0-Smart",      # H3-Smart (rejestry 46xxx/49xxx)
    "KH10.5",            # KH
    "Kuara 6.0-3-H",
])
def test_identify_rejects_other_foxess_families(profile, model):
    pg.assert_identify_rejects(profile, golden_image(PID, **_packed(model)))


def test_one_char_per_register_layout_is_not_decoded(profile):
    # H1 G1 i H3 trzymają model po jednym znaku na rejestr: rdzeń składa dwa bajty na rejestr,
    # więc taki model nie jest rozpoznawany (luka w engine-gaps.md), nawet z napisem H1-G2.
    pg.assert_identify_rejects(profile, golden_image(PID, **_one_char_per_register("H1-5.0-E-G2")))


def test_reads_decode_reference_state(profile, image):
    values = pg.decode_reads(profile, image)
    assert set(values) == set(profile.raw["read"])
    assert set(values) == set(REFERENCE)
    for key, want in REFERENCE.items():
        assert values[key] == pytest.approx(want), key


def test_power_balance_of_reference_state(profile, image):
    v = pg.decode_reads(profile, image)
    assert v["pv_power_w"] + v["battery_power_w"] + v["grid_power_w"] == v["load_power_w"]
    assert v["load_power_w"] - v["grid_power_w"] == v["active_power_w"]


def test_register_map_is_holding_only(profile):
    read = profile.raw["read"]
    for key, spec in read.items():
        parts = spec["sum"] if "sum" in spec else [spec]
        for part in parts:
            assert part.get("fc", 3) == 3, key
            if part.get("type") in ("u32", "i32"):
                assert part.get("word_order", "hi_lo") == "hi_lo", key
    assert read["soc"] == {"addr": 31024, "type": "u16"}
    assert read["battery_power_w"] == {"addr": 31022, "type": "i16"}
    assert read["grid_power_w"] == {"addr": 31014, "type": "i16", "sign": -1}
    assert read["load_power_w"] == {"addr": 31016, "type": "i16"}
    assert read["active_power_w"] == {"addr": 31008, "type": "i16"}
    assert read["battery_temp_c"] == {"addr": 31023, "type": "i16", "scale": 0.1}
    # PV = PV1 + PV2; na G2 czytane jest tylko młodsze słowo (starsze nie jest zapisywane).
    assert read["pv_power_w"] == {"sum": [{"addr": 39280, "type": "i16"}, {"addr": 39282, "type": "i16"}]}
    assert read["pv_energy_total_kwh"] == {"addr": 32000, "type": "u32", "scale": 0.1}
    assert read["grid_export_total_kwh"] == {"addr": 32009, "type": "u32", "scale": 0.1}
    assert read["grid_import_total_kwh"] == {"addr": 32012, "type": "u32", "scale": 0.1}
    assert read["mode_value"] == {"addr": WORK_MODE, "type": "u16"}
    assert read["soc_min"] == {"addr": MIN_SOC_ON_GRID, "type": "u16"}
    assert read["soc_max"] == {"addr": MAX_SOC, "type": "u16"}


def test_static_consistency(profile):
    pg.assert_intents_consistent(profile)
    pg.assert_ha_regexes_compile(profile)
    pg.assert_capabilities_match_writes(profile)


def test_control_work_mode_and_soc_limits(profile):
    raw = profile.raw
    assert raw["write"] == {
        "soc_min": {"addr": MIN_SOC_ON_GRID, "type": "u16", "encode": "percent"},
        "soc_max": {"addr": MAX_SOC, "type": "u16", "encode": "percent"},
        "mode": {"addr": WORK_MODE, "type": "u16", "encode": "mode"},
    }
    # 41000: 0 Self Use, 1 Feed-in First, 2 Back-up, 4 Peak Shaving (H1-G2). Back-up (2) na H1-G2
    # z firmware 1.18 nic nie robi (foxess_modbus #553), a odczyt zwrotny i tak by przeszedł —
    # bez źródła z firmware, na którym działa, postój jest nieobsługiwany. Feed-in First to nie sprzedaż.
    assert raw["modes"] == {"self_use": {"value": 0, "direction": "neutral", "ha_option": "Self Use"}}
    assert raw["neutral_mode"] == raw["baseline"]["mode"] == "self_use"
    neutral = {"mode": "self_use", "power": "none"}
    for intent in ("charge_grid", "discharge_forced", "sell", "self_consume", "charge_pv", "standby"):
        assert raw["intents"][intent] == neutral, intent
    caps = raw["capabilities"]
    assert caps["set_soc_floor"] is True and caps["set_soc_ceiling"] is True
    for cap in ("force_charge_from_grid", "sell_from_battery", "force_discharge", "standby", "set_power_w",
                "limit_export"):
        assert caps[cap] is False, cap
    assert caps["time_windows"] == 0
    assert raw["write_policy"]["order"][-1] == "mode"
    assert raw["write_policy"]["nvm"] is True


def test_write_read_back_at_same_address_and_verify_blocks_cover_writes(profile):
    m = profile.raw["modbus"]
    addrs = {spec["addr"] for spec in profile.raw["write"].values()}
    # 41000-41999 foxess_modbus czyta pojedynczo na H1-G2 — bloki weryfikacji po jednym rejestrze.
    assert all(b["count"] == 1 and b.get("fc", 3) == 3 for b in m["verify_blocks"])
    assert {b["addr"] for b in m["verify_blocks"]} == addrs
    assert set(m["probe_keys"]) == set(profile.raw["write"])
    assert "echo_only" not in m
    assert m["write_function"] == 6
    assert profile.raw["unit_id"] == 247
    assert profile.raw["transports"] == ["modbus_tcp", "modbus_rtu"]
    assert m["transport_options"]["modbus_tcp"]["port"] == 502
    assert m["transport_options"]["modbus_rtu"]["port"] == 502
    assert set(m["transport_options"]) == {"modbus_tcp", "modbus_rtu"}
    assert "re-writing" in m["status_note"] and "41000" in m["status_note"]
    assert "41000-41999" in m["status_note"]


def _ents(profile):
    integ = next(i for i in profile.raw["ha"]["integrations"] if i["domain"] == "foxess_modbus")
    assert integ["ems"] is False and integ["status"] == "draft"
    assert len(profile.raw["ha"]["integrations"]) == 1
    return integ["entities"]


def test_ha_entity_map_matches_foxess_modbus_unique_ids(profile):
    ents = _ents(profile)
    # nathanmarlor/foxess_modbus: "foxess_modbus_" + opcjonalny "<prefiks>_" + klucz encji.
    keys = {
        "soc": "battery_soc",
        "battery_temp_c": "battery_temp",
        "battery_power_w": "invbatpower",
        "battery_voltage_v": "batvolt",
        "pv_power_w": "pv_power_now",
        "grid_power_w": "grid_ct",
        "load_power_w": "load_power",
        "active_power_w": "rpower",
        "pv_energy_total_kwh": "solar_energy_total",
        "grid_import_total_kwh": "grid_consumption_energy_total",
        "grid_export_total_kwh": "feed_in_energy_total",
        "mode": "work_mode",
        "soc_min": "min_soc_on_grid",
        "soc_max": "max_soc",
    }
    assert set(ents) == set(keys)
    for key, fox in keys.items():
        for prefix in ("", "H1_", "garage_inverter_", "Garage H1_"):
            uid = f"foxess_modbus_{prefix}{fox}"
            assert re.search(ents[key]["unique_id_regex"], uid), (key, uid)
    near = {
        "soc": ("battery_soc_1", "battery_soh", "bms_kwh_remaining"),
        "battery_temp_c": ("battery_temp_1", "bms_cell_temp_high", "invtemp"),
        "battery_power_w": ("invbatpower_1", "battery_charge", "battery_discharge"),
        "battery_voltage_v": ("batvolt_1",),
        "pv_power_w": ("pv1_power", "pv2_power"),
        "grid_power_w": ("feed_in", "grid_consumption", "ct2_meter"),
        "load_power_w": ("load_power_total", "eps_rpower"),
        "active_power_w": ("eps_rpower", "rpower_Q", "rpower_S"),
        "pv_energy_total_kwh": ("pv1_energy_total", "solar_energy_today"),
        "grid_import_total_kwh": ("grid_consumption_energy_today",),
        "grid_export_total_kwh": ("feed_in_energy_today",),
        "soc_min": ("min_soc",),
        "soc_max": ("force_charge_max_soc", "max_soc_1"),
    }
    for key, foxes in near.items():
        for fox in foxes:
            for prefix in ("", "H1_"):
                uid = f"foxess_modbus_{prefix}{fox}"
                assert not re.search(ents[key]["unique_id_regex"], uid), (key, uid)
    # Siatka: Grid CT dodatnie = oddawanie → negate; bateria dodatnia = rozładowanie (jak rdzeń).
    assert ents["grid_power_w"].get("transform") == "negate"
    for key in ("battery_power_w", "active_power_w", "load_power_w", "pv_power_w"):
        assert "transform" not in ents[key], key
    assert ents["mode"]["domain"] == "select"
    assert ents["soc_min"]["domain"] == ents["soc_max"]["domain"] == "number"
    # Prąd baterii pominięty: znak bat_current nieopisany w źródłach.
    assert "battery_current_a" not in ents


def test_engine_gaps_document_foxess():
    text = GAPS_FILE.read_text(encoding="utf-8")
    assert SECTION in text
    section = text.split(SECTION, 1)[1].split("\n## ", 1)[0]
    for needle in ("30000", "41001", "41002", "41003", "41004", "41006", "44000", "44001", "44002",
                   "41000-41999", "31025", "Manager 1.70", "49203"):
        assert needle in section, needle
    assert text.index(SECTION) < text.index("## Automatic verification ladder")
    assert "identity_unknown" in section
    ladder = text.split("## Automatic verification ladder", 1)[1]
    assert "foxess-h" in ladder
    assert any("foxess-h" in line and "fingerprint" in line for line in ladder.splitlines())
    core = text.split("## Core", 1)[1].split("\n## ", 1)[0]
    rated_rows = [line for line in core.splitlines() if "rated" in line.lower()]
    assert any("foxess-h" in line and "identity_unknown" in line for line in rated_rows)
