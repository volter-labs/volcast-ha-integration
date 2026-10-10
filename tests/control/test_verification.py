"""Drabina weryfikacji w HA: wykonawca (bramka planu w trybie bezpośrednim, zapis kontrolny, okno próbne,
powrót) na symulatorze GoodWe oraz runner drabiny (`VerificationRunner`) na atrapach wykonawcy."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from custom_components.volcast.const import ISSUE_VERIFICATION_STOPPED
from custom_components.volcast.control import verification as ver_mod
from custom_components.volcast.control.device_io import DirectIO, EntityIO, Reading
from custom_components.volcast.control.verification import VerificationRunner, window_schedule
from custom_components.volcast.core.control.cycle import BLOCKED, DRY_RUN, WRITE, CycleDecision
from custom_components.volcast.core.control.ladder import (
    IDLE, RUNNING, STOPPED, VERIFIED, WAITING, Ladder, LadderParams, device_key)
from tests.control.test_executor_direct import (  # noqa: F401 — `issues` to fixture
    DEYE_V, GW_DRAFT, GW_NOW, GW_V, MODE, POWER, SALT, TOU_RAW, Harness, _deye, gw_raw_word, gw_target,
    issues, regs)

BASELINE_AUTO = 1                                   # GoodWe ET: tryb bazowy `auto` (symulator startuje w 11)
from tests.core.control.test_ladder import shape_errors

P = LadderParams(trial_hours=24, window_minutes=15, window_power_w=500)


# ── wykonawca: bramka, zapis kontrolny, okno, powrót (symulator) ─────────


class Gate:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed

    def plan_allowed(self) -> bool:
        return self.allowed


@pytest.mark.asyncio
async def test_direct_plan_writes_wait_for_a_verified_device(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await Harness(make_hass, GW_V, gw_target(goodwe_udp_sim)).start()
    try:
        h.ex.verification = Gate(False)
        await h.ex.async_tick()
        d = h.ex.last_decision
        assert goodwe_bank.writes == [] and d.status == DRY_RUN and d.writes   # „co bym zapisał”
        assert not h.ex.owned
        h.ex.verification.allowed = True
        await h.cycle()
        assert h.ex.last_decision.status == WRITE and gw_raw_word(goodwe_bank, MODE) == 10
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_without_a_ladder_gate_direct_control_is_unchanged(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    # Regresja: wykonawca bez runnera (testy, powrót przez stary sposób) steruje jak dotąd.
    h = await Harness(make_hass, GW_V, gw_target(goodwe_udp_sim)).start()
    try:
        assert h.ex.verification is None
        await h.ex.async_tick()
        assert h.ex.last_decision.status == WRITE
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_closed_gate_returns_an_owned_inverter_to_its_baseline(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await Harness(make_hass, GW_V, gw_target(goodwe_udp_sim)).start()
    try:
        await h.ex.async_tick()
        assert h.ex.owned and gw_raw_word(goodwe_bank, MODE) == 10
        h.ex.verification = Gate(False)
        await h.cycle()
        assert not h.ex.owned and gw_raw_word(goodwe_bank, MODE) == BASELINE_AUTO
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_control_write_rewrites_the_current_mode_with_read_back(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await Harness(make_hass, GW_V, gw_target(goodwe_udp_sim)).start()
    try:
        h.ex.verification = Gate(False)
        before = gw_raw_word(goodwe_bank, MODE)
        assert await h.ex.async_control_write() is True
        assert regs(goodwe_bank) == [MODE] and gw_raw_word(goodwe_bank, MODE) == before
        assert not h.ex.owned
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_control_write_without_identity_reports_no_read(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await Harness(make_hass, GW_V, {**gw_target(goodwe_udp_sim), "device_fp": None}).start()
    try:
        assert await h.ex.async_control_write() is None
        assert goodwe_bank.writes == []
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_window_charges_through_the_plan_path_and_restore_returns_the_baseline(
        make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await Harness(make_hass, GW_V, gw_target(goodwe_udp_sim)).start()
    try:
        h.ex.verification = Gate(False)
        now = h.utc()
        h.ex.start_verification_window(window_schedule(now, minutes=15, power_w=500, base=h.ex.schedule))
        await h.ex.async_tick()
        d = h.ex.last_decision
        assert d.status == WRITE and d.intent == "charge_grid" and d.guard is not None and d.guard.write_allowed
        assert h.ex.owned and gw_raw_word(goodwe_bank, MODE) == 11 and gw_raw_word(goodwe_bank, POWER) == 500
        await h.ex.async_verification_restore()
        assert not h.ex.owned and gw_raw_word(goodwe_bank, MODE) == BASELINE_AUTO
        n = len(goodwe_bank.writes)
        await h.cycle()                                   # bramka zamknięta: bez okna nic nie idzie
        assert len(goodwe_bank.writes) == n and h.ex.last_decision.status == DRY_RUN
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_window_needs_the_owner_consent(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await Harness(make_hass, GW_V, gw_target(goodwe_udp_sim)).start(consent=False)
    try:
        assert h.ex.verification_can_write() is False
        h.ex.start_verification_window(window_schedule(h.utc(), minutes=15, power_w=500, base=None))
        await h.ex.async_tick()
        assert goodwe_bank.writes == []
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_trial_connection_cannot_run_the_writing_rungs(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    target = gw_target(goodwe_udp_sim)
    h = await Harness(make_hass, GW_V, target, options={"direct_trial": True, "direct_target": target},
                      trial=True).start()
    try:
        assert h.ex.verification_can_write() is False
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_verification_record_saved_with_the_control_state(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await Harness(make_hass, GW_V, gw_target(goodwe_udp_sim)).start()
    try:
        lad = Ladder(4, P, device_key="ab" * 16)
        lad.tick(GW_NOW)
        assert await h.ex.async_save_verification(lad.to_record())
        loaded = await h.store.async_load()
        assert loaded.verification == lad.to_record() and h.ex.verification_record == lad.to_record()
    finally:
        await h.close()


def test_window_schedule_is_one_grid_charge_slot_with_the_plan_reserve():
    from custom_components.volcast.core.slot import Action, Fallback, Schedule

    base = Schedule("p", None, (), Fallback(soc_reserve=35.0), True)
    s = window_schedule(GW_NOW, minutes=15, power_w=500, base=base)
    slot, fallback = s.effective_slot(GW_NOW + timedelta(minutes=5))
    assert not fallback and slot.action is Action.CHARGE and slot.charge_source == "grid"
    assert slot.power_w == 500 and slot.end == GW_NOW + timedelta(minutes=15)
    assert s.fallback.soc_reserve == 35.0
    assert s.effective_slot(GW_NOW + timedelta(minutes=16))[1] is True


# ── runner na atrapach ────────────────────────────────────────────────────


class FakeIO:
    def __init__(self, kind="entities", identity="entities|goodwe-et|goodwe|uid"):
        self.kind = kind
        self._identity = identity
        self.readings = {"mode": "general", "soc": 50.0, "battery_power_w": 0.0}
        self.raw_mode = "general"
        self.soc_age = 1.0
        self.writer = SimpleNamespace(is_ours=lambda cid: cid == "ours")
        self.foreign_cb = None
        self.identity_ok = True

    def identity(self):
        return self._identity

    def read(self, now):
        return Reading(dict(self.readings), self.raw_mode, {}, {}, self.soc_age)

    def identity_confirmed(self):
        return self.identity_ok

    def subscribe_foreign(self, cb):
        self.foreign_cb = cb
        return lambda: None


class FakeExecutor:
    def __init__(self, io=None, *, can_write=True, record=None, profile=GW_V):
        self.io = io or FakeIO()
        self.profile = profile
        self.can_write = can_write
        self.verification_record = record or {}
        self.verification = None
        self.last_decision = None
        self.last_write_end = None
        self.schedule = None
        self.window = None
        self.restores = 0
        self.control_writes = []
        self.control_result = True
        self.saved = []
        self.migration_ok = False
        self.forced = []

    def verification_migration_ok(self):
        return self.migration_ok

    def verification_can_write(self):
        return self.can_write

    async def async_save_verification(self, record):
        self.verification_record = dict(record)
        self.saved.append(dict(record))
        return True

    async def async_control_write(self):
        self.control_writes.append(1)
        return self.control_result

    def start_verification_window(self, schedule):
        self.window = schedule

    async def async_verification_restore(self, *, force=False):
        self.window = None
        self.restores += 1
        self.forced.append(force)


class Clock:
    def __init__(self):
        self.now = GW_NOW

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


class Env:
    def __init__(self, monkeypatch, executor=None, *, start=1):
        self.clock = Clock()
        self.ex = executor or FakeExecutor()
        self.points = []
        self.created, self.deleted, self.urgent, self.changes = [], [], [], []
        monkeypatch.setattr(ver_mod.ir, "async_create_issue",
                            lambda hass, domain, issue_id, **kw: self.created.append((issue_id, kw)))
        monkeypatch.setattr(ver_mod.ir, "async_delete_issue",
                            lambda hass, domain, issue_id: self.deleted.append(issue_id))
        monkeypatch.setattr(ver_mod, "async_dispatcher_send", lambda hass, sig: self.changes.append(sig))
        entry = SimpleNamespace(entry_id="e1", options={})

        async def urgent():
            self.urgent.append(1)

        def track(hass, action, when):
            self.points.append(when)
            return lambda: None

        self.runner = VerificationRunner(SimpleNamespace(data={}), entry, self.ex, params=P, start_rung=start,
                                         salt=SALT, on_urgent=urgent, utcnow=self.clock, track_point=track)

    @property
    def state(self):
        s = self.runner.ladder.state
        return s.rung, s.state

    async def step(self, **advance):
        if advance:
            self.clock.advance(**advance)
        await self.runner.async_step()


def foreign_event(eid="select.mode", ctx_id="x", user="u1", old="general", new="eco"):
    ctx = SimpleNamespace(id=ctx_id, user_id=user, parent_id=None)
    return SimpleNamespace(data={"entity_id": eid, "old_state": SimpleNamespace(state=old),
                                 "new_state": SimpleNamespace(state=new, context=ctx)}, context=ctx)


@pytest.mark.asyncio
async def test_runner_full_draft_run_to_verified(monkeypatch):
    env = Env(monkeypatch)
    await env.runner.async_start()
    assert env.ex.verification is env.runner and env.runner.plan_allowed() is False
    assert env.state == (3, RUNNING)          # encje: identyfikacja i odczyt przechodzą od razu
    assert env.points[-1] == GW_NOW + timedelta(hours=24)
    env.ex.last_decision = CycleDecision(DRY_RUN, "unverified_profile", writes=("w",), flat={"mode": "eco"})
    await env.step(minutes=1)
    await env.step(minutes=1)                     # ta sama decyzja — jeden „zapis”
    assert env.runner.ladder.state.would_write == 1
    await env.step(hours=24)
    assert env.ex.control_writes == [1] and env.state == (5, RUNNING)   # zapis kontrolny i okno od razu
    assert env.ex.window is not None and env.points[-1] == env.clock.now + timedelta(minutes=15)
    for soc in (50.0, 50.5, 51.0):
        env.ex.io.readings.update(battery_power_w=-500.0, soc=soc)
        await env.step(minutes=5)
    assert env.state == (5, VERIFIED) and env.runner.plan_allowed() is True
    assert env.ex.restores == 1 and env.ex.window is None              # po oknie powrót — raz
    assert env.ex.verification_record == env.runner.ladder.to_record()
    assert env.created == [] and env.urgent == []
    assert shape_errors(env.runner.payload()) == []


@pytest.mark.asyncio
async def test_runner_stop_restores_once_raises_an_issue_and_requests_telemetry(monkeypatch):
    env = Env(monkeypatch)
    await env.runner.async_start()
    await env.runner.async_on_state_event(foreign_event())
    assert env.state == (3, STOPPED) and env.runner.ladder.state.stop_reason == "foreign_write"
    for _ in range(3):
        await env.step(minutes=1)
    await env.runner.async_on_state_event(foreign_event())
    assert env.ex.restores == 1 and env.urgent == [1]
    (issue_id, kw), = env.created
    assert issue_id == "verification_stopped_e1" and kw["translation_key"] == ISSUE_VERIFICATION_STOPPED
    assert kw["translation_placeholders"] == {"reason": "foreign_write"}
    assert shape_errors(env.runner.payload()) == []


@pytest.mark.asyncio
async def test_runner_ignores_own_and_actorless_entity_changes(monkeypatch):
    env = Env(monkeypatch)
    await env.runner.async_start()
    await env.runner.async_on_state_event(foreign_event(ctx_id="ours"))
    await env.runner.async_on_state_event(foreign_event(user=None))
    await env.runner.async_on_state_event(foreign_event(old="eco", new="eco"))
    assert env.state == (3, RUNNING)


@pytest.mark.asyncio
async def test_runner_waits_without_consent_and_moves_on_with_it(monkeypatch):
    env = Env(monkeypatch, FakeExecutor(can_write=False))
    await env.runner.async_start()
    await env.step(hours=24)
    assert env.state == (4, WAITING) and env.ex.control_writes == []
    env.ex.can_write = True
    await env.step(minutes=1)
    assert env.ex.control_writes == [1] and env.state == (5, RUNNING)


@pytest.mark.asyncio
async def test_runner_waits_for_room_in_the_battery_before_the_window(monkeypatch):
    env = Env(monkeypatch)
    env.ex.io.readings["soc"] = 97.0
    await env.runner.async_start()
    await env.step(hours=24)
    assert env.state == (5, WAITING) and env.ex.window is None
    env.ex.io.readings["soc"] = 80.0
    await env.step(minutes=1)
    assert env.state == (5, RUNNING) and env.ex.window is not None


@pytest.mark.asyncio
async def test_runner_read_back_mismatch_stops_and_restores_once(monkeypatch):
    env = Env(monkeypatch)
    env.ex.control_result = False
    await env.runner.async_start()
    await env.step(hours=24)
    await env.step(minutes=1)
    assert env.state == (4, STOPPED) and env.ex.restores == 1 and len(env.created) == 1


@pytest.mark.asyncio
async def test_runner_window_deviation_stops_and_restores_once(monkeypatch):
    env = Env(monkeypatch)
    await env.runner.async_start()
    await env.step(hours=24)
    for soc in (50.0, 51.0, 52.0):
        env.ex.io.readings.update(battery_power_w=-200.0, soc=soc)
        await env.step(minutes=5)
    assert env.state == (5, STOPPED) and env.runner.ladder.state.stop_reason == "window_deviation"
    assert env.ex.restores == 1 and env.urgent == [1]


@pytest.mark.asyncio
async def test_runner_retry_returns_to_the_stopped_rung_and_clears_the_issue(monkeypatch):
    env = Env(monkeypatch)
    await env.runner.async_start()
    await env.runner.async_abort()
    assert env.state == (3, STOPPED) and env.ex.restores == 1
    await env.runner.async_retry()
    assert env.state == (3, RUNNING) and "verification_stopped_e1" in env.deleted
    await env.runner.async_conflict()
    assert env.runner.ladder.state.stop_reason == "controller_conflict" and env.ex.restores == 2


@pytest.mark.asyncio
async def test_runner_resumes_the_saved_ladder_and_restarts_for_another_device(monkeypatch):
    key = device_key(SALT, "entities|goodwe-et|goodwe|uid")
    lad = Ladder(1, P, device_key=key)
    lad.tick(GW_NOW - timedelta(hours=5))
    lad.identify_ok(GW_NOW - timedelta(hours=5))
    lad.read_ok(GW_NOW - timedelta(hours=5))
    lad.would_write(4)
    env = Env(monkeypatch, FakeExecutor(record=lad.to_record()))
    await env.runner.async_start()
    assert env.state == (3, RUNNING) and env.runner.ladder.state.would_write == 4
    assert env.points[-1] == GW_NOW + timedelta(hours=19)
    env.ex.io._identity = "entities|goodwe-et|goodwe|other"
    await env.step(minutes=1)
    assert env.state == (1, STOPPED) and env.runner.ladder.state.stop_reason == "identify_changed"
    assert env.runner.ladder.device_key == device_key(SALT, "entities|goodwe-et|goodwe|other")


@pytest.mark.asyncio
async def test_runner_verified_device_allows_the_plan_only_for_that_device(monkeypatch):
    key = device_key(SALT, "direct|goodwe-et|fp")
    lad = Ladder(4, P, device_key=key)
    lad.state.state, lad.state.since = VERIFIED, GW_NOW
    io = FakeIO(kind="direct", identity="direct|goodwe-et|fp")
    env = Env(monkeypatch, FakeExecutor(io, record=lad.to_record()), start=4)
    await env.runner.async_start()
    assert env.runner.plan_allowed() is True
    io._identity = "direct|goodwe-et|fp2"
    await env.step(minutes=1)
    assert env.runner.ladder.state.state != VERIFIED and env.runner.plan_allowed() is False


@pytest.mark.asyncio
async def test_runner_direct_draft_identifies_and_reads_before_the_trial(monkeypatch):
    io = FakeIO(kind="direct", identity="direct|goodwe-et|fp")
    io.identity_ok = False
    io.conn = SimpleNamespace(conflict=False)
    env = Env(monkeypatch, FakeExecutor(io), start=1)
    await env.runner.async_start()
    assert env.state == (1, RUNNING)
    io.identity_ok = True
    io.readings = {"soc": 50.0}                       # tryb nieczytelny — odczyt jeszcze nie
    await env.step(minutes=1)
    assert env.state == (2, RUNNING)
    io.readings = {"mode": "general", "soc": 50.0}
    await env.step(minutes=1)
    assert env.state == (3, RUNNING)
    io.conn.conflict = True                           # drugi klient na łączu w próbie
    await env.step(minutes=1)
    assert env.state == (3, STOPPED) and env.runner.ladder.state.stop_reason == "foreign_write"


@pytest.mark.asyncio
async def test_runner_direct_register_change_in_the_trial_is_a_foreign_write(monkeypatch):
    io = FakeIO(kind="direct", identity="direct|goodwe-et|fp")
    io.conn = SimpleNamespace(conflict=False)
    env = Env(monkeypatch, FakeExecutor(io))
    await env.runner.async_start()
    await env.step(minutes=1)
    env.ex.last_write_end = 5.0                       # nasz zapis (np. hamulec) — bez stopu
    io.readings["mode"] = "eco"
    await env.step(minutes=1)
    assert env.state == (3, RUNNING)
    io.readings["mode"] = "general"
    await env.step(minutes=1)
    assert env.state == (3, STOPPED)


@pytest.mark.asyncio
async def test_runner_without_device_identity_reports_nothing(monkeypatch):
    env = Env(monkeypatch, FakeExecutor(FakeIO(identity=None)))
    await env.runner.async_start()
    assert env.runner.ladder is None and env.runner.payload() is None and env.runner.plan_allowed() is False


def test_start_rung_follows_the_profile_status():
    assert ver_mod.start_rung_for(GW_V, None, direct=True) == 4
    assert ver_mod.start_rung_for(GW_DRAFT, None, direct=True) == 1


def test_default_params_skip_the_window_for_time_window_profiles():
    assert ver_mod.default_params(GW_V).window is True
    assert ver_mod.default_params(DEYE_V).window is False


@pytest.mark.asyncio
async def test_device_io_identity_needs_a_device(make_hass):
    io = EntityIO(make_hass(), GW_V, "goodwe", {"mode": "select.m"}, None, mode_unique_id="uid-1")
    assert io.identity() == "entities|goodwe-et|goodwe|uid-1"
    assert EntityIO(make_hass(), GW_V, None, {}, None).identity() is None
    conn = SimpleNamespace(target={"device_fp": "fp1"}, client=object(), refused=lambda: None, identity="confirmed")
    io = DirectIO(conn, GW_V, trial=False, salt=SALT)
    assert io.identity() == "direct|goodwe-et|fp1" and io.identity_confirmed()
    conn.identity = "mismatch"
    assert not io.identity_confirmed()
    assert DirectIO(SimpleNamespace(target={}), GW_V, trial=False, salt=SALT).identity() is None


def test_blocked_decision_is_not_a_would_write():
    assert not ver_mod.would_write_flat(CycleDecision(BLOCKED, "paused", writes=("w",), flat={"mode": "x"}))
    assert ver_mod.would_write_flat(CycleDecision(DRY_RUN, "x", writes=("w",), flat={"mode": "x"})) == {"mode": "x"}


# ── start od zapisu kontrolnego, okna czasowe, migracja, sygnał ──────────


@pytest.mark.asyncio
async def test_runner_verified_profile_goes_straight_to_the_control_write(monkeypatch):
    env = Env(monkeypatch, start=4)
    await env.runner.async_start()
    assert env.ex.control_writes == [1] and env.state == (5, RUNNING)
    assert "trial" not in env.runner.payload()


@pytest.mark.asyncio
async def test_runner_verified_profile_without_consent_waits_at_the_control_write(monkeypatch):
    env = Env(monkeypatch, FakeExecutor(can_write=False), start=4)
    await env.runner.async_start()
    assert env.state == (4, WAITING) and env.ex.control_writes == []
    assert shape_errors(env.runner.payload()) == []


@pytest.mark.asyncio
async def test_runner_time_window_profile_is_verified_by_the_control_write(monkeypatch):
    io = FakeIO(kind="direct", identity="direct|deye-sg|fp")
    io.conn = SimpleNamespace(conflict=False)
    ex = FakeExecutor(io, profile=DEYE_V)
    env = Env(monkeypatch, ex, start=4)
    env.runner = VerificationRunner(SimpleNamespace(data={}), SimpleNamespace(entry_id="e1", options={}), ex,
                                    params=ver_mod.default_params(DEYE_V), start_rung=4, salt=SALT,
                                    utcnow=env.clock, track_point=lambda hass, action, when: (lambda: None))
    await env.runner.async_start()
    assert env.state == (4, VERIFIED) and ex.control_writes == [1]
    assert ex.window is None and ex.restores == 0 and env.runner.plan_allowed() is True


@pytest.mark.asyncio
async def test_runner_time_window_in_entity_mode_cannot_write(monkeypatch):
    ex = FakeExecutor(FakeIO(identity="entities|deye-sg|x|y"), profile=DEYE_V)
    env = Env(monkeypatch, ex, start=4)
    await env.runner.async_start()
    assert env.state == (4, WAITING) and ex.control_writes == []


@pytest.mark.asyncio
async def test_runner_migrates_a_device_controlled_before_the_update(monkeypatch):
    io = FakeIO(kind="direct", identity="direct|goodwe-et|fp")
    io.conn = SimpleNamespace(conflict=False)
    ex = FakeExecutor(io)
    ex.migration_ok = True
    env = Env(monkeypatch, ex, start=4)
    await env.runner.async_start()
    assert env.state == (4, VERIFIED) and env.runner.plan_allowed() is True
    assert ex.verification_record["migrated"] is True and env.runner.payload()["migrated"] is True
    assert ex.control_writes == [] and ex.restores == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["no_previous_control", "draft_profile", "record_exists"])
async def test_runner_does_not_migrate_otherwise(monkeypatch, case):
    io = FakeIO(kind="direct", identity="direct|goodwe-et|fp")
    io.conn = SimpleNamespace(conflict=False)
    record = None
    if case == "record_exists":
        lad = Ladder(4, P, device_key=device_key(SALT, "direct|goodwe-et|fp"))
        lad.tick(GW_NOW)
        record = lad.to_record()
    ex = FakeExecutor(io, can_write=False, record=record)
    ex.migration_ok = case != "no_previous_control"
    env = Env(monkeypatch, ex, start=1 if case == "draft_profile" else 4)
    await env.runner.async_start()
    assert env.runner.ladder.state.state != VERIFIED and env.runner.plan_allowed() is False
    assert not env.runner.ladder.migrated


@pytest.mark.asyncio
async def test_executor_migration_check_needs_previous_control_of_this_device(
        make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await Harness(make_hass, GW_V, gw_target(goodwe_udp_sim)).start()
    try:
        assert h.ex.verification_migration_ok() is False          # nigdy nie sterowaliśmy
        await h.ex.async_tick()
        assert h.ex.owned and h.ex.verification_migration_ok() is True
        await h.restart()                                         # ten sam falownik — własność zostaje
        assert h.ex.owned and h.ex.verification_migration_ok() is True
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_executor_migration_check_rejects_another_device(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await Harness(make_hass, GW_V, gw_target(goodwe_udp_sim)).start()
    try:
        await h.ex.async_tick()
        assert h.ex.owned
        state = await h.store.async_load()
        state.owner = {**state.owner, "device": "another-device"}
        await h.store.async_save(state)
        await h.restart()
        assert h.ex.verification_migration_ok() is False
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_time_window_control_write_rewrites_the_first_program_soc(make_hass, rtu_tcp_sim, deye_bank, issues):
    h = await _deye(make_hass, rtu_tcp_sim).start(raw=TOU_RAW)
    try:
        h.ex.verification = Gate(False)
        soc1 = DEYE_V.raw["write"]["tou_program"]["soc"]["addr"]
        before = deye_bank.read(soc1, 1)[0]
        assert await h.ex.async_control_write() is True
        assert regs(deye_bank) == [soc1] and deye_bank.read(soc1, 1)[0] == before
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_time_window_plan_waits_for_a_verified_device(make_hass, rtu_tcp_sim, deye_bank, issues):
    h = await _deye(make_hass, rtu_tcp_sim).start(raw=TOU_RAW)
    try:
        h.ex.verification = Gate(False)
        await h.ex.async_tick()
        assert deye_bank.writes == [] and h.ex.last_decision.status != WRITE
        h.ex.verification.allowed = True
        await h.cycle()
        assert h.ex.last_decision.status == WRITE and h.ex.owned
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_runner_signals_trial_progress_at_most_hourly(monkeypatch):
    env = Env(monkeypatch)
    await env.runner.async_start()
    assert env.state == (3, RUNNING)
    start_signals, start_saves = len(env.changes), len(env.ex.saved)
    for _ in range(30):                                          # 3 h, krok co 6 min
        await env.step(minutes=6)
    assert len(env.ex.saved) - start_saves >= 25                  # postęp próby trwały w magazynie
    assert len(env.changes) - start_signals <= 3                  # sygnał (telemetria) najwyżej co godzinę
    n = len(env.changes)
    await env.runner.async_abort()                                # zmiana stanu — sygnał od razu
    assert len(env.changes) == n + 1
    await env.runner.async_retry()
    assert len(env.changes) == n + 2


# ── poprawki po przeglądzie: zapis kontrolny bez okna, bramka urządzenia, świeży odczyt, restart w oknie ──

FOXESS = __import__("custom_components.volcast.core.profile", fromlist=["load_builtin"]).load_builtin("foxess-h")
DEYE_DRAFT = __import__("custom_components.volcast.core.profile", fromlist=["load_builtin"]).load_builtin("deye-sg")


def test_window_needs_a_guarded_forced_grid_charge_with_power():
    assert ver_mod.window_capable(GW_V) is True
    assert ver_mod.window_capable(FOXESS) is False                # ładowanie z sieci bez mocy, bez możliwości
    assert ver_mod.default_params(FOXESS).window is False
    assert ver_mod.writing_supported(FOXESS, "entities") and ver_mod.writing_supported(FOXESS, "direct")
    assert ver_mod.writing_supported(DEYE_DRAFT, "direct") and not ver_mod.writing_supported(DEYE_DRAFT, "entities")


@pytest.mark.asyncio
async def test_runner_draft_profile_without_a_test_window_is_verified_by_the_control_write(monkeypatch):
    ex = FakeExecutor(FakeIO(identity="entities|foxess-h|foxess_modbus|uid"), profile=FOXESS)
    env = Env(monkeypatch, ex)
    env.runner = VerificationRunner(SimpleNamespace(data={}), SimpleNamespace(entry_id="e1", options={}), ex,
                                    params=ver_mod.default_params(FOXESS), start_rung=1, salt=SALT,
                                    utcnow=env.clock, track_point=lambda hass, action, when: (lambda: None))
    await env.runner.async_start()
    assert env.state == (3, RUNNING)
    await env.step(hours=24)
    assert env.state == (4, VERIFIED) and ex.control_writes == [1] and ex.window is None
    assert env.runner.plan_allowed() is True


@pytest.mark.asyncio
async def test_runner_window_waits_for_a_fresh_reading(monkeypatch):
    env = Env(monkeypatch)
    env.ex.io.soc_age = 600.0                                      # odczyt sprzed 10 min
    await env.runner.async_start()
    await env.step(hours=24)
    assert env.state == (5, WAITING) and env.ex.window is None
    env.ex.io.soc_age = 5.0
    await env.step(minutes=1)
    assert env.state == (5, RUNNING) and env.ex.window is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["entities", "direct"])
async def test_runner_restart_mid_window_restores_in_both_modes(monkeypatch, kind):
    identity = f"{kind}|goodwe-et|fp"
    lad = Ladder(4, P, device_key=device_key(SALT, identity))
    lad.consent(True, GW_NOW)
    lad.tick(GW_NOW)
    lad.write_result(True, GW_NOW)
    lad.window_open(GW_NOW)
    assert lad.window_running
    io = FakeIO(kind=kind, identity=identity)
    io.conn = SimpleNamespace(conflict=False)
    ex = FakeExecutor(io, can_write=False, record=lad.to_record())
    env = Env(monkeypatch, ex, start=4)
    await env.runner.async_start()
    assert ex.restores == 1 and ex.forced == [True]
    assert env.state == (4, WAITING)


@pytest.mark.asyncio
async def test_draft_direct_profile_follows_the_plan_once_the_device_is_verified(
        make_hass, rtu_tcp_sim, deye_bank, issues):
    from tests.control.test_executor_direct import TOU_NOW, deye_target
    h = await Harness(make_hass, DEYE_DRAFT, deye_target(rtu_tcp_sim), utc=lambda: TOU_NOW,
                      rated=10000.0).start(raw=TOU_RAW)
    try:
        h.ex.verification = Gate(False)
        await h.ex.async_tick()
        assert deye_bank.writes == [] and h.ex.last_decision.status != WRITE
        h.ex.verification.allowed = True
        await h.cycle()
        assert h.ex.last_decision.status == WRITE and h.ex.owned
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_draft_direct_profile_without_a_ladder_stays_dry(make_hass, rtu_tcp_sim, deye_bank, issues):
    from tests.control.test_executor_direct import TOU_NOW, deye_target
    h = await Harness(make_hass, DEYE_DRAFT, deye_target(rtu_tcp_sim), utc=lambda: TOU_NOW,
                      rated=10000.0).start(raw=TOU_RAW)
    try:
        await h.ex.async_tick()
        assert deye_bank.writes == [] and h.ex.last_decision.status != WRITE
    finally:
        await h.close()


def test_entity_executor_plain_verification_restore_keeps_plan_control(monkeypatch):
    import asyncio

    from tests.control.test_executor import E, make, ready
    h, ex = make(monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_tick()
        assert ex.owned and h.states.get(E["mode"]).state == "sell_power"
        await ex.async_verification_restore()                     # tryb encji bez okna: nic
        assert ex.owned
    asyncio.run(go())


def test_entity_executor_restore_after_an_interrupted_window(monkeypatch):
    import asyncio

    from tests.control.test_executor import E, make, ready
    h, ex = make(monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_tick()
        assert ex.owned
        ex.schedule = None                                         # restart bez planu w magazynie
        await ex.async_verification_restore(force=True)
        assert not ex.owned and h.states.get(E["mode"]).state != "sell_power"
    asyncio.run(go())


def test_writing_supported_accepts_register_address_zero():
    tou0 = SimpleNamespace(control_model="time_window", raw={"write": {"tou_program": {"soc": {"addr": 0}}}})
    assert ver_mod.writing_supported(tou0, "direct") is True
