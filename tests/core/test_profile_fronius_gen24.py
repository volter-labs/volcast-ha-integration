"""Profil fronius-gen24 (draft): identyfikacja, odczyty i spójność na syntetycznym obrazie golden."""
import json
import re
from pathlib import Path

import pytest

from custom_components.volcast.core.control.caps import REQUIRED_WRITE_KEYS
from custom_components.volcast.core.control.conflict import CONFLICT_DOMAINS
from custom_components.volcast.core.discovery.known import INVERTER_DOMAINS
from custom_components.volcast.core.modbus.identity import MIN_SALT_BYTES, device_fingerprint, identity_info
from custom_components.volcast.core.profile import load_builtin
from custom_components.volcast.core.profile_schema import validate_profile
from tests.core import profile_golden as pg
from tests.core.modbus.helpers import GOLDEN, golden_image, goodwe_image
from tests.core.test_profile_sofar_hyd import assert_sources_public

PID = "fronius-gen24"
ROOT = Path(__file__).resolve().parents[2]
PROFILE_FILE = ROOT / "custom_components" / "volcast" / "profiles" / f"{PID}.json"
GAPS_FILE = ROOT / "docs" / "profiles" / "engine-gaps.md"
SECTION = "## Fronius (`fronius-gen24`)"
SALT = b"f" * MIN_SALT_BYTES

# Adresy z ramki (PDU), wariant "int + SF". Łańcuch modeli SunSpec od 40000 ("SunS"): model 1 od
# 40002, falownik 103 od 40069, tabliczka 120 od 40121, 121 od 40149, 122 od 40181, 123 od 40227,
# MPPT 160 od 40253 (4 moduły: PV 1, PV 2, ładowanie i rozładowanie magazynu), magazyn 124 od 40343.
MODEL = 40020
SERIAL = 40052
WRTG, WRTG_SF = 40124, 40125
AC_POWER, AC_POWER_SF = 40083, 40084
DCW_SF = 40257
PV1_DCW, PV2_DCW, CHA_DCW, DISCHA_DCW = 40274, 40294, 40314, 40334
WCHAMAX = 40345
STORCTL_MOD = 40348
MINRSVPCT = 40350
CHASTATE = 40351
OUTWRTE, INWRTE = 40355, 40356
RVRT_TMS = 40358
CHAGRISET = 40360
MINRSVPCT_SF, CHASTATE_SF, INOUTWRTE_SF = 40364, 40365, 40368

# Stan wzorcowy (README katalogu golden). Konwencja rdzenia: bateria dodatnia = rozładowanie
# (moduł 4 "rozładowanie" minus moduł 3 "ładowanie"). Licznik sieci jest pod innym unit id (200),
# więc sieć i dom nie są czytane bezpośrednio (luka w engine-gaps.md).
REFERENCE = {
    "soc": 55,
    "pv_power_w": 3200,
    "battery_power_w": -1500,
    "active_power_w": 1700,
    "mode_value": 0,
}


def _text(addr: int, text: str, regs: int = 16) -> dict[str, int]:
    """Napis SunSpec: 2 znaki ASCII na rejestr (starszy bajt pierwszy), dopełniony zerami."""
    raw = text.encode("ascii").ljust(2 * regs, b"\x00")
    return {str(addr + i): (raw[2 * i] << 8) | raw[2 * i + 1] for i in range(regs)}


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
    # Konwencja adresów: adres z ramki = numer rejestru producenta minus 1.
    assert "wire (PDU) addresses" in note and "40001" in note and "40349" in note
    # Wariant objęty (int + SF) i nieobjęte (float, Symo Hybrid, Verto Plus, Tauro).
    assert "int + SF" in note and "float" in note and "not covered" in note
    for variant in ("Symo Hybrid", "Verto", "Tauro", "unit id 200"):
        assert variant in note, variant
    # Włączenie Modbus TCP i sterowania w interfejsie WWW.
    assert "Inverter control via Modbus" in note and "502" in note
    # Dynamiczne współczynniki skali jako luka z listą do próby.
    assert "auto-scale" in note and "40084" in note and "40257" in note and "40365" in note
    # Sieć i dom nie są czytane bezpośrednio.
    assert "grid power and house load are not read" in note


