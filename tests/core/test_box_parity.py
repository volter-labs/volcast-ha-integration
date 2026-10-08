"""Parity of the direct (register) control cycle with the reference executor.

Each vector is one plan slot of the device contract, the inverter registers before the cycle
and the live readings. `box` is what the reference executor writes in that situation — the
register writes in its order (parameters first, mode last), derived from its mapper, guards,
live sell setpoint and applier, in steady state (its write memory equals the registers).
Numbers are anonymised and rounded; they follow the shapes seen on a live installation.

Vectors where the direct cycle already agrees are plain tests. Each known difference is an
`xfail(strict=False)` test whose expectation is the reference behaviour; the reason names it.
Differences where the direct cycle is deliberately safer are plain tests named
`test_intentional_*` — they guard the safer behaviour and must not be "fixed".
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from custom_components.volcast.core.control.conflict import DriftTracker
from custom_components.volcast.core.control.cycle import (
    BLOCKED, ControlMemory, Gates, Limits, Telemetry, decide_cycle)
from custom_components.volcast.core.control.target import RegisterTarget
from custom_components.volcast.core.modbus.reading import build_reading
from custom_components.volcast.core.profile import load_builtin
from custom_components.volcast.core.registers import RegisterImage
from custom_components.volcast.core.slot import parse_schedule
from tests.core.golden import T0
from tests.sim.fixtures import goodwe_words

GW = load_builtin("goodwe-et")
RATED_W = 8000.0
RESERVE = 8.0            # fallback.soc_reserve (self-consumption floor of the account strategy)
SELL_FLOOR = 10.0        # soc_target of sell slots
NOW_MONO = 1000.0

SOC_MIN, SOC_MAX, XSET, EXP_W, EXP_EN, MODE = 45356, 47760, 47512, 47510, 47509, 47511
AUTO, STANDBY, SELL, CHARGE, DISCHARGE = 1, 8, 10, 11, 12

# Registers of a settled inverter: neutral mode, stale Xset from an earlier charge, export
# limiter on at the connection limit, floor at the reserve.
SETTLED = {MODE: AUTO, XSET: 907, EXP_EN: 1, EXP_W: 8000, SOC_MIN: 8}

_SLOT_BASE = {"export_allowed": True, "export_limit_w": 8000, "price_pln_kwh": 0.8}

VECTORS = {
    # charge from grid at the full planned rate (power_w = capped AC rate from the SoC profile)
    "charge_grid_full": dict(
        slot={"mode": "charge", "charge_source": "grid", "power_w": 5000, "price_pln_kwh": 0.28},
        regs={}, box=[(XSET, 5000), (MODE, CHARGE)]),
    # partial-hour charge: power below the previous setpoint
    "charge_grid_partial": dict(
        slot={"mode": "charge", "charge_source": "grid", "power_w": 2041, "price_pln_kwh": 0.28},
        regs={XSET: 5000}, box=[(XSET, 2041), (MODE, CHARGE)]),
    # next charge hour with another power: only the setpoint changes
    "charge_grid_power_step": dict(
        slot={"mode": "charge", "charge_source": "grid", "power_w": 2041, "price_pln_kwh": 0.28},
        regs={MODE: CHARGE, XSET: 5000}, box=[(XSET, 2041)]),
    # sell: battery AC power 3893 W, house 500 W, no PV -> export setpoint 3393 W, floor 10 %
    "sell": dict(
        slot={"mode": "discharge", "discharge_purpose": "sell", "power_w": 3893,
              "soc_target": SELL_FLOOR, "price_pln_kwh": 1.16},
        regs={XSET: 2041}, pv=0.0, load=500.0,
        box=[(SOC_MIN, 10), (XSET, 3393), (MODE, SELL)]),
    # sell with the house above the planned battery power -> export setpoint 0 (battery covers house)
    "sell_house_above_battery": dict(
        slot={"mode": "discharge", "discharge_purpose": "sell", "power_w": 699,
              "soc_target": SELL_FLOOR, "price_pln_kwh": 1.5},
        regs={XSET: 2041}, pv=0.0, load=1200.0,
        box=[(SOC_MIN, 10), (XSET, 0), (MODE, SELL)]),
    # self-consumption with a described discharge; slot power is not a command
    "self_consume_self": dict(
        slot={"mode": "self_consume", "discharge_purpose": "self", "power_w": 99, "soc_target": RESERVE},
        regs={MODE: SELL, XSET: 3393, SOC_MIN: 10}, box=[(SOC_MIN, 8), (MODE, AUTO)]),
    # plain self-consumption carrying the default floor
    "self_consume": dict(
        slot={"mode": "self_consume", "soc_target": RESERVE},
        regs={MODE: CHARGE, XSET: 5000, SOC_MIN: 10}, box=[(SOC_MIN, 8), (MODE, AUTO)]),
    # self-consumption charging from PV surplus: auto, no setpoint, no floor
    "self_consume_pv": dict(
        slot={"mode": "self_consume", "charge_source": "pv", "power_w": 274},
        regs={MODE: STANDBY, XSET: 0}, box=[(MODE, AUTO)]),
    # charge from PV: auto
    "charge_pv": dict(
        slot={"mode": "charge", "charge_source": "pv", "power_w": 680},
        regs={MODE: CHARGE, XSET: 5000}, box=[(MODE, AUTO)]),
    # hold (export PV, battery still): standby with an explicit zero setpoint BEFORE the mode
    "hold": dict(
        slot={"mode": "hold", "price_pln_kwh": 0.45},
        regs={}, box=[(XSET, 0), (MODE, STANDBY)]),
    # idle at a negative price: standby, export limiter on at 0 W
    "idle_negative_price": dict(
        slot={"mode": "idle", "export_allowed": False, "export_limit_w": None, "price_pln_kwh": -0.05},
        regs={}, box=[(XSET, 0), (EXP_W, 0), (MODE, STANDBY)]),
    # discharge without a purpose (old/partial contract): fixed-rate discharge
    "discharge_forced": dict(
        slot={"mode": "discharge", "power_w": 2000, "soc_target": SELL_FLOOR, "price_pln_kwh": 1.2},
        regs={}, box=[(SOC_MIN, 10), (XSET, 2000), (MODE, DISCHARGE)]),
    # charge slot priced <= 0: export closed by the price guard (I-4)
    "charge_grid_price_not_positive": dict(
        slot={"mode": "charge", "charge_source": "grid", "power_w": 5000, "price_pln_kwh": 0.0},
        regs={XSET: 5000}, box=[(EXP_W, 0), (MODE, CHARGE)]),
    # export allowed without a ceiling: the reference switches the limiter OFF
    "export_allowed_no_ceiling": dict(
        slot={"mode": "self_consume", "soc_target": RESERVE, "export_limit_w": None},
        regs={}, box=[(EXP_EN, 0)]),
}

# Vectors whose register SET differs from the reference (value or presence).
_VALUE_DIFFS = {
    "sell": "direct mode writes the battery power as the sell setpoint (no live export conversion)",
    "sell_house_above_battery": "direct mode writes the battery power as the sell setpoint "
                                "(no live export conversion)",
    "export_allowed_no_ceiling": "an uncapped export slot leaves the limiter untouched "
                                 "(the reference switches it off)",
}
# Vectors whose set agrees but the write ORDER differs from the reference.
_ORDER_DIFFS = {
    "charge_grid_full": "rising power: mode is written before the setpoint (reference: setpoint first)",
    "sell": "rising power: mode is written before the setpoint (reference: setpoint first)",
    "discharge_forced": "rising power: mode is written before the setpoint (reference: setpoint first)",
    "idle_negative_price": "export limiter pair is written before the setpoint (reference: setpoint first)",
}


def _schedule(slot: dict):
    iso = lambda t: t.isoformat().replace("+00:00", "Z")  # noqa: E731
    raw = {"from": iso(T0 - timedelta(minutes=30)), "to": iso(T0 + timedelta(minutes=30)),
           **_SLOT_BASE, **slot}
    return parse_schedule({"schedule_id": "parity", "slots": [raw],
                           "fallback": {"mode": "self_consume", "soc_reserve": RESERVE},
                           "control_enabled": True})


def _reading(regs: dict, soc: float):
    words = goodwe_words()
    words.update({**SETTLED, **regs, 37007: int(soc or 0)})
    words.pop(SOC_MAX, None)
    return build_reading(GW, RegisterImage(words), at_mono=NOW_MONO, at_utc=T0)


def _direct(vec: dict, *, soc: float = 50.0, memory: ControlMemory | None = None,
            temp: float | None = 25.0, previous_soc: float | None = None, gap: float | None = None):
    memory = memory or ControlMemory.for_profile(GW)
    tele = Telemetry(soc=soc, soc_age_s=5.0, battery_temp_c=temp, previous_soc=previous_soc,
                     previous_soc_gap_s=gap, pv_power_w=vec.get("pv"), pv_age_s=5.0,
                     load_power_w=vec.get("load"), load_age_s=5.0)
    gates = Gates(consent=True, local_switch=True, control_mode="direct", verified=True)
    d = decide_cycle(profile=GW, schedule=_schedule(vec["slot"]), now_utc=T0, now_mono=NOW_MONO, tele=tele,
                     limits=Limits(rated_power_w=RATED_W), gates=gates, memory=memory,
                     target=RegisterTarget(_reading(vec.get("regs", {}), soc)))
    return d, [(w.addr, int(w.value)) for w in d.writes]


def _params(names, diffs):
    return [pytest.param(n, marks=pytest.mark.xfail(strict=False, reason=diffs[n])) if n in diffs
            else pytest.param(n) for n in names]


# ── register set: the same registers with the same values ──────────────────────────────────

@pytest.mark.parametrize("name", _params(VECTORS, _VALUE_DIFFS))
def test_direct_writes_the_same_registers_and_values(name):
    vec = VECTORS[name]
    _, writes = _direct(vec)
    assert sorted(writes) == sorted(vec["box"])


# ── write order: parameters first, mode last, setpoint before mode ────────────────────────

@pytest.mark.parametrize("name", _params(VECTORS, {**_ORDER_DIFFS, **_VALUE_DIFFS}))
def test_direct_writes_in_the_reference_order(name):
    vec = VECTORS[name]
    _, writes = _direct(vec)
    assert writes == vec["box"]


@pytest.mark.parametrize("name", [n for n, v in VECTORS.items() if any(r == MODE for r, _ in v["box"])])
def test_direct_mode_is_written_after_every_non_setpoint_parameter(name):
    """The part of the reference order the direct cycle keeps everywhere (floor and limiter first)."""
    _, writes = _direct(VECTORS[name])
    regs = [r for r, _ in writes]
    assert MODE in regs
    assert all(regs.index(r) < regs.index(MODE) for r in regs if r not in (MODE, XSET))


def test_sell_mode_and_floor_agree_even_where_the_setpoint_differs():
    _, writes = _direct(VECTORS["sell"])
    assert (MODE, SELL) in writes and (SOC_MIN, 10) in writes


# ── guards ────────────────────────────────────────────────────────────────────────────────

_SELLING = {MODE: SELL, XSET: 3000, SOC_MIN: 10}


def test_reserve_reached_in_sell_returns_to_auto_and_keeps_floor():
    """I-1 at SoC == reserve: discharge dropped (auto, no setpoint), floor stays >= reserve."""
    vec = dict(VECTORS["sell"], regs=_SELLING)
    d, writes = _direct(vec, soc=RESERVE)
    assert d.guard.invariant == "I-1"
    assert writes == [(MODE, AUTO)]


def test_soc_unknown_writes_nothing():
    """I-9: no SoC reading -> no writes at all; the inverter keeps its last state (both sides)."""
    d, writes = _direct(VECTORS["hold"], soc=None)  # type: ignore[arg-type]
    assert (d.status, d.reason) == (BLOCKED, "guard:I-9") and writes == []


@pytest.mark.xfail(strict=False, reason="reserve latch keeps discharge off until reserve + 3 pp and 30 min; "
                                        "the reference resumes as soon as SoC > reserve")
def test_discharge_resumes_once_soc_is_above_reserve():
    memory = ControlMemory.for_profile(GW)
    memory.latch.engaged(RESERVE, RESERVE, NOW_MONO - 100.0)      # engaged one cycle earlier
    vec = dict(VECTORS["discharge_forced"], regs={MODE: AUTO, XSET: 2000, SOC_MIN: 10})
    _, writes = _direct(vec, soc=RESERVE + 1.0, memory=memory)
    assert writes == [(MODE, DISCHARGE)]


@pytest.mark.xfail(strict=False, reason="a mapped temperature register without a value blocks the cycle; "
                                        "the reference treats an unknown temperature as OK")
def test_unknown_battery_temperature_does_not_block():
    _, writes = _direct(VECTORS["hold"], temp=None)
    assert writes == VECTORS["hold"]["box"]


@pytest.mark.xfail(strict=False, reason="SoC plausibility (jump faster than 4 pp/min) blocks the cycle; "
                                        "the reference has no such check")
def test_soc_jump_does_not_block():
    _, writes = _direct(VECTORS["hold"], soc=80.0, previous_soc=40.0, gap=60.0)
    assert writes == VECTORS["hold"]["box"]


# ── re-assert after a change on the inverter ─────────────────────────────────────────────

def test_first_drift_is_reasserted():
    """A value changed on the inverter (reading differs from our write) is written again."""
    memory = ControlMemory.for_profile(GW)
    memory.throttle.record({"mode": "battery_standby", "power_w": 0.0}, ["mode", "power_w"], NOW_MONO - 600.0)
    _, writes = _direct(dict(VECTORS["hold"], regs={MODE: AUTO, XSET: 0}), memory=memory)
    assert writes == [(MODE, STANDBY)]


@pytest.mark.xfail(strict=False, reason="a second change of the same key within 30 min is an owner takeover "
                                        "(control paused, owner value kept); the reference re-asserts forever")
def test_repeated_drift_never_pauses():
    drift = DriftTracker()
    assert drift.note_drift("mode", 0.0) is False
    assert drift.note_drift("mode", 120.0) is False


# ── deliberate deviations: the direct cycle is safer — keep ──────────────────────────────

def test_intentional_mode_waits_for_its_throttled_setpoint():
    """Sell -> hold while the sell setpoint was written < 60 s ago.

    The reference throttles the setpoint per register but still writes the mode, so standby
    would run on the old sell setpoint (standby reads it as a CHARGE setpoint). The direct
    cycle holds the mode until the zero setpoint can go first.
    """
    memory = ControlMemory.for_profile(GW)
    memory.throttle.record({"mode": "sell_power", "power_w": 3393.0}, ["mode", "power_w"], NOW_MONO - 30.0)
    d, writes = _direct(dict(VECTORS["hold"], regs={MODE: SELL, XSET: 3393}), memory=memory)
    assert (MODE, STANDBY) not in writes
    assert "mode_held" in d.notes or "group_held" in d.notes
