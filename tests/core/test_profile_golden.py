"""Bramka zgodności możliwości z kluczami zapisu: moc wg kierunku."""
from types import SimpleNamespace

import pytest

from tests.core import profile_golden as pg


def _profile(write, *, by_direction=False, set_power_w=True):
    raw = {
        "control_model": "mode_setpoint",
        "write": write,
        "write_policy": {"power_by_direction": True} if by_direction else {},
        "capabilities": {
            "set_power_w": set_power_w,
            "set_soc_floor": False,
            "set_soc_ceiling": False,
            "limit_export": False,
        },
    }
    return SimpleNamespace(id="x", raw=raw)


PAIR = {"mode": {}, "charge_power_w": {}, "discharge_power_w": {}}


def test_direction_pair_with_flag_accepts_set_power():
    pg.assert_capabilities_match_writes(_profile(PAIR, by_direction=True))


def test_direction_pair_with_flag_rejects_undeclared_power():
    with pytest.raises(AssertionError):
        pg.assert_capabilities_match_writes(_profile(PAIR, by_direction=True, set_power_w=False))


@pytest.mark.parametrize("missing", ["charge_power_w", "discharge_power_w"])
def test_single_direction_set_point_rejected(missing):
    write = {k: v for k, v in PAIR.items() if k != missing}
    with pytest.raises(AssertionError):
        pg.assert_capabilities_match_writes(_profile(write, by_direction=True))


def test_direction_pair_without_flag_rejected():
    with pytest.raises(AssertionError):
        pg.assert_capabilities_match_writes(_profile(PAIR, by_direction=False))


def test_plain_power_w_unchanged():
    pg.assert_capabilities_match_writes(_profile({"mode": {}, "power_w": {}}))
