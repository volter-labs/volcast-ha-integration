"""Budżet zapisów NVM w trybie encji: każde wywołanie usługi zapisu planu; powrót do trybu bazowego poza nim."""
from __future__ import annotations

import asyncio
from collections import Counter

from custom_components.volcast.control.store import ControlStore

from .ha_fakes import GOODWE_ENTITIES as E
from .test_executor import make, ready


def _wall(ex) -> float:
    return ex._now_wall()


def test_plan_write_service_calls_are_counted_and_persisted(monkeypatch):
    h, ex = make(monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_tick()
    asyncio.run(go())
    written = [c[2]["entity_id"] for c in h.services.calls]
    counts = ex._memory.budget.counts(_wall(ex))
    assert sum(counts.values()) == len(written) > 0
    assert counts.get("mode") == 1 and counts.get("power_w") == 1
    stored = asyncio.run(ex._store.async_load())
    assert Counter(k for k, _ in stored.nvm_log) == Counter(counts)


def test_restore_service_calls_are_not_counted(monkeypatch):
    h, ex = make(monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_tick()
        before = dict(ex._memory.budget.counts(_wall(ex)))
        n = len(h.services.calls)
        await ex.async_set_consent(False)
        await ex.async_tick()
        return before, n
    before, n = asyncio.run(go())
    assert len(h.services.calls) > n and h.states.get(E["mode"]).state == "auto"      # powrót poszedł
    assert ex._memory.budget.counts(_wall(ex)) == before


def test_budget_survives_restart_and_blocks_writes_in_entity_mode(monkeypatch):
    h, ex = make(monkeypatch=monkeypatch)
    store = ControlStore(h, "e1")
    _, ex = make(h, store=store, monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        budget = ex._memory.budget
        for _ in range(budget.per_key):
            budget.note("power_w", _wall(ex))
        await ex._persist_budget()
        await ex.async_stop()
        _, ex2 = make(h, store=store, monkeypatch=monkeypatch)
        await ex2.async_start()
        await ex2.async_tick()
        return ex2
    ex2 = asyncio.run(go())
    assert ex2._memory.budget.counts(_wall(ex2))["power_w"] == ex2._memory.budget.per_key
    assert "nvm_budget" in (ex2.last_decision.notes or ())
    assert not any(c[2]["entity_id"] == E["power_w"] for c in h.services.calls)
