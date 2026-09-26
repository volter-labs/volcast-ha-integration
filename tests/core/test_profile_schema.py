import pytest

from custom_components.volcast.core.profile_schema import validate_profile
from tests.core.profile_fixtures import ms_profile, tw_profile


def test_fixtures_are_valid():
    assert validate_profile(ms_profile()) == []
    assert validate_profile(tw_profile()) == []


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
    # F9: type errors must produce a path error, never crash with TypeError
    (lambda p: p["limits"].update(rated_power_register=["rated_power_w"]), "$.limits.rated_power_register"),
    (lambda p: p["intents"]["sell"].update(mode=["sell_power"]), "$.intents.sell.mode"),
    (lambda p: p.update(neutral_mode=["auto"]), "$.neutral_mode"),
    (lambda p: p["read"]["load_power_w"]["sum"].append({"ref": ["pv_power_w"]}),
     "$.read.load_power_w.sum[2].ref"),
    (lambda p: p["write_policy"].update(order=[1, 2, 3, 4, 5]), "$.write_policy.order"),
    # F11: mode_setpoint requires max_direction_changes_per_hour >= 1
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
    # F9: type errors must produce a path error, never crash with TypeError
    (lambda p: p["tou"].update(field_order="soc,power_w,grid_charge,start"), "$.tou.field_order"),
])
def test_time_window_errors(mutate, needle):
    p = tw_profile()
    mutate(p)
    assert needle in _errs(p)


def test_time_window_ignores_zero_direction_changes():
    # F11: max_direction_changes_per_hour == 0 nie jest błędem dla time_window
    p = tw_profile()
    assert p["write_policy"]["max_direction_changes_per_hour"] == 0
    assert validate_profile(p) == []


def test_not_a_dict():
    assert validate_profile([]) == ["$: oczekiwano obiektu"]
