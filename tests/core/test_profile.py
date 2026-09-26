import json

import pytest

from custom_components.volcast.core.profile import (
    ProfileError, load_profile, profile_from_dict,
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
