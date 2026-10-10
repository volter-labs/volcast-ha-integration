import json
import re
from pathlib import Path

import pytest

from custom_components.volcast.core.profile import builtin_ids, load_builtin
from custom_components.volcast.core.registers import RegisterImage, decode, read_values
from custom_components.volcast.core.transports.modbus_frames import parse_aa55_read

FRAMES = json.loads((Path(__file__).resolve().parents[1] / "golden" / "goodwe_et" / "frames.json").read_text())


@pytest.fixture(scope="module")
def image():
    blocks = {f["offset"]: parse_aa55_read(bytes.fromhex(f["response"]), 0xF7, f["count"])
              for f in FRAMES.values() if f["valid"]}
    return RegisterImage.from_blocks(blocks)


def test_all_builtin_profiles_load():
    for pid in builtin_ids():
        assert load_builtin(pid).id == pid


def test_goodwe_reads_match_reference_decoder(image):
    p = load_builtin("goodwe-et")
    v = read_values(p.raw["read"], image)
    # wartości oczekiwane = asercje test/host/test_goodwe_et.c
    assert v["soc"] == 83
    assert v["battery_temp_c"] == pytest.approx(31.0)
    assert v["battery_voltage_v"] == pytest.approx(304.9)
    assert v["battery_current_a"] == pytest.approx(-9.0)
    assert (v["pv_power_w"], v["battery_power_w"], v["active_power_w"], v["load_power_w"]) == (828, -2734, -2270, 364)
    assert v["pv_energy_total_kwh"] == pytest.approx(3389.5)
    assert v["grid_power_w"] == 2240                      # znak odwrócony: + = import
    assert v["grid_export_total_kwh"] == pytest.approx(1088.88, abs=0.01)
    assert v["grid_import_total_kwh"] == pytest.approx(2664.02, abs=0.01)
    assert (v["export_limit_enabled"], v["export_limit_w"], v["mode_value"], v["power_w"]) == (0, 16000, 11, 8846)
    assert v["soc_min"] == 5                              # 45356 wprost, nie 100 - x
    assert "soc_max" not in v                             # 47760 nieobsługiwany na GW8KN-ET


def test_goodwe_identification(image):
    p = load_builtin("goodwe-et")
    ident = p.raw["identify"]
    model = decode(ident["model_register"], image)
    assert model == "GW8KN-ET"
    assert any(re.search(r, model) for r in ident["model_regex"])
    assert decode(ident["registers"]["rated_power_w"], image) == 8000      # moc znamionowa z rejestru
    assert decode(ident["registers"]["serial"], image) == "GOLDENSERIAL0000"


def test_goodwe_policy_is_box_policy():
    p = load_builtin("goodwe-et")
    assert p.write_order == ("soc_min", "soc_max", "power_w", "export_limit_w", "export_limit_enabled", "mode")
    assert (p.min_interval_s, p.max_direction_changes_per_hour, p.max_state_age_s) == (60, 4, 300)
    assert (p.temp_min_c, p.temp_max_c) == (-10, 55)
    assert {m.name: m.value for m in p.modes.values()} == {
        "auto": 1, "charge_pv": 2, "battery_standby": 8, "sell_power": 10,
        "charge_battery": 11, "discharge_battery": 12}


def test_goodwe_modbus_is_verified_with_fc6():
    p = load_builtin("goodwe-et")
    m = p.modbus
    assert (m.status, m.write_function, m.max_read_registers) == ("verified", 6, 125)
    assert set(m.transport_options) == {"goodwe_udp", "modbus_tcp", "modbus_rtu"}
    # 300 ms jak Box: moduł Wi-Fi przy krótszym odstępie odpowiada poprzednią odpowiedzią
    assert m.transport_options["goodwe_udp"] == {"port": 8899, "timeout_ms": 2000, "gap_ms": 300}
    assert m.transport_options["modbus_tcp"] == {"port": 502, "timeout_ms": 2000, "gap_ms": 50}
    assert m.transport_options["modbus_rtu"] == {"port": 8899, "timeout_ms": 2000, "gap_ms": 100}
    assert m.identify_reads == ((35000, 33),)
    assert m.probe_keys == ("mode", "power_w", "soc_min", "soc_max", "export_limit_w", "export_limit_enabled")
    assert (p.nvm_budget.window_s, p.nvm_budget.per_key, p.nvm_budget.total) == (86400.0, 144, 600)


