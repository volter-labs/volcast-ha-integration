"""Wykonawca w trybie bezpośrednim (rejestry): tryb+nastawa, okna czasowe, powrót, przejęcie, próba.

Wszystko na symulatorach z pętli zwrotnej (`tests/sim`); profile przełączone na `verified`
wyłącznie w teście (`profile_from_dict`).
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from custom_components.volcast.const import DOMAIN
from custom_components.volcast.control import direct as direct_mod
from custom_components.volcast.control import executor as ex_mod
from custom_components.volcast.control.device_io import DirectIO
from custom_components.volcast.control.direct import DirectConnection, target_fingerprint
from custom_components.volcast.control.executor import VolcastExecutor
from custom_components.volcast.control.store import ControlState, ControlStore
from custom_components.volcast.core.control.select import ProfileChoice
from custom_components.volcast.core.modbus.identity import device_fingerprint
from custom_components.volcast.core.modbus.writer import NoWriteWriter
from custom_components.volcast.core.profile import load_builtin, profile_from_dict
from custom_components.volcast.core.registers import RegisterImage
from custom_components.volcast.core.slot import parse_schedule
from custom_components.volcast.core.write_sequence import DENIED
from tests.control.test_direct_connection import _factory
from tests.control.test_executor import plan, sell_ban
from tests.core.control.tou_helpers import DAY0, NOW as TOU_NOW, SELF, iso
from tests.core.transports.helpers import FakeClock
from tests.sim.fixtures import deye_words, goodwe_words

SALT = bytes(range(16))
GW_NOW = __import__("tests.control.ha_fakes", fromlist=["NOW"]).NOW
MODE, POWER, SOC_MIN, SOC_MAX, EXPORT_W, EXPORT_EN = 47511, 47512, 45356, 47760, 47510, 47509
TOU_EN = 146


def _thaw(v):
    if isinstance(v, Mapping):
        return {k: _thaw(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_thaw(x) for x in v]
    return v


def verified(pid: str, *, modbus: str = "verified"):
    raw = _thaw(load_builtin(pid).raw)
    raw["status"] = "verified"
    raw["modbus"]["status"] = modbus
    return profile_from_dict(raw)


GW_V = verified("goodwe-et")
GW_DRAFT = verified("goodwe-et", modbus="draft")
DEYE_V = verified("deye-sg")


def gw_target(sim, kind="goodwe_udp", **over):
    fp = device_fingerprint(SALT, GW_V, RegisterImage(goodwe_words()))
    return {"profile_id": "goodwe-et", "transport": kind, "host": sim.host, "port": sim.port, "unit_id": 247,
            "device_fp": fp, "unreadable": ["soc_max"],
            "capabilities": {k: True for k in GW_V.modbus.probe_keys}, **over}


def deye_target(sim, **over):
    fp = device_fingerprint(SALT, DEYE_V, RegisterImage(deye_words()))
    return {"profile_id": "deye-sg", "transport": "modbus_rtu", "host": sim.host, "port": sim.port, "unit_id": 1,
            "device_fp": fp, "unreadable": [], "capabilities": {"tou": True}, **over}


@pytest.fixture
def issues(monkeypatch):
    created, deleted = [], []

    def create(hass, domain, issue_id, **kw):
        created.append((issue_id, kw.get("translation_key"), kw.get("translation_placeholders")))

    for mod in (ex_mod, direct_mod):
        monkeypatch.setattr(mod.ir, "async_create_issue", create)
        monkeypatch.setattr(mod.ir, "async_delete_issue", lambda hass, domain, issue_id: deleted.append(issue_id))
    return SimpleNamespace(created=created, deleted=deleted,
                           keys=lambda: [k for _, k, _ in created])


class TickClock(FakeClock):
    """Zegar monotoniczny testów, który — jak prawdziwy — rośnie przy każdym odczycie (1 ms)."""

    def __call__(self) -> float:
        self.now += 0.001
        return self.now


class Harness:
    def __init__(self, make_hass, profile, target, *, options=None, trial=False, store=None, clock=None,
                 utc=None, rated=8000.0, entry_id="e1") -> None:
        self.clock = clock or TickClock()
        self.options = options if options is not None else {"control_mode": "direct", "direct_target": target}
        self.entry = SimpleNamespace(domain=DOMAIN, entry_id=entry_id, options=self.options, data={},
                                     disabled_by=None)
        self.hass = make_hass(entries=[self.entry])
        self.store = store or ControlStore(self.hass, entry_id)
        self.profile, self.target, self.trial = profile, target, trial
        self.utc = utc or (lambda: GW_NOW + timedelta(seconds=30))
        self.rated = rated
        self._compose()

    def _compose(self) -> None:
        t = self.target
        self.conn = DirectConnection(self.hass, self.entry, self.profile, t, trial=self.trial, salt=SALT,
                                     transport_factory=_factory(), allow_loopback=True, clock=self.clock,
                                     unreadable=t.get("unreadable", ()))
        self.io = DirectIO(self.conn, self.profile, trial=self.trial, unreadable=t.get("unreadable", ()),
                           capabilities=t.get("capabilities"), salt=SALT, clock=self.clock)
        self.ex = VolcastExecutor(self.hass, self.entry, choice=ProfileChoice(self.profile, None, None), mapped={},
                                  rated_power_w=self.rated, store=self.store, clock=self.clock, utcnow=self.utc,
                                  io=self.io)

    async def start(self, *, raw=None, consent=True, local=True) -> "Harness":
        await self.conn.async_start()
        await self.ex.async_start()
        raw = raw or plan()
        await self.ex.async_on_plan(raw, parse_schedule(raw))
        await self.ex.async_set_consent(consent)
        await self.ex.async_set_local_switch(local)
        await self.conn.async_poll()
        return self

    async def restart(self) -> None:
        await self.ex.async_stop()
        await self.conn.async_stop()
        self._compose()
        await self.conn.async_start()
        await self.ex.async_start()
        await self.conn.async_poll()

    async def cycle(self, advance: float = 61.0) -> None:
        self.clock.advance(advance)
        await self.conn.async_poll()
        await self.ex.async_tick()

    async def close(self) -> None:
        await self.ex.async_stop()
        await self.conn.async_stop()


def regs(bank) -> list[int]:
    return [a for a, _ in bank.writes]


def gw_raw_word(bank, addr):
    return bank.read(addr, 1)[0]


# ── bramki ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["no_consent", "local_off", "trial", "draft", "paused", "conflict",
                                  "identity_unknown", "identity_mismatch", "identity_pending"])
async def test_direct_writes_only_with_all_gates(make_hass, goodwe_udp_sim, goodwe_bank, sim_faults, issues, case):
    target = gw_target(goodwe_udp_sim)
    options = None
    if case == "trial":
        options = {"direct_trial": True, "direct_target": target}
    if case == "identity_unknown":
        target = {**target, "device_fp": None}
    h = Harness(make_hass, GW_DRAFT if case == "draft" else GW_V, target, options=options, trial=case == "trial")
    try:
        await h.start(consent=case != "no_consent", local=case != "local_off")
        if case == "paused":
            h.ex._memory.paused_until = h.clock() + 3600.0
        if case == "conflict":
            h.conn.stats.stray += 3
            await h.conn.async_poll()
            assert h.conn.conflict
        if case == "identity_mismatch":
            for a in range(35003, 35011):
                goodwe_bank.poke(a, 0x5A5A)
            h.clock.advance(3601.0)
            await h.conn.async_poll()
            assert h.conn.identity == "mismatch"
        if case == "identity_pending":
            h.conn.stats.peer_resets += 1
            sim_faults.drop_next = 1000
        await h.ex.async_tick()
        assert goodwe_bank.writes == []
        assert h.ex.last_decision.status != "write"
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_direct_sell_slot_end_to_end(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await Harness(make_hass, GW_V, gw_target(goodwe_udp_sim)).start()
    try:
        await h.ex.async_tick()
        assert h.ex.last_decision.status == "write"
        assert gw_raw_word(goodwe_bank, MODE) == 10 and gw_raw_word(goodwe_bank, POWER) == 2000
        assert h.ex.owned and h.ex._state.owner == {
            "profile": "goodwe-et", "mode": "direct", "target": target_fingerprint(h.target, SALT),
            "device": h.target["device_fp"]}
        assert SOC_MAX not in regs(goodwe_bank)                  # bez odczytu zwrotnego — nigdy pisany
        n = len(goodwe_bank.writes)
        await h.cycle()
        assert len(goodwe_bank.writes) == n and h.ex.last_decision.reason == "nothing_to_write"
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_direct_sell_warns_once_per_slot_about_missing_live_conversion(
        make_hass, goodwe_udp_sim, goodwe_bank, issues, caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.volcast")
    text = "live sell conversion is not available in direct register mode"
    count = lambda: len([r for r in caplog.records if text in r.getMessage()])  # noqa: E731
    h = await Harness(make_hass, GW_V, gw_target(goodwe_udp_sim)).start()
    try:
        await h.ex.async_tick()
        for _ in range(4):
            await h.cycle()
        d = h.ex.last_decision
        assert "sell_live_unavailable" in d.notes
        assert gw_raw_word(goodwe_bank, POWER) == 2000            # nastawa z planu, bez przeliczenia
        assert count() == 1 and all(r.levelno == logging.WARNING for r in caplog.records if text in r.getMessage())
        start, end, intent = d.sell_live_unavailable
        h.ex._warn_sell_suspended(replace(d, sell_live_unavailable=(end, end + (end - start), intent)))
        assert count() == 2                                        # nowy slot — nowe ostrzeżenie
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_probe_unsupported_seeds_memory(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    target = gw_target(goodwe_udp_sim, capabilities={"export_limit_enabled": False, "mode": True})
    charge = plan(slots=[{"from": "2026-09-23T10:00:00Z", "to": "2026-09-23T11:00:00Z", "mode": "charge",
                          "charge_source": "grid", "power_w": 3000, "soc_target": 90, "price_pln_kwh": 0.2}])
    h = await Harness(make_hass, GW_V, target).start(raw=charge)
    try:
        assert {"soc_max", "export_limit_enabled"} <= h.ex._memory.unsupported
        await h.ex.async_tick()
        assert h.ex.last_decision.status == "write"
        assert SOC_MAX not in regs(goodwe_bank) and EXPORT_EN not in regs(goodwe_bank)
        assert "soc_max" in h.ex.last_decision.dropped_unsupported
    finally:
        await h.close()


# ── próba (dry run) ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_trial_computes_dry_run_decisions_without_control_mode(make_hass, goodwe_udp_sim, goodwe_bank):
    target = gw_target(goodwe_udp_sim)
    h = await Harness(make_hass, GW_V, target, options={"direct_trial": True, "direct_target": target},
                      trial=True).start()
    try:
        await h.ex.async_tick()
        d = h.ex.last_decision
        assert d.status == "dry_run" and d.summary()["would_write"]
        assert goodwe_bank.writes == [] and not h.ex.owned
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_trial_never_writes_even_if_gates_open(make_hass, goodwe_udp_sim, goodwe_bank):
    target = gw_target(goodwe_udp_sim)
    h = await Harness(make_hass, GW_V, target, options={"control_mode": "direct", "direct_trial": True,
                                                         "direct_target": target}, trial=True).start()
    try:
        assert isinstance(h.io.writer, NoWriteWriter)
        await h.ex.async_tick()
        assert h.ex.last_decision.status == "dry_run" and goodwe_bank.writes == []
        assert h.io.writer.blocked_attempts == 0                  # bramki zatrzymują wcześniej
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_trial_decision_requires_confirmed_identity(make_hass, goodwe_udp_sim):
    target = {**gw_target(goodwe_udp_sim), "device_fp": None}
    h = await Harness(make_hass, GW_V, target, options={"direct_trial": True, "direct_target": target},
                      trial=True).start()
    try:
        await h.ex.async_tick()
        assert h.ex.last_decision.status == "blocked" and h.ex.last_decision.reason == "identity"
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_trial_refused_while_owned(make_hass, goodwe_udp_sim, goodwe_bank):
    target = gw_target(goodwe_udp_sim)
    store = ControlStore(make_hass(), "e1")
    h = await Harness(make_hass, GW_V, target, store=store).start()
    await h.ex.async_tick()
    assert h.ex.owned
    await h.close()
    n = len(goodwe_bank.writes)
    t = Harness(make_hass, GW_V, target, options={"direct_trial": True, "direct_target": target}, trial=True,
                store=store)
    await t.start()
    try:
        await t.ex.async_tick()
        assert t.ex.last_decision.reason == "trial_while_owned" and t.ex.owned
        assert len(goodwe_bank.writes) == n and t.io.writer.blocked_attempts == 0
    finally:
        await t.close()


# ── powrót do trybu bazowego ──────────────────────────────────────────────


async def _owned_sell(make_hass, sim, bank, **kw):
    bank.poke(EXPORT_EN, 1)                                  # właściciel: ogranicznik eksportu włączony (plan go nie rusza)
    h = await Harness(make_hass, GW_V, gw_target(sim), **kw).start()
    await h.ex.async_tick()
    assert h.ex.owned and gw_raw_word(bank, MODE) == 10 and gw_raw_word(bank, EXPORT_EN) == 1
    return h


@pytest.mark.asyncio
async def test_restore_on_consent_withdrawal_direct(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        await h.ex.async_set_consent(False)
        await h.cycle()
        assert h.ex.last_decision.status == "restore" and not h.ex.owned
        assert gw_raw_word(goodwe_bank, MODE) == 1 and gw_raw_word(goodwe_bank, EXPORT_EN) == 1
        assert h.ex._state.snapshot == {} and h.ex._state.owner == {}
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_restore_skips_echo_only_keys(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    charge = plan(slots=[{"from": "2026-09-23T10:00:00Z", "to": "2026-09-23T11:00:00Z", "mode": "charge",
                          "charge_source": "grid", "power_w": 3000, "soc_target": 90, "price_pln_kwh": 0.2}])
    h = await Harness(make_hass, GW_V, gw_target(goodwe_udp_sim)).start(raw=charge)
    try:
        await h.ex.async_tick()
        assert h.ex.owned and "soc_max" not in h.ex._state.snapshot
        await h.ex.async_set_local_switch(False)
        await h.cycle()
        assert not h.ex.owned and SOC_MAX not in regs(goodwe_bank)
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_revoke_restores_despite_stray_conflict(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        h.conn.stats.stray += 3
        await h.conn.async_poll()
        assert h.conn.conflict
        await h.ex.async_set_consent(False)
        await h.ex.async_tick()
        assert not h.ex.owned and gw_raw_word(goodwe_bank, MODE) == 1
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_restore_blocked_on_identity_mismatch(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        for a in range(35003, 35011):
            goodwe_bank.poke(a, 0x5A5A)
        h.clock.advance(3601.0)
        await h.conn.async_poll()
        n = len(goodwe_bank.writes)
        await h.ex.async_set_consent(False)
        await h.ex.async_tick()
        await h.ex.async_restore_now()
        assert len(goodwe_bank.writes) == n and h.ex.owned
        assert h.ex.last_decision.reason == "identity"
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_partial_restore_writes_are_ours_no_takeover(make_hass, goodwe_udp_sim, goodwe_bank, sim_faults,
                                                           issues):
    goodwe_bank.poke(EXPORT_EN, 0)                             # właściciel: ogranicznik wyłączony
    h = await Harness(make_hass, GW_V, gw_target(goodwe_udp_sim)).start(raw=sell_ban())
    try:
        await h.ex.async_tick()
        assert h.ex.owned and gw_raw_word(goodwe_bank, MODE) == 10 and gw_raw_word(goodwe_bank, EXPORT_EN) == 1
        goodwe_bank.readonly.add(EXPORT_EN)                    # przełącznik eksportu nie wraca (wyjątek 2)
        await h.ex.async_set_consent(False)
        for _ in range(3):
            await h.cycle()
        assert h.ex.owned and gw_raw_word(goodwe_bank, MODE) == 1
        assert not h.ex.paused and "foreign_control" not in " ".join(filter(None, issues.keys()))
        assert h.ex._memory.last_written.get("mode") == "auto"
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_owner_bound_to_target_fingerprint(make_hass, goodwe_udp_sim, goodwe_bank, caplog, issues):
    store = ControlStore(make_hass(), "e1")
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank, store=store)
    await h.close()
    moved = {**h.target, "unit_id": 246}                      # inny cel (np. zmiana adresu)
    h2 = Harness(make_hass, GW_V, moved, store=store)
    with caplog.at_level(logging.WARNING):
        await h2.ex.async_start()
    try:
        assert not h2.ex.owned and h2.ex._state.snapshot == {} and h2.ex._state.tou_snapshot is None
        assert "different inverter" in caplog.text
    finally:
        await h2.close()


# ── budżet NVM ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_budget_exhausted_forced_mode_restores_to_auto(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        budget = h.ex._memory.budget
        for _ in range(budget.per_key):
            budget.note("power_w", h.utc().timestamp())
        new = plan(power=3000, sid="p2")
        await h.ex.async_on_plan(new, parse_schedule(new))
        await h.cycle()
        assert h.ex.last_decision.status == "restore" and h.ex.last_decision.reason == "nvm_budget"
        assert gw_raw_word(goodwe_bank, MODE) == 1 and h.ex.owned
        assert h.ex._memory.last_written["mode"] == "auto"
        assert "nvm_budget" in issues.keys()
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_budget_persisted_across_restart(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        sent = len(h.ex._memory.budget.to_list())
        assert sent == len(goodwe_bank.writes) > 0              # każda ramka zapisu policzona
        assert len(h.ex._state.nvm_log) == sent
        await h.restart()
        assert len(h.ex._memory.budget.to_list()) == sent
        assert h.ex._memory.budget.exhausted({"power_w"}, h.utc().timestamp()) == set()
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_budget_hit_raises_repair_issue(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await Harness(make_hass, GW_V, gw_target(goodwe_udp_sim)).start(
        raw=plan(slots=[{"from": "2026-09-23T10:00:00Z", "to": "2026-09-23T11:00:00Z", "mode": "self_consume",
                         "price_pln_kwh": 0.2}]))
    try:
        for _ in range(h.ex._memory.budget.total):
            h.ex._memory.budget.note("other", h.utc().timestamp())
        await h.ex.async_tick()
        assert "nvm_budget" in h.ex.last_decision.notes and goodwe_bank.writes == []
        assert ("nvm_budget_e1", "nvm_budget") in [(i, k) for i, k, _ in issues.created]
    finally:
        await h.close()


# ── rozjazd i przejęcie ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_foreign_mode_value_pauses(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        goodwe_bank.poke(MODE, 99)
        n = len(goodwe_bank.writes)
        await h.cycle()
        assert h.ex.paused and len(goodwe_bank.writes) == n
        assert ("foreign_control_direct", {"setting": "mode"}) in [(k, p) for _, k, p in issues.created]
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_two_drifts_pause_single_drift_reconciles(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        goodwe_bank.poke(POWER, 4000)                         # właściciel zmienia moc raz
        await h.cycle()
        assert not h.ex.paused and gw_raw_word(goodwe_bank, POWER) == 2000     # uzgodnienie
        goodwe_bank.poke(POWER, 4000)                         # i drugi raz w 30 min
        n = len(goodwe_bank.writes)
        await h.cycle()
        assert h.ex.paused and len(goodwe_bank.writes) == n
        assert "power_w" in h.ex._state.taken_over and "power_w" not in (h.ex._state.restore_keys or [])
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_drift_noted_once_per_control_cycle(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        async def refused(w):                                 # uzgodnienie nie dochodzi (bez ramki)
            return DENIED
        h.io.writer.async_write = refused
        goodwe_bank.poke(POWER, 4000)
        for _ in range(5):                                    # kilka odpytań w jednym cyklu
            h.clock.advance(10.0)
            await h.conn.async_poll()
        await h.ex.async_tick()
        await h.ex.async_tick()                               # ten sam odczyt: drugi raz się nie liczy
        assert not h.ex.paused
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_plan_change_forgets_drift(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        goodwe_bank.poke(POWER, 4000)
        await h.cycle()                                       # rozjazd 1 → uzgodnienie
        new = plan(power=2500, sid="p2")
        await h.ex.async_on_plan(new, parse_schedule(new))
        await h.cycle()                                       # nowa wartość planu
        assert gw_raw_word(goodwe_bank, POWER) == 2500
        goodwe_bank.poke(POWER, 4000)
        await h.cycle()                                       # pierwszy rozjazd NOWEJ wartości
        assert not h.ex.paused
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_drift_ignored_for_reading_started_before_write_end(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        stale = h.conn.reading
        end = h.ex._last_write_end
        for started in (end - 1.0, end):                      # dwa odczyty rozpoczęte przed końcem zapisu
            h.conn.reading = SimpleNamespace(**{**stale.__dict__, "at_mono": started,
                                                "device": {**stale.device, "power_w": 8846.0}})
            await h.ex.async_tick()
        assert not h.ex.paused
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_bus_conflict_blocks_plan_writes_only(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await Harness(make_hass, GW_V, gw_target(goodwe_udp_sim)).start()
    try:
        h.conn.stats.stray += 3
        await h.conn.async_poll()
        await h.ex.async_tick()
        assert h.ex.last_decision.reason == "bus_conflict" and goodwe_bank.writes == []
        assert any(k == "direct_conflict" and p == {"reason": "stray_frames"} for _, k, p in issues.created)
    finally:
        await h.close()


# ── odczyt zwrotny ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_readback_denied_marks_failed_and_holds_mode(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    goodwe_bank.ignore_writes.add(POWER)                      # falownik przyjmuje ramkę, rejestru nie zmienia
    h = await Harness(make_hass, GW_V, gw_target(goodwe_udp_sim)).start()
    try:
        await h.ex.async_tick()
        assert gw_raw_word(goodwe_bank, POWER) == 8846 and gw_raw_word(goodwe_bank, MODE) == 11
        assert "power_w" in h.ex._memory.denied
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_lost_echo_uncertain_resolved_next_poll(make_hass, modbus_tcp_sim, goodwe_bank, sim_faults, issues):
    h = await Harness(make_hass, GW_V, gw_target(modbus_tcp_sim, kind="modbus_tcp")).start()
    try:
        sim_faults.mute_write_echo = 1                        # zapis doszedł, echo zgubione
        writer = h.io.writer
        real = writer._read_back
        calls = []

        async def lost_once(addr):
            calls.append(addr)
            return None if len(calls) == 1 else await real(addr)
        writer._read_back = lost_once                         # … i odczyt zwrotny też przepadł
        await h.ex.async_tick()
        assert h.ex._memory.uncertain                          # wynik nieznany — nie „zapisane”
        await h.cycle()
        assert gw_raw_word(goodwe_bank, POWER) == 2000 and gw_raw_word(goodwe_bank, MODE) == 10
        assert not h.ex._memory.uncertain
    finally:
        await h.close()


# ── okna czasowe (Deye) ───────────────────────────────────────────────────


def _deye(make_hass, sim, **kw):
    return Harness(make_hass, DEYE_V, deye_target(sim), utc=lambda: TOU_NOW, rated=10000.0, **kw)


def tou_raw(days=3):
    """Ten sam wzór godzinowy każdej doby: ładowanie z sieci 2–5, poza tym samokonsumpcja."""
    pattern = {hr: ({"mode": "charge", "charge_source": "grid", "power_w": 3000, "soc_target": 90,
                     "price_pln_kwh": 0.2} if 2 <= hr < 5 else SELF) for hr in range(24)}
    slots = []
    for d in range(-1, days):
        for hr in range(24):
            s = DAY0 + timedelta(days=d, hours=hr)
            slots.append({"from": iso(s), "to": iso(s + timedelta(hours=1)), **pattern[hr]})
    return {"schedule_id": "tou", "slots": slots, "fallback": {"mode": "self_consume", "soc_reserve": 10},
            "control_enabled": True}


TOU_RAW = tou_raw()


@pytest.mark.asyncio
async def test_tou_direct_end_to_end_deye(make_hass, rtu_tcp_sim, deye_bank, issues):
    h = await _deye(make_hass, rtu_tcp_sim).start(raw=TOU_RAW)
    try:
        await h.ex.async_tick()
        assert h.ex.last_decision.status == "write" and h.ex.owned
        assert h.ex._state.tou_snapshot is not None
        written = regs(deye_bank)
        assert written[0] == TOU_EN and written[-1] == TOU_EN and len(written) > 2
        assert deye_bank.read(TOU_EN, 1)[0] & 1
        n = len(deye_bank.writes)
        await h.cycle()
        assert len(deye_bank.writes) == n and h.ex.last_decision.reason == "nothing_to_write"
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_tou_snapshot_survives_restart(make_hass, rtu_tcp_sim, deye_bank, issues):
    owner_word = deye_bank.read(TOU_EN, 1)[0]
    h = await _deye(make_hass, rtu_tcp_sim).start(raw=TOU_RAW)
    try:
        await h.ex.async_tick()
        snap = h.ex._state.tou_snapshot
        assert snap["tou_word"] == owner_word
        await h.restart()
        assert h.ex._state.tou_snapshot == snap and h.ex.owned
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_tou_restore_from_snapshot(make_hass, rtu_tcp_sim, deye_bank, issues):
    before = deye_bank.read(146, 32)
    h = await _deye(make_hass, rtu_tcp_sim).start(raw=TOU_RAW)
    try:
        await h.ex.async_tick()
        assert deye_bank.read(146, 32) != before
        await h.ex.async_set_consent(False)
        await h.cycle()
        assert deye_bank.read(146, 32) == before                  # programy i słowo włącznika właściciela
        assert not h.ex.owned and h.ex._state.tou_snapshot is None
        assert h.ex.last_decision.status == "restore"
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_tou_failed_rewrite_restores_owner_programs(make_hass, rtu_tcp_sim, deye_bank, issues):
    before = deye_bank.read(146, 32)
    h = await _deye(make_hass, rtu_tcp_sim).start(raw=TOU_RAW)
    try:
        # pole SoC programu 2 odrzucone przez urządzenie po wyłączeniu harmonogramu → powrót od razu
        deye_bank.ignore_writes.add(167)
        await h.ex.async_tick()
        rep = h.ex.last_tou_report
        assert rep is not None and rep.restore_needed
        assert deye_bank.read(146, 32) == before
        assert h.ex._memory.last_written.get("tou_enabled") == (1.0 if before[0] & 1 else 0.0)
        for _ in range(2):
            await h.cycle(400.0)
        assert not h.ex.paused                                    # powrót to nasz zapis, nie przejęcie
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_tou_commit_uses_time_after_the_write_sequence(make_hass, rtu_tcp_sim, deye_bank, issues):
    h = await _deye(make_hass, rtu_tcp_sim).start(raw=TOU_RAW)
    try:
        real = h.io.writer.async_write
        sent_at = []

        async def slow(w):
            h.clock.advance(1.0)                              # sekwencja zapisów trwa
            out = await real(w)
            sent_at.append(h.clock.now)
            return out
        h.io.writer.async_write = slow
        await h.ex.async_tick()
        assert sent_at and h.ex._memory.tou_write_end >= sent_at[-1]
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_owner_program_edit_twice_pauses_and_is_not_restored(make_hass, rtu_tcp_sim, deye_bank, issues):
    h = await _deye(make_hass, rtu_tcp_sim).start(raw=TOU_RAW)
    try:
        await h.ex.async_tick()
        soc_reg = 166                                         # SoC programu 1
        ours = deye_bank.read(soc_reg, 1)[0]
        deye_bank.poke(soc_reg, 55)
        await h.cycle(400.0)
        assert not h.ex.paused
        await h.cycle(400.0)                                  # ta sama wartość trwa — liczy się raz
        assert not h.ex.paused
        deye_bank.poke(soc_reg, 60)                           # druga ZMIANA właściciela w 30 min
        await h.cycle(400.0)
        assert h.ex.paused and "tou.1.soc" in h.ex._state.taken_over
        await h.ex.async_set_consent(False)
        await h.cycle()
        assert deye_bank.read(soc_reg, 1)[0] == 60 and ours != 60
    finally:
        await h.close()


# ── runtime: złożenie, przeładowanie, wyłączenie, usunięcie ────────────────


@pytest.mark.asyncio
async def test_disable_entry_restores_before_close(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    from custom_components.volcast.control import runtime as rt_mod
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    order = []
    real_stop = h.conn.async_stop

    async def stop(**kw):
        order.append(("close", len(goodwe_bank.writes)))
        await real_stop(**kw)
    h.conn.async_stop = stop
    telemetry = SimpleNamespace(async_stop=_noop)
    rt = rt_mod.ControlRuntime(h.ex, None, telemetry, None, None, {}, 8000.0, direct=h.conn)
    n = len(goodwe_bank.writes)
    await rt_mod.async_unload_control(h.hass, rt, restore=True)
    assert gw_raw_word(goodwe_bank, MODE) == 1 and order == [("close", len(goodwe_bank.writes))]
    assert len(goodwe_bank.writes) > n and h.hass.data[DOMAIN]["direct_hosts"] == {}


@pytest.mark.asyncio
async def test_reload_does_not_restore_direct(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    from custom_components.volcast.control import runtime as rt_mod
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    n = len(goodwe_bank.writes)
    rt = rt_mod.ControlRuntime(h.ex, None, SimpleNamespace(async_stop=_noop), None, None, {}, 8000.0,
                               direct=h.conn)
    await rt_mod.async_unload_control(h.hass, rt)
    assert len(goodwe_bank.writes) == n and gw_raw_word(goodwe_bank, MODE) == 10
    assert h.hass.data[DOMAIN]["direct_hosts"] == {} and h.ex.owned


@pytest.mark.asyncio
async def test_switch_direct_to_entities_restores_through_direct_first(make_hass, goodwe_udp_sim, goodwe_bank,
                                                                       issues):
    from custom_components.volcast.control import runtime as rt_mod
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        rt = rt_mod.ControlRuntime(h.ex, None, SimpleNamespace(async_stop=_noop), None, None, {}, 8000.0,
                                   direct=h.conn)
        old = dict(h.options)
        new = {"control_mode": "entities", "profile_id": "goodwe-et", "inverter_domain": "goodwe"}
        assert rt_mod.control_options_changed(old, new)
        assert rt_mod.control_options_changed(old, {**old, "direct_target": {**h.target, "unit_id": 1}})
        assert await rt_mod.async_restore_if_control_changed(rt, old, new) is True
        assert gw_raw_word(goodwe_bank, MODE) == 1 and not h.ex.owned
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_remove_owned_direct_without_link_warns(make_hass, goodwe_udp_sim, goodwe_bank, monkeypatch, caplog,
                                                      issues):
    from custom_components.volcast.control import runtime as rt_mod
    store = ControlStore(make_hass(), "e1")
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank, store=store)
    await h.close()
    await goodwe_udp_sim.close()                            # falownik poza zasięgiem
    n = len(goodwe_bank.writes)
    monkeypatch.setattr(rt_mod, "ControlStore", lambda hass, eid: store)
    monkeypatch.setattr(rt_mod, "async_installation_salt", _salt)
    monkeypatch.setattr(rt_mod, "DirectConnection", _loopback_connection)
    entry = SimpleNamespace(domain=DOMAIN, entry_id="e1", options=h.options, disabled_by=None,
                            data={"api_key": "vk_x", "backend": _BACKEND})
    hass = make_hass(entries=[entry])
    hass.async_add_executor_job = _executor_job
    with caplog.at_level(logging.WARNING):
        await rt_mod.async_remove_control(hass, entry)
    assert "could not return the inverter" in caplog.text and len(goodwe_bank.writes) == n
    assert hass.data.get(DOMAIN, {}).get("direct_hosts", {}) == {}


@pytest.mark.asyncio
async def test_remove_owned_direct_restores_through_link(make_hass, goodwe_udp_sim, goodwe_bank, monkeypatch,
                                                         issues):
    from custom_components.volcast.control import runtime as rt_mod
    store = ControlStore(make_hass(), "e1")
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank, store=store)
    await h.close()
    monkeypatch.setattr(rt_mod, "ControlStore", lambda hass, eid: store)
    monkeypatch.setattr(rt_mod, "async_installation_salt", _salt)
    monkeypatch.setattr(rt_mod, "DirectConnection", _loopback_connection)
    entry = SimpleNamespace(domain=DOMAIN, entry_id="e1", options=h.options, disabled_by=None,
                            data={"api_key": "vk_x", "backend": _BACKEND})
    hass = make_hass(entries=[entry])
    hass.async_add_executor_job = _executor_job
    await rt_mod.async_remove_control(hass, entry)
    assert gw_raw_word(goodwe_bank, MODE) == 1 and hass.data[DOMAIN]["direct_hosts"] == {}
    assert "direct_identity_changed_e1" in issues.deleted


def test_compose_direct_trial_uses_no_write_writer(make_hass):
    from custom_components.volcast.control import runtime as rt_mod
    target = {"profile_id": "goodwe-et", "transport": "goodwe_udp", "host": "192.168.1.9", "port": 8899,
              "unit_id": 247, "device_fp": "0123456789abcdef", "unreadable": ["soc_max"],
              "capabilities": {"export_limit_enabled": False}}
    entry = SimpleNamespace(domain=DOMAIN, entry_id="e1", options={"direct_trial": True, "direct_target": target},
                            data={}, disabled_by=None)
    composed = rt_mod.compose_direct(make_hass(entries=[entry]), entry, [GW_V], salt=SALT)
    assert composed is not None
    choice, io, conn = composed
    assert choice.profile is GW_V and choice.integration_domain is None
    assert io.trial and isinstance(io.writer, NoWriteWriter) and conn.unreadable == {"soc_max"}
    assert io.unsupported_seed() == {"soc_max", "export_limit_enabled"}
    assert conn.unsupported == {"export_limit_enabled"}          # rejestr bez rejestru (sonda) — bez odpytywania
    # tylko łącze bez korelacji odpowiedzi; Modbus TCP odpytuje po staremu
    entry.options = {"direct_trial": True, "direct_target": {**target, "transport": "modbus_tcp", "port": 502}}
    tcp = rt_mod.compose_direct(make_hass(entries=[entry]), entry, [GW_V], salt=SALT)
    assert tcp is not None and tcp[2].unsupported == frozenset()
    for options in ({"control_mode": "entities", "direct_target": target}, {"control_mode": "direct"},
                    {"control_mode": "direct", "direct_target": {**target, "profile_id": "nope"}}):
        entry.options = options
        assert rt_mod.compose_direct(make_hass(entries=[entry]), entry, [GW_V], salt=SALT) is None


_BACKEND = {"base_url": "https://s.example.test", **{k: f"https://s.example.test/functions/v1/{k}" for k in (
    "forecast", "submit_production", "telemetry", "schedule", "history_import", "pairing")}}


async def _noop(*a, **k):
    return None


async def _salt(hass):
    return SALT


async def _executor_job(func, *args):
    return func(*args)


def _loopback_connection(*args, **kw):
    kw.update(allow_loopback=True, transport_factory=_factory())
    return DirectConnection(*args, **kw)


@pytest.mark.asyncio
async def test_budget_restore_needs_ownership(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await Harness(make_hass, GW_V, gw_target(goodwe_udp_sim)).start()
    try:
        assert gw_raw_word(goodwe_bank, MODE) == 11            # tryb wymuszony ustawiony przez właściciela
        budget = h.ex._memory.budget
        for key in ("mode", "power_w"):
            for _ in range(budget.per_key):
                budget.note(key, h.utc().timestamp())
        await h.ex.async_tick()
        assert h.ex.last_decision.reason == "restore_not_owned" and goodwe_bank.writes == []
    finally:
        await h.close()