def test_sources_are_public_and_cover_key_registers(profile):
    raw = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
    assert_sources_public(raw)
    whats = " ".join(s["what"] for s in raw["sources"])
    for reg in ("40000", "40020", "40052", "40083", "40084", "40121", "40124", "40253", "40257", "40274",
                "40294", "40314", "40334", "40343", "40345", "40348", "40350", "40351", "40355", "40356",
                "40358", "40360", "40364", "40365", "40368", "502", "200"):
        assert reg in whats, reg
    refs = [s["ref"] for s in raw["sources"]]
    assert any(r.startswith("https://manuals.fronius.com/") for r in refs)
    assert any("sunspec/models" in r and r.endswith("model_124.json") for r in refs)
    assert any("sunspec/models" in r and r.endswith("model_160.json") for r in refs)
    assert any("evcc-io/evcc" in r and r.endswith("/fronius-gen24.yaml") for r in refs)
    assert any("libe.net" in r for r in refs)
    assert any("home-assistant/core" in r and "/fronius/" in r for r in refs)


def test_identify_matches_golden_and_rejects_other_brands(profile, image):
    pg.assert_identify_matches(profile, image)
    others = sorted(p.name.replace("_", "-") for p in GOLDEN.iterdir()
                    if (p / "registers.json").exists() and p.name != PID.replace("-", "_"))
    assert {"deye-sg", "huawei-sun2000", "sungrow-sh", "solaredge-storedge"} <= set(others)
    for other in others:
        pg.assert_identify_rejects(profile, golden_image(other))
    pg.assert_identify_rejects(profile, goodwe_image())


def test_identity_model_rating_and_fingerprint(profile, image):
    info = identity_info(profile, image)
    assert info["model"] == "Symo GEN24 10.0 Plus"
    assert info["rated_power_w"] == pytest.approx(10000)
    ident = profile.raw["identify"]
    assert ident["model_register"] == {"addr": MODEL, "type": "ascii", "len": 16}
    assert ident["registers"]["serial"] == {"addr": SERIAL, "type": "ascii", "len": 16}
    assert ident["registers"]["rated_power_w"] == {"addr": WRTG, "type": "u16"}
    assert profile.raw["limits"]["rated_power_register"] == "rated_power_w"
    covered = {a for b in profile.raw["modbus"]["identify_reads"] if b.get("fc", 3) == 3
               for a in range(b["addr"], b["addr"] + b["count"])}
    for spec in (ident["model_register"], *ident["registers"].values()):
        width = spec.get("len", 2 if spec["type"] in ("u32", "i32", "f32") else 1)
        assert set(range(spec["addr"], spec["addr"] + width)) <= covered
    fp = device_fingerprint(SALT, profile, image)
    assert fp is not None
    other = golden_image(PID, **_text(SERIAL, "FAKE0002FR"))
    assert device_fingerprint(SALT, profile, other) not in (None, fp)
    blank = golden_image(PID, **{str(SERIAL + i): 0 for i in range(16)})
    assert device_fingerprint(SALT, profile, blank) is None


def test_rating_uses_a_fixed_scale_factor(profile):
    # WRtg_SF 40125 nie jest czytany: przy SF 1 surowe 1000 to 10 kW, rdzeń widzi 1 kW (luka).
    info = identity_info(profile, golden_image(PID, **{str(WRTG): 1000, str(WRTG_SF): 1}))
    assert info["matched"] and info["rated_power_w"] == pytest.approx(1000)


@pytest.mark.parametrize("model", [
    "Symo GEN24 10.0 Plus", "Primo GEN24 6.0 Plus", "Symo GEN24 6.0", "Fronius Primo GEN24 3.0 Plus",
])
def test_identify_accepts_gen24_models(profile, model):
    pg.assert_identify_matches(profile, golden_image(PID, **_text(MODEL, model)))


@pytest.mark.parametrize("model", [
    "Symo 10.0-3-M", "Fronius Symo 10.0-3-M", "Symo Hybrid 5.0-3-S", "Tauro 50-3-D", "Verto Plus 30.0",
    "SE10K-RWB48BFN4", "gen24", "",
])
def test_identify_rejects_other_models(profile, model):
    pg.assert_identify_rejects(profile, golden_image(PID, **_text(MODEL, model)))


def test_reads_decode_reference_state(profile, image):
    values = pg.decode_reads(profile, image)
    assert set(values) == set(profile.raw["read"])
    assert set(values) == set(REFERENCE)
    for key, want in REFERENCE.items():
        assert values[key] == pytest.approx(want), key


