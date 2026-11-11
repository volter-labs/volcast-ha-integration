"""Przełącznik zdalnego sterowania (`enable`) w sekwencji zapisów: włączenie czeka, aż nastawy przed
nim dojdą; wyłączenie (oddanie sterowania) idzie zawsze."""
import asyncio
from dataclasses import dataclass

import pytest

from custom_components.volcast.core.control.group_writes import async_run_group_writes, run_group_writes
from custom_components.volcast.core.entity_map import EntityWrite
from custom_components.volcast.core.params import Params
from custom_components.volcast.core.profile import load_builtin, profile_from_dict
from custom_components.volcast.core.registers import RegisterWrite, encode_writes
from custom_components.volcast.core.write_sequence import (
    DENIED, ERROR, OK, UNSUPPORTED, async_run_writes, run_writes,
)


@dataclass(frozen=True)
class W:
    key: str


def _ew(key, service="set_value"):
    domain = "switch" if service in ("turn_on", "turn_off") else ("select" if key == "mode" else "number")
    return EntityWrite(key, f"{domain}.{key}", domain, service, {})


ON, OFF = _ew("enable", "turn_on"), _ew("enable", "turn_off")
CP, DP, MODE = _ew("charge_power_w"), _ew("discharge_power_w"), _ew("mode", "select_option")


def _fake(results):
    calls = []

    def write(w):
        calls.append((w.key, getattr(w, "service", None)))
        return results.get(w.key, OK)
    return write, calls


@pytest.mark.parametrize("outcome", [ERROR, DENIED, UNSUPPORTED])
def test_switch_on_is_held_after_a_failed_set_point(outcome):
    write, calls = _fake({"charge_power_w": outcome})
    rep = run_writes([CP, ON, MODE], write)
    assert calls == [("charge_power_w", "set_value")]          # ani włączenia, ani trybu
    assert rep.held == ["enable"] and rep.mode_held and "enable" not in rep.written


def test_switch_on_after_successful_set_points_is_written():
    write, calls = _fake({})
    rep = run_writes([CP, DP, ON, MODE], write)
    assert [k for k, _ in calls] == ["charge_power_w", "discharge_power_w", "enable", "mode"]
    assert rep.written == ["charge_power_w", "discharge_power_w", "enable", "mode"] and rep.held == []


def test_switch_off_is_sent_even_after_a_failed_set_point():
    write, calls = _fake({"charge_power_w": ERROR})
    rep = run_writes([CP, DP, OFF], write)
    assert [k for k, _ in calls] == ["charge_power_w", "discharge_power_w", "enable"]
    assert rep.written == ["discharge_power_w", "enable"] and rep.failed == ["charge_power_w"]
    assert rep.held == []


def test_switch_of_unknown_direction_is_treated_as_on():
    write, calls = _fake({"charge_power_w": ERROR})
    rep = run_writes([W("charge_power_w"), W("enable"), W("mode")], write)
    assert [k for k, _ in calls] == ["charge_power_w"] and rep.held == ["enable"]


def test_failure_after_the_switch_does_not_hold_it():
    write, calls = _fake({"soc_min": ERROR})
    rep = run_writes([CP, ON, _ew("soc_min"), MODE], write)
    assert [k for k, _ in calls] == ["charge_power_w", "enable", "soc_min"] and rep.mode_held
    assert rep.written == ["charge_power_w", "enable"] and rep.held == []


def test_async_twin_holds_the_switch_the_same_way():
    write, calls = _fake({"charge_power_w": ERROR})

    async def awrite(w):
        return write(w)
    rep = asyncio.run(async_run_writes([CP, ON, MODE], awrite))
    assert calls == [("charge_power_w", "set_value")] and rep.held == ["enable"] and rep.mode_held
    write, calls = _fake({"charge_power_w": ERROR})
    rep = asyncio.run(async_run_writes([CP, OFF], awrite))
    assert [k for k, _ in calls] == ["charge_power_w", "enable"] and rep.held == []


def test_group_writes_hold_switch_and_mode_after_failed_set_point():
    write, calls = _fake({"charge_power_w": ERROR})
    rep = run_group_writes([CP, ON, MODE], write)
    assert [k for k, _ in calls] == ["charge_power_w"]
    assert rep.held == ["enable"] and rep.mode_held and rep.group_skipped


def test_async_group_writes_send_switch_off_after_failed_set_point():
    write, calls = _fake({"charge_power_w": ERROR})

    async def awrite(w):
        return write(w)
    rep = asyncio.run(async_run_group_writes([CP, OFF], awrite))
    assert [k for k, _ in calls] == ["charge_power_w", "enable"] and "enable" in rep.written
    write, calls = _fake({"charge_power_w": ERROR})
    rep = asyncio.run(async_run_group_writes([CP, ON, MODE], awrite))
    assert [k for k, _ in calls] == ["charge_power_w"] and rep.held == ["enable"]


def test_entity_and_register_writes_tell_the_switch_direction():
    from tests.core.test_entity_map import direction_profile_dict
    assert ON.turns_on is True and OFF.turns_on is False and CP.turns_on is None
    raw = direction_profile_dict()
    raw["write"]["enable"].update(on=0xAA, off=0x55)
    prof = profile_from_dict(raw)
    on, = encode_writes(Params(enable=True), prof)
    off, = encode_writes(Params(enable=False), prof)
    assert (on.value, on.turns_on, off.value, off.turns_on) == (0xAA, True, 0x55, False)
    assert on == RegisterWrite("enable", 40029, 0xAA)                   # równość bez zmian


def test_existing_profile_sequence_is_unchanged():
    """Regresja: profil bez przełącznika — po nieudanej nastawie wstrzymany tylko tryb, jak dotąd."""
    gw = load_builtin("goodwe-et")
    ws = encode_writes(Params(mode="sell_power", power_w=400.0, soc_min=20.0, export_limit_w=500.0,
                              export_limit_enabled=True), gw)
    assert all(w.turns_on is None for w in ws)
    rep = run_writes(ws, lambda w: ERROR if w.key == "soc_min" else OK)
    assert rep.held == [] and rep.mode_held and rep.failed == ["soc_min"]
    assert rep.written == [w.key for w in ws if w.key not in ("soc_min", "mode")]
