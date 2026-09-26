import json

import pytest

from custom_components.volcast.core.profile import ProfileError, load_profile
from custom_components.volcast.core.profile_schema import validate_profile
from tests.core.profile_fixtures import ms_profile, tw_profile


def test_fixtures_are_valid():
    assert validate_profile(ms_profile()) == []
    assert validate_profile(tw_profile()) == []


def test_oversized_int_is_profile_error_not_overflow(tmp_path):
    # `json.loads` przyjmuje int dowolnej długości; walidator nie może rzucić
    # OverflowError próbując go zamienić na float (np. w math.isfinite).
    raw = ms_profile()
    raw["write_policy"]["min_interval_s"] = int("1" + "0" * 400)
    f = tmp_path / f"{raw['id']}.json"
    f.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ProfileError) as ei:
        load_profile(f)
    assert any("$.write_policy.min_interval_s" in e for e in ei.value.errors)


def _errs(p):
    return "\n".join(validate_profile(p))


@pytest.mark.parametrize("mutate,needle", [
    (lambda p: p.update(extra=1), "$.extra"),
    (lambda p: p.pop("id"), "$.id"),
    (lambda p: p.update(id="GoodWe ET"), "$.id"),
    (lambda p: p.update(schema_version=2), "$.schema_version"),
    (lambda p: p.update(status="beta"), "$.status"),
    (lambda p: p["read"].update(sock={"addr": 1, "type": "u16"}), "$.read.sock"),
    (lambda p: p["read"]["soc"].update(type="u8"), "$.read.soc.type"),
    (lambda p: p["read"]["soc"].update(addr=70000), "$.read.soc.addr"),
    (lambda p: p["read"]["load_power_w"]["sum"].append({"ref": "nope"}), "$.read.load_power_w.sum[2].ref"),
    (lambda p: p["intents"]["sell"].update(mode="sell"), "$.intents.sell.mode"),
    (lambda p: p["intents"].pop("standby"), "$.intents.standby"),
    (lambda p: p.update(neutral_mode="general"), "$.neutral_mode"),
    (lambda p: p["write_policy"].update(order=["mode", "soc_min", "power_w",
                                               "export_limit_w", "export_limit_enabled"]),
     "$.write_policy.order"),
    (lambda p: p["write_policy"]["order"].remove("soc_min"), "$.write_policy.order"),
    (lambda p: p["ha"]["integrations"][0]["entities"]["soc"].update(unique_id_regex="(["),
     "unique_id_regex"),
    (lambda p: p["ha"]["integrations"][0]["entities"].update(foo={"domain": "sensor",
                                                                  "unique_id_regex": "x"}),
     "entities.foo"),
    (lambda p: p["limits"].update(rated_power_register="nope"), "$.limits.rated_power_register"),
    (lambda p: p["capabilities"].pop("standby"), "$.capabilities.standby"),
    (lambda p: p["capabilities"].update(time_windows=6), "$.capabilities.time_windows"),
    (lambda p: p["write"]["power_w"].update(encode="percent"), "$.write.power_w.encode"),
    # zły typ zamiast tekstu → błąd ze ścieżką, nigdy TypeError
    (lambda p: p["limits"].update(rated_power_register=["rated_power_w"]), "$.limits.rated_power_register"),
    (lambda p: p["intents"]["sell"].update(mode=["sell_power"]), "$.intents.sell.mode"),
    (lambda p: p.update(neutral_mode=["auto"]), "$.neutral_mode"),
    (lambda p: p["baseline"].update(mode=["auto"]), "$.baseline.mode"),
    (lambda p: p["read"]["load_power_w"]["sum"].append({"ref": ["pv_power_w"]}),
     "$.read.load_power_w.sum[2].ref"),
    (lambda p: p["write_policy"].update(order=[1, 2, 3, 4, 5]), "$.write_policy.order"),
    (lambda p: p["identify"].update(registers=["x"]), "$.identify.registers"),
    (lambda p: p["modes"]["auto"].update(value=[1]), "$.modes.auto.value"),
    (lambda p: p["read"]["pv_power_w"].update(scale=0.1), "$.read.pv_power_w.scale"),
    # liczby niekończone (NaN/Infinity) nie mogą przejść walidacji
    (lambda p: p["limits"]["battery_temp_c"].update(min=float("nan")), "$.limits.battery_temp_c.min"),
    (lambda p: p["write_policy"].update(min_interval_s=float("inf")), "$.write_policy.min_interval_s"),
    # mode_setpoint wymaga co najmniej 1 zmiany kierunku na godzinę
    (lambda p: p["write_policy"].update(max_direction_changes_per_hour=0),
     "$.write_policy.max_direction_changes_per_hour"),
])
def test_mode_setpoint_errors(mutate, needle):
    p = ms_profile()
    mutate(p)
    assert needle in _errs(p)


@pytest.mark.parametrize("mutate,needle", [
    (lambda p: p["write"]["tou_program"].update(count=5), "$.write.tou_program.count"),
    (lambda p: p["intents"].update(self_consume=None), "$.intents.self_consume"),
    (lambda p: p["intents"]["charge_grid"].update(soc="full"), "$.intents.charge_grid.soc"),
    (lambda p: p["tou"].update(time_step_min=7), "$.tou.time_step_min"),
    (lambda p: p["tou"].update(field_order=["soc", "soc", "power_w", "start"]), "$.tou.field_order"),
    (lambda p: p.pop("tou"), "$.tou"),
    (lambda p: p["write"]["tou_program"]["grid_charge"].pop("bit"), "grid_charge.bit"),
    # zły typ zamiast listy → błąd ze ścieżką, nigdy TypeError
    (lambda p: p["tou"].update(field_order="soc,power_w,grid_charge,start"), "$.tou.field_order"),
    # liczby niekończone (NaN/Infinity) nie mogą przejść walidacji
    (lambda p: p["tou"].update(soc_tolerance_pp=float("nan")), "$.tou.soc_tolerance_pp"),
])
def test_time_window_errors(mutate, needle):
    p = tw_profile()
    mutate(p)
    assert needle in _errs(p)


def test_time_window_ignores_zero_direction_changes():
    # max_direction_changes_per_hour == 0 nie jest błędem dla time_window
    p = tw_profile()
    assert p["write_policy"]["max_direction_changes_per_hour"] == 0
    assert validate_profile(p) == []


def test_not_a_dict():
    assert validate_profile([]) == ["$: oczekiwano obiektu"]


def test_baseline_export_flag_optional_and_negate_transform_allowed():
    p = ms_profile()
    p["baseline"] = {"mode": "auto"}
    p["ha"]["integrations"][0]["entities"]["grid_power_w"] = {
        "domain": "sensor", "unique_id_regex": "^x-", "transform": "negate"}
    assert validate_profile(p) == []


def test_baseline_export_flag_when_present_must_be_bool():
    p = ms_profile()
    p["baseline"]["export_limit_enabled"] = "yes"
    assert "$.baseline.export_limit_enabled" in _errs(p)