def test_register_map(profile):
    read = profile.raw["read"]
    assert read["soc"] == {"addr": CHASTATE, "type": "u16", "scale": 0.01}
    assert read["pv_power_w"] == {"sum": [{"addr": PV1_DCW, "type": "u16"}, {"addr": PV2_DCW, "type": "u16"}]}
    assert read["battery_power_w"] == {"sum": [{"addr": DISCHA_DCW, "type": "u16"},
                                               {"addr": CHA_DCW, "type": "u16", "sign": -1}]}
    assert read["active_power_w"] == {"addr": AC_POWER, "type": "i16"}
    assert read["mode_value"] == {"addr": STORCTL_MOD, "type": "u16"}
    for key, spec in read.items():
        for part in spec.get("sum", [spec]):
            assert part.get("fc", 3) == 3, key
    # Licznik (unit 200) i temperatura baterii (brak rejestru w modelach 124/160) pominięte.
    for absent in ("grid_power_w", "load_power_w", "battery_temp_c", "grid_import_total_kwh"):
        assert absent not in read, absent


def test_fixed_scale_ignores_the_scale_factor_registers(profile, image):
    # Dokumentuje lukę: rdzeń nie czyta SF, więc zmiana SF nie zmienia wyniku (powinna 10×).
    assert image.words(AC_POWER_SF, 1)[0] == 0 and image.words(DCW_SF, 1)[0] == 0
    assert image.words(CHASTATE_SF, 1)[0] == 0xFFFE
    changed = golden_image(PID, **{str(DCW_SF): 0xFFFF, str(CHASTATE_SF): 0xFFFF})
    values = pg.decode_reads(profile, changed)
    assert values["pv_power_w"] == pytest.approx(3200) and values["soc"] == pytest.approx(55)


@pytest.mark.parametrize("charge,discharge,want", [(1500, 0, -1500), (0, 2000, 2000), (0, 0, 0)])
def test_battery_power_from_the_storage_modules(profile, charge, discharge, want):
    values = pg.decode_reads(profile, golden_image(PID, **{str(CHA_DCW): charge, str(DISCHA_DCW): discharge}))
    assert values["battery_power_w"] == pytest.approx(want)


def test_static_consistency(profile):
    pg.assert_intents_consistent(profile)
    pg.assert_ha_regexes_compile(profile)
    pg.assert_capabilities_match_writes(profile)


def test_control_storage_mode_only(profile):
    raw = profile.raw
    assert raw["write"] == {"mode": {"addr": STORCTL_MOD, "type": "u16", "encode": "mode"}}
    assert set(raw["modes"]) == {"no_limits"}
    assert raw["modes"]["no_limits"]["value"] == 0
    assert raw["modes"]["no_limits"]["direction"] == "neutral"
    assert raw["neutral_mode"] == raw["baseline"]["mode"] == "no_limits"
    neutral = {"mode": "no_limits", "power": "none"}
    for intent in ("charge_grid", "charge_pv", "discharge_forced", "sell", "self_consume", "standby"):
        assert raw["intents"][intent] == neutral, intent
    caps = raw["capabilities"]
    for cap in ("force_charge_from_grid", "sell_from_battery", "force_discharge", "standby", "set_power_w",
                "limit_export", "set_soc_floor", "set_soc_ceiling"):
        assert caps[cap] is False, cap
    assert caps["time_windows"] == 0
    assert raw["write_policy"]["order"] == ["mode"]
    # Nastawy w % WChaMax ze skalą, rezerwa ze skalą, zgoda na ładowanie z sieci: bez zapisu (luki).
    for addr in (MINRSVPCT, OUTWRTE, INWRTE, RVRT_TMS, CHAGRISET, WCHAMAX):
        assert all(spec["addr"] != addr for spec in raw["write"].values()), addr


def test_modbus_block(profile):
    m = profile.raw["modbus"]
    assert m["verify_blocks"] == [{"addr": STORCTL_MOD, "count": 1}]
    assert m["probe_keys"] == ["mode"]
    assert "echo_only" not in m
    assert m["write_function"] == 6
    assert profile.raw["unit_id"] == 1
    assert profile.raw["transports"] == ["modbus_tcp"]
    assert m["transport_options"]["modbus_tcp"]["port"] == 502
    assert m["max_read_registers"] <= 125
    note = m["status_note"]
    assert "re-writing" in note and "40348" in note and "Inverter control via Modbus" in note