# Liczniki i limity BMS — wartości z ramek nagranych na GW8KN-ET (tests/golden/goodwe_et/frames.json).
_UNVERIFIED = ("load_energy_total_kwh", "load_energy_day_kwh", "battery_charge_total_kwh",
               "battery_discharge_total_kwh", "bms_charge_current_max_a", "bms_discharge_current_max_a")


def test_goodwe_et_counters_and_bms_reads_from_recorded_frames(image):
    v = read_values(load_builtin("goodwe-et").raw["read"], image)
    assert v["load_energy_total_kwh"] == pytest.approx(4315.5)
    assert v["load_energy_day_kwh"] == pytest.approx(39.8)
    assert v["battery_charge_total_kwh"] == pytest.approx(2495.1)
    assert v["battery_discharge_total_kwh"] == pytest.approx(1987.9)
    assert (v["bms_charge_current_max_a"], v["bms_discharge_current_max_a"]) == (9, 22)
    assert "battery_capacity_kwh" not in v              # mapa ET nie ma rejestru pojemności


def test_goodwe_et_unverified_reads_are_marked_and_never_written():
    from custom_components.volcast.control.device_io import LIVE_KEYS
    from custom_components.volcast.core.modbus.blocks import spec_addresses
    from custom_components.volcast.core.profile_schema import profile_warnings
    p = load_builtin("goodwe-et")
    read = p.raw["read"]
    for key in _UNVERIFIED:
        assert read[key]["verify"] is True and read[key]["note"], key
    assert {k for k, s in read.items() if s.get("verify")} == set(_UNVERIFIED)
    # rejestry do weryfikacji: poza zapisem, odczytem zwrotnym, SoC/temperaturą i odczytami nastawy sprzedaży
    written = {a for s in p.raw["write"].values() for a in spec_addresses(s)}
    unverified = {a for k in _UNVERIFIED for a in spec_addresses(read[k])}
    assert not unverified & written
    write_path = {"soc", "battery_temp_c", *LIVE_KEYS, *p.raw["write"], *p.modbus.probe_keys,
                  "mode_value", "battery_power_w", "active_power_w"}
    assert not write_path & set(_UNVERIFIED)
    for key in write_path & set(read):
        addrs = set(spec_addresses(read[key]))
        for term in read[key].get("sum", ()):
            if "ref" in term:
                addrs |= set(spec_addresses(read[term["ref"]]))
        assert not addrs & unverified, key
    # profil zweryfikowany z niezweryfikowanymi odczytami: ostrzeżenie, nie błąd
    assert sorted(w.split(".")[2] for w in profile_warnings(dict(p.raw))) == sorted(_UNVERIFIED)


def test_goodwe_et_limits_from_bms_registers(image):
    from custom_components.volcast.core.control.limits import register_limits
    p = load_builtin("goodwe-et")
    lim = p.raw["limits"]
    assert (lim["max_charge_register"], lim["max_discharge_register"]) == (
        "bms_charge_current_max_a", "bms_discharge_current_max_a")
    assert "capacity_register" not in lim
    v = read_values(p.raw["read"], image)
    # 9 A × 304,9 V = 2744 W, 22 A × 304,9 V = 6708 W — w dół do pełnych 100 W
    assert register_limits({"rated_power_w": 8000, "source": "registers"}, lim, v) == {
        "rated_power_w": 8000, "max_charge_w": 2700, "max_discharge_w": 6700, "source": "registers"}
    # przycięte do mocy znamionowej
    assert register_limits({"rated_power_w": 5000, "source": "registers"}, lim, v)["max_discharge_w"] == 5000


def test_goodwe_et_read_plan_covers_counters_in_existing_blocks():
    from custom_components.volcast.core.modbus.blocks import read_plan
    p = load_builtin("goodwe-et")
    plan = read_plan(p)
    covered = {a for b in plan for a in range(b[0], b[0] + b[1])}
    assert set(range(35203, 35211)) | {37004, 37005} <= covered      # 35209 u32 = 35209–35210
    # bloki mieszczą się w zakresach odczytanych poprawnie na GW8KN-ET (ramki golden)
    recorded = [(f["offset"], f["count"]) for f in FRAMES.values() if f["valid"]]
    for b in plan:
        if 35100 <= b[0] < 35225 or 37000 <= b[0] < 37024:
            assert any(o <= b[0] and b[0] + b[1] <= o + c for o, c in recorded), b
