"""Wynik OK_ADJUSTED: zapis zastosowany z wartością rzeczywistą, nie zamówioną."""
import asyncio

from custom_components.volcast.core.control.cycle import WRITE, CycleDecision, ControlMemory, commit
from custom_components.volcast.core.control.group_writes import run_group_writes
from custom_components.volcast.core.profile import load_builtin
from custom_components.volcast.core.registers import RegisterWrite
from custom_components.volcast.core.write_sequence import (
    OK, OK_ADJUSTED, AdjustedOutcome, async_run_writes, run_writes)

GW = load_builtin("goodwe-et")


def test_adjusted_outcome_is_the_ok_adjusted_string():
    out = AdjustedOutcome(5000.0)
    assert out == OK_ADJUSTED and out.actual == 5000.0 and isinstance(out, str)


def test_run_writes_counts_adjusted_as_written_with_actual():
    ws = [RegisterWrite("power_w", 47512, 8000), RegisterWrite("mode", 47511, 10)]
    outcome = {"power_w": AdjustedOutcome(5000.0), "mode": OK}
    rep = run_writes(ws, lambda w: outcome[w.key])
    assert rep.written == ["power_w", "mode"] and rep.failed == [] and rep.mode_held is False
    assert rep.adjusted == ["power_w"] and rep.actual == {"power_w": 5000.0}

    async def aw(w):
        return outcome[w.key]
    assert asyncio.run(async_run_writes(ws, aw)) == rep


def test_group_writes_carry_actual():
    ws = [RegisterWrite("power_w", 47512, 8000), RegisterWrite("mode", 47511, 10)]
    rep = run_group_writes(ws, lambda w: AdjustedOutcome(5000.0) if w.key == "power_w" else OK)
    assert rep.written == ["power_w", "mode"] and rep.actual == {"power_w": 5000.0}
    assert rep.restored == [] and rep.adjusted == ["power_w"]


def test_commit_records_actual_value_not_requested():
    memory = ControlMemory.for_profile(GW)
    d = CycleDecision(WRITE, "ok", writes=[RegisterWrite("power_w", 47512, 8000)],
                      flat={"power_w": 8000.0}, target_kind="direct")
    rep = run_writes(d.writes, lambda w: AdjustedOutcome(5000.0))
    commit(d, rep, memory, 1000.0)
    assert memory.last_written["power_w"] == 5000.0
    assert memory.throttle.known("power_w") == 5000.0