def test_ha_entity_map_fronius(profile):
    integs = profile.raw["ha"]["integrations"]
    assert [i["domain"] for i in integs] == ["fronius"]
    integ = integs[0]
    assert integ["ems"] is False and integ["status"] == "draft"
    ents = integ["entities"]
    # Rdzeń HA: "<solar_net_device_id>-power_flow-<klucz>", "<uid falownika>-<klucz>",
    # "<uid falownika>-modbus-<klucz>", "<serial magazynu>-<klucz>", "<serial licznika>-<klucz>".
    uids = {
        "pv_power_w": "solar_net_12345678-power_flow-power_photovoltaics",
        "battery_power_w": "solar_net_12345678-power_flow-power_battery",
        "grid_power_w": "solar_net_12345678-power_flow-power_grid",
        "load_power_w": "solar_net_12345678-power_flow-power_load_consumed",
        "active_power_w": "12345678-power_ac",
        "pv_energy_total_kwh": "12345678-modbus-energy_total_pv",
        "soc": "BYD0001-state_of_charge",
        "battery_temp_c": "BYD0001-temperature_cell",
        "grid_import_total_kwh": "FAKEMETER1-energy_real_consumed",
        "grid_export_total_kwh": "FAKEMETER1-energy_real_produced",
        "soc_min": "12345678-modbus-battery_minimum_reserve",
    }
    assert set(ents) == set(uids)
    for key, uid in uids.items():
        assert re.search(ents[key]["unique_id_regex"], uid), (key, uid)
    near = {
        "battery_power_w": ("solar_net_1-power_flow-power_battery_charge",
                            "solar_net_1-power_flow-power_battery_discharge"),
        "grid_power_w": ("solar_net_1-power_flow-power_grid_import", "solar_net_1-power_flow-power_grid_export"),
        "load_power_w": ("solar_net_1-power_flow-power_load", "solar_net_1-power_flow-power_load_generated"),
        "active_power_w": ("OHM1-power_real_ac", "1-modbus-mppt_1_power_dc"),
        "pv_energy_total_kwh": ("1-energy_total", "solar_net_1-power_flow-energy_total"),
        "battery_temp_c": ("OHM1-temperature_channel_1",),
        "grid_import_total_kwh": ("OHM1-energy_real_ac_consumed", "M1-energy_reactive_ac_consumed"),
        "grid_export_total_kwh": ("M1-energy_reactive_ac_produced",),
        "soc_min": ("1-modbus-battery_charge_power_limit",),
    }
    for key, uids_near in near.items():
        for uid in uids_near:
            assert not re.search(ents[key]["unique_id_regex"], uid), (key, uid)
    assert ents["soc_min"]["domain"] == "number"
    assert all(e["domain"] == "sensor" for k, e in ents.items() if k != "soc_min")
    # Znaki Fronius = znaki rdzenia (bateria + = rozładowanie, sieć + = pobór); dom z "consumed" (+).
    assert all("transform" not in e for e in ents.values())


def test_entity_mode_is_not_available(profile):
    # Brak encji select trybu → tryb encji niedostępny (poprawnie); domena znana rdzeniowi.
    ents = profile.raw["ha"]["integrations"][0]["entities"]
    assert REQUIRED_WRITE_KEYS == ("mode",) and "mode" not in ents
    assert "fronius" in INVERTER_DOMAINS and "fronius" in CONFLICT_DOMAINS
    note = profile.raw["modbus"]["status_note"]
    assert "no mode select" in note and "conflict" in note


def test_engine_gaps_document_fronius():
    text = GAPS_FILE.read_text(encoding="utf-8")
    assert SECTION in text
    assert text.index(SECTION) < text.index("## Automatic verification ladder")
    section = text.split(SECTION, 1)[1].split("\n## ", 1)[0]
    for needle in ("40348", "40350", "40355", "40356", "40345", "40358", "40360", "40364", "40368",
                   "40343", "40253", "unit id 200", "float", "Inverter control via Modbus", "battery_minimum_reserve"):
        assert needle in section, needle
    core = text.split("## Core", 1)[1].split("\n## ", 1)[0]
    rows = core.splitlines()
    assert any("fronius-gen24" in line and "Dynamic SunSpec scale factors" in line for line in rows)
    assert any("fronius-gen24" in line and "One `unit_id` per profile" in line for line in rows)
    assert any("fronius-gen24" in line and "Capabilities are global" in line for line in rows)
    ladder = text.split("## Automatic verification ladder", 1)[1]
    assert any("fronius-gen24" in line and "forced test window" in line for line in ladder.splitlines())
    assert any("fronius-gen24" in line and "Inverter control via Modbus" in line for line in ladder.splitlines())
