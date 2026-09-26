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
