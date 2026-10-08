import json

import pytest

from custom_components.volcast.core.profile import (
    ProfileError, builtin_ids, load_builtin, load_profile, profile_from_dict,
)
from tests.core.profile_fixtures import ms_profile, tw_profile


def test_mode_setpoint_model():
    p = profile_from_dict(ms_profile())
    assert p.mode_value("sell_power") == 10
    assert p.mode_by_value(8).name == "battery_standby"
    assert p.mode_by_value(99) is None
    assert p.mode_direction("charge_battery") == "charge"
    assert p.write_order[-1] == "mode"
    assert (p.temp_min_c, p.temp_max_c, p.max_direction_changes_per_hour) == (-10, 55, 4)
    assert p.tou_programs == 0
    assert p.intent("standby") == {"mode": "battery_standby", "power": "zero"}


def test_time_window_model():
    p = profile_from_dict(tw_profile())
    assert (p.tou_programs, p.time_step_min) == (6, 5)
    assert p.intent("sell") is None
    assert p.tou_field_order == ("soc", "power_w", "grid_charge", "start")


def test_invalid_profile_raises_with_paths():
    raw = ms_profile()
    raw["neutral_mode"] = "general"
    with pytest.raises(ProfileError) as ei:
        profile_from_dict(raw)
    assert any("$.neutral_mode" in e for e in ei.value.errors)


def test_file_name_must_match_id(tmp_path):
    f = tmp_path / "other.json"
    f.write_text(json.dumps(ms_profile()), encoding="utf-8")
    with pytest.raises(ProfileError, match="nazwa pliku"):
        load_profile(f)


def test_bad_json_is_profile_error(tmp_path):
    f = tmp_path / "test-ms.json"
    f.write_text("{", encoding="utf-8")
    with pytest.raises(ProfileError):
        load_profile(f)


def test_duplicate_json_key_is_profile_error(tmp_path):
    good = json.dumps(ms_profile())
    # ten sam klucz dwa razy na najwyższym poziomie — „ostatni wygrywa" ukryłby błąd
    dup = good[:-1] + ', "unit_id": 1}'
    f = tmp_path / "test-ms.json"
    f.write_text(dup)
    with pytest.raises(ProfileError, match="unit_id"):
        load_profile(f)


def test_duplicate_nested_json_key_is_profile_error(tmp_path):
    f = tmp_path / "test-ms.json"
    f.write_text(json.dumps(ms_profile()).replace(
        '"neutral_mode": "auto"', '"neutral_mode": "auto", "neutral_mode": "auto"', 1))
    with pytest.raises(ProfileError, match="neutral_mode"):
        load_profile(f)


@pytest.mark.parametrize("bad", ["../profiles/goodwe-et", "goodwe-et/../goodwe-et", "/etc/passwd",
                                 "GoodWe-ET", "goodwe_et", "", "goodwe-et.json"])
def test_load_builtin_rejects_non_id(bad):
    with pytest.raises(ProfileError, match=r"\$\.id"):
        load_builtin(bad)


def test_builtin_profiles_have_no_duplicate_keys():
    for pid in builtin_ids():
        assert load_builtin(pid).id == pid


@pytest.mark.parametrize("profile_status,modbus_status,expected", [
    ("draft", "draft", False), ("verified", "draft", False),
    ("draft", "verified", False), ("verified", "verified", True),
])
def test_direct_verified_needs_both_statuses(profile_status, modbus_status, expected):
    from custom_components.volcast.core.profile import direct_verified
    raw = ms_profile()
    raw["status"] = profile_status
    raw["modbus"]["status"] = modbus_status
    assert direct_verified(profile_from_dict(raw)) is expected


def test_modbus_spec_and_nvm_budget_parsed():
    from custom_components.volcast.core.profile import ModbusSpec, NvmBudget
    raw = ms_profile()
    raw["write_policy"]["nvm_budget"] = {"window_h": 12, "per_key": 10, "total": 30}
    p = profile_from_dict(raw)
    assert isinstance(p.modbus, ModbusSpec)
    assert p.modbus.transport_options["goodwe_udp"] == {"port": 8899, "timeout_ms": 2000, "gap_ms": 50}
    assert p.modbus.identify_reads == ((35000, 33),)
    assert p.nvm_budget == NvmBudget(window_s=12 * 3600.0, per_key=10, total=30)
    assert profile_from_dict(ms_profile()).nvm_budget is None
    with pytest.raises(TypeError):
        p.modbus.transport_options["goodwe_udp"]["port"] = 1       # tylko do odczytu


def test_readback_settle_default_and_override():
    from custom_components.volcast.core.profile import DEFAULT_READBACK_SETTLE_S
    assert DEFAULT_READBACK_SETTLE_S == 1.5
    assert profile_from_dict(ms_profile()).readback_settle_s == 1.5          # pole nieobecne
    raw = ms_profile()
    raw["write_policy"]["readback_settle_s"] = 0.4
    assert profile_from_dict(raw).readback_settle_s == 0.4
    gw = load_builtin("goodwe-et")
    assert gw.raw["write_policy"]["readback_settle_s"] == 1.5 and gw.readback_settle_s == 1.5
