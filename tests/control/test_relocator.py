"""Relocator: samoczynne odnajdywanie falownika po utracie łącza bezpośredniego.

Sieć, identyfikacja i kolizje są atrapami (wstrzykiwane), zegar fałszywy. Adresy wyłącznie z bloków
dokumentacyjnych (RFC 5737).
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
from types import SimpleNamespace

import pytest

from custom_components.volcast.const import DOMAIN
from custom_components.volcast.control import direct_search as ds
from custom_components.volcast.control import relocator as rl
from custom_components.volcast.control import runtime as rt_mod
from custom_components.volcast.control.direct import START_FAILED
from custom_components.volcast.core.discovery.identify import MAX_CANDIDATES, Candidate, Identity
from custom_components.volcast.core.discovery.probe import ProbeReport
from custom_components.volcast.core.modbus.identity import device_fingerprint
from custom_components.volcast.core.registers import RegisterImage
from custom_components.volcast.core.transports.base import TransportConfig
from tests.control.test_executor_direct import GW_V, SALT, issues, verified  # noqa: F401 — `issues` to fikstura
from tests.sim.fixtures import goodwe_words

FOX_V = verified("foxess-h")
GW_FP = device_fingerprint(SALT, GW_V, RegisterImage(goodwe_words()))
FOREIGN_FP = "ffeeddccbbaa9988"
OLD, NEW, OTHER, HA_IP = "192.0.2.10", "192.0.2.42", "198.51.100.7", "203.0.113.5"
NOW = SimpleNamespace(isoformat=lambda: "2026-11-15T10:00:00+00:00")


def _report(host, *, fp=GW_FP, profile_id="goodwe-et", transport="goodwe_udp", port=8899, unit=247,
            logger_serial=None, identified=True):
    ident = Identity(profile_id, transport, port, unit, "GW10K-ET", 10000.0, fp) if identified else None
    return ProbeReport(ident, {"mode": True}, (), identified, None, "verified", 4, (),
                       Candidate(host, "sweep", logger_serial, (transport,)))


def _target(**over) -> dict:
    """Cel tak, jak zapisuje go sonda (`target_from_report`) — bez `fp_kind`."""
    t = ds.target_from_report(_report(OLD))
    assert "fp_kind" not in t
    t.update(over)
    return t


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class FakeConn:
    def __init__(self, clock, profile=GW_V, *, trial=False):
        self._clock, self.profile, self.trial = clock, profile, trial
        self.reading_at: float | None = None
        self.refusal: str | None = None

    def refused(self):
        return self.refusal

    def age_s(self):
        return math.inf if self.reading_at is None else self._clock() - self.reading_at


class FakeEntries:
    def __init__(self, entry):
        self.entry, self.updates = entry, []

    def async_get_entry(self, entry_id):
        return self.entry if entry_id == self.entry.entry_id else None

    def async_entries(self, domain=None):
        return [self.entry]

    def async_update_entry(self, entry, *, options):
        self.updates.append(options)
        entry.options = options


class FakeEntry:
    domain = DOMAIN

    def __init__(self, options):
        self.entry_id, self.options, self.data, self.disabled_by = "e1", options, {}, None

    def async_create_background_task(self, hass, coro, name):
        return asyncio.get_running_loop().create_task(coro)


class Net:
    """Atrapa sieci: rozgłoszenie, skan, identyfikacja (raporty), kolizje — z zapisem wywołań."""

    def __init__(self, *, reports=(), swept=None, replies=(), clashes=None, block: asyncio.Event | None = None):
        self.reports = list(reports)
        self.swept = [Candidate(r.candidate.host, "sweep", r.candidate.logger_serial, ())
                      for r in self.reports] if swept is None else list(swept)
        self.replies, self.clashes, self.block = list(replies), dict(clashes or {}), block
        self.udp_calls, self.sweep_calls, self.search_calls, self.clash_calls = [], [], [], []

    async def udp_probe(self, **kw):
        self.udp_calls.append(kw)
        return SimpleNamespace(replies=self.replies if "target" not in kw else [])

    async def sweep(self, subnets, **kw):
        self.sweep_calls.append((list(subnets), kw))
        if self.block is not None:
            await self.block.wait()
        return list(self.swept)

    async def search(self, candidates, *, port, unit_id):
        self.search_calls.append((list(candidates), port, unit_id))
        return list(self.reports)

    async def clash(self, host):
        self.clash_calls.append(host)
        return self.clashes.get(host, ())

    async def adapters(self):
        return [{"enabled": True, "ipv4": [{"address": HA_IP, "network_prefix": 24}]}]


def _make(net: Net, *, target=None, profile=GW_V, options=None, control_change=lambda o, n: False,
          onboarding=lambda: False, clock=None):
    clock = clock or Clock()
    opts = options if options is not None else {"control_mode": "direct", "direct_target": target or _target(),
                                                "rated_power_w": 9000}
    entry = FakeEntry(opts)
    hass = SimpleNamespace(data={})
    hass.config_entries = FakeEntries(entry)
    conn = FakeConn(clock, profile)
    rel = rl.Relocator(hass, entry, conn, control_change=control_change, onboarding_active=onboarding,
                       search=net.search, clash=net.clash, adapters=net.adapters, udp_probe=net.udp_probe,
                       sweep_fn=net.sweep, clock=clock, utcnow=lambda: NOW)
    return SimpleNamespace(rel=rel, hass=hass, entry=entry, conn=conn, clock=clock, net=net)


async def _run(ctx, at: float | None = None) -> bool:
    """Takt sprawdzenia (opcjonalnie o czasie `at`); True = wyszukiwanie ruszyło i się skończyło."""
    if at is not None:
        ctx.clock.t = at
    before = ctx.rel.task
    ctx.rel.check()
    task = ctx.rel.task
    if task is None or task is before:
        return False
    await task
    return True


# ── przepięcie ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_relocation_changes_only_the_host():
    ctx = _make(Net(reports=[_report(NEW)]))
    old_opts = dict(ctx.entry.options)
    assert await _run(ctx, 180.0)
    assert len(ctx.hass.config_entries.updates) == 1
    new = ctx.hass.config_entries.updates[0]
    assert {k: v for k, v in new.items() if k != "direct_target"} == \
        {k: v for k, v in old_opts.items() if k != "direct_target"}
    assert new["direct_target"] == {**old_opts["direct_target"], "host": NEW}
    assert "fp_kind" not in new["direct_target"]          # rodzaj odcisku liczony, nie zapisywany
    st = ctx.rel.state
    assert st.relocations == 1 and st.last_outcome == "relocated" and st.last_at == NOW.isoformat()


@pytest.mark.asyncio
async def test_solarman_relocation_carries_the_new_logger_serial():
    target = _target(transport="solarman_v5", logger_serial=2712345678)
    report = _report(NEW, transport="solarman_v5", logger_serial=2798765432)
    ctx = _make(Net(reports=[report]), target=target)
    assert await _run(ctx, 180.0)
    new_target = ctx.hass.config_entries.updates[0]["direct_target"]
    assert new_target == {**target, "host": NEW, "logger_serial": 2798765432}


@pytest.mark.asyncio
async def test_probe_uses_the_target_port_and_unit():
    ctx = _make(Net(reports=[_report(NEW, port=502, unit=1, transport="modbus_tcp")]),
                target=_target(transport="modbus_tcp", port=502, unit_id=1))
    assert await _run(ctx, 180.0)
    (_cands, port, unit), = ctx.net.search_calls
    assert (port, unit) == (502, 1)
    assert ctx.hass.config_entries.updates[0]["direct_target"]["host"] == NEW
    # TCP: bez rozgłoszenia UDP; skan na porcie celu
    assert ctx.net.udp_calls == [] and ctx.net.sweep_calls[0][1]["port"] == 502


@pytest.mark.asyncio
async def test_default_search_probes_with_the_target_port_and_unit_factory(monkeypatch):
    seen = {}

    async def fake_search(hass, entry, profiles, **kw):
        seen.update(kw, profiles=profiles)
        return []

    monkeypatch.setattr(ds, "async_search", fake_search)
    ctx = _make(Net(swept=[Candidate(NEW, "sweep")]), target=_target(port=8898, unit_id=3))
    ctx.rel._search = ctx.rel._default_search
    assert await _run(ctx, 180.0)
    assert [c.host for c in seen["candidates"]] == [NEW] and seen["profiles"] == [GW_V]
    made = []
    monkeypatch.setattr(ds, "make_transport", lambda cfg, **kw: made.append(cfg))
    seen["transport_factory"](TransportConfig(kind="goodwe_udp", host=NEW, port=8899, unit=247))
    assert (made[0].port, made[0].unit, made[0].host) == (8898, 3, NEW)


@pytest.mark.parametrize("reports, clashes, outcome", [
    ([_report(NEW), _report(OTHER)], {}, "ambiguous"),
    ([_report(NEW, fp=FOREIGN_FP)], {}, "foreign_only"),
    ([_report(NEW)], {NEW: ("goodwe",)}, "clash"),
    ([_report(NEW)], {NEW: ("unknown",)}, "clash"),
    ([_report(NEW, identified=False)], {}, "not_found"),
    ([], {}, "not_found"),
    ([_report(OLD)], {}, "same_host"),
    ([_report(NEW, profile_id="goodwe-dt")], {}, "foreign_only"),
    ([_report(NEW, transport="modbus_tcp")], {}, "foreign_only"),
])
@pytest.mark.asyncio
async def test_doubtful_results_never_relocate(reports, clashes, outcome):
    net = Net(reports=reports, clashes=clashes,
              swept=[Candidate(NEW, "sweep")] if not reports else None)
    ctx = _make(net)
    assert await _run(ctx, 180.0)
    assert ctx.hass.config_entries.updates == []
    assert ctx.rel.state.last_outcome == outcome and ctx.rel.state.relocations == 0


@pytest.mark.asyncio
async def test_clash_is_checked_for_every_candidate():
    net = Net(reports=[_report(NEW), _report(OTHER, fp=FOREIGN_FP)])
    ctx = _make(net)
    assert await _run(ctx, 180.0)
    assert sorted(net.clash_calls) == sorted([NEW, OTHER])


@pytest.mark.asyncio
async def test_failed_clash_check_never_relocates():
    net = Net(reports=[_report(NEW)])
    ctx = _make(net)

    async def no_answer(host):
        raise OSError("resolver down")
    ctx.rel._clash = no_answer                            # brak wiedzy o kolizji: wyszukiwanie bez zapisu
    assert await _run(ctx, 180.0)
    assert ctx.hass.config_entries.updates == [] and ctx.rel.state.last_outcome == "not_found"


@pytest.mark.asyncio
async def test_truncated_candidate_list_is_not_conclusive():
    swept = [Candidate(f"192.0.2.{20 + i}", "sweep") for i in range(MAX_CANDIDATES + 1)]
    net = Net(reports=[_report(NEW)], swept=swept)
    ctx = _make(net)
    assert await _run(ctx, 180.0)
    assert net.search_calls == [] and ctx.hass.config_entries.updates == []
    st = ctx.rel.state
    assert st.last_outcome == "not_found" and st.last_truncated is True and st.last_candidates == MAX_CANDIDATES + 1


@pytest.mark.asyncio
async def test_broadcast_and_sweep_hits_count_once_towards_the_cap():
    # ten sam adres z rozgłoszenia i ze skanu = jeden kandydat; cztery unikalne mieszczą się w limicie
    replies = [SimpleNamespace(ip=NEW, logger_serial=None), SimpleNamespace(ip="192.0.2.21", logger_serial=None)]
    swept = [Candidate(NEW, "sweep"), Candidate("192.0.2.22", "sweep"), Candidate("192.0.2.23", "sweep")]
    net = Net(reports=[_report(NEW)], swept=swept, replies=replies)
    ctx = _make(net)
    assert await _run(ctx, 180.0)
    (cands, _p, _u), = net.search_calls
    assert [c.host for c in cands] == [NEW, "192.0.2.21", "192.0.2.22", "192.0.2.23"]
    assert all(c.transports == ("goodwe_udp",) for c in cands)
    assert ctx.rel.state.last_outcome == "relocated"


@pytest.mark.asyncio
async def test_home_assistant_own_address_is_skipped():
    replies = [SimpleNamespace(ip=HA_IP, logger_serial=None)]
    net = Net(reports=[_report(NEW)], replies=replies)
    ctx = _make(net)
    assert await _run(ctx, 180.0)
    assert HA_IP in net.sweep_calls[0][1]["skip"]
    assert HA_IP not in [c.host for c in net.search_calls[0][0]]


def test_old_host_subnet_is_swept_first():
    import ipaddress
    nets = [ipaddress.ip_network("192.0.2.0/24"), ipaddress.ip_network("198.51.100.0/24")]
    assert rl.ordered_subnets(nets, "198.51.100.9") == list(reversed(nets))
    assert rl.ordered_subnets(nets, "not-an-ip") == nets


# ── rodzaj odcisku ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_weak_fingerprint_profile_never_relocates_nor_probes():
    target = {"profile_id": "foxess-h", "transport": "modbus_tcp", "host": OLD, "port": 502, "unit_id": 247,
              "device_fp": "0123456789abcdef"}
    net = Net(reports=[_report(NEW, fp="0123456789abcdef", profile_id="foxess-h", transport="modbus_tcp",
                               port=502)])
    ctx = _make(net, target=target, profile=FOX_V)
    assert await _run(ctx, 180.0)
    assert ctx.hass.config_entries.updates == [] and ctx.rel.state.last_outcome == "not_found"
    assert net.sweep_calls == [] and net.udp_calls == [] and net.search_calls == []


@pytest.mark.parametrize("over", [{"fp_kind": "model"}, {"device_fp": None}, {"profile_id": "goodwe-dt"}])
@pytest.mark.asyncio
async def test_target_without_a_serial_identity_never_relocates(over):
    net = Net(reports=[_report(NEW)])
    ctx = _make(net, target=_target(**over))
    assert await _run(ctx, 180.0)
    assert ctx.hass.config_entries.updates == [] and net.sweep_calls == []


# ── harmonogram ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_schedule_0_5_15_60_minutes_and_reset_on_recovery():
    net = Net(swept=[])                                  # nic w sieci: każde wyszukiwanie = not_found
    ctx = _make(net)
    assert not await _run(ctx, 179.0)
    runs = []
    for at in (180.0, 479.0, 480.0, 1379.0, 1380.0, 4979.0, 4980.0, 8579.0, 8580.0):
        if await _run(ctx, at):
            runs.append(at)
    assert runs == [180.0, 480.0, 1380.0, 4980.0, 8580.0]
    assert ctx.rel.state.attempts == 5 and len(net.sweep_calls) == 5
    # udany odczyt: harmonogram od zera, następne wyszukiwanie dopiero po RELOCATE_AFTER_S ciszy
    ctx.conn.reading_at = 9000.0
    assert not await _run(ctx, 9000.0)
    assert ctx.rel.state.attempts == 0 and ctx.rel.state.next_due is None
    assert not await _run(ctx, 9179.0)
    assert await _run(ctx, 9180.0)
    assert ctx.rel.state.attempts == 1


@pytest.mark.asyncio
async def test_start_failure_searches_at_once_other_refusals_never():
    ctx = _make(Net(swept=[]))
    ctx.conn.refusal = START_FAILED
    assert await _run(ctx, 1.0)
    for refusal in ("direct_in_use", "direct_conflict:goodwe", "bad_target"):
        other = _make(Net(swept=[]))
        other.conn.refusal = refusal
        assert not await _run(other, 10_000.0)


@pytest.mark.asyncio
async def test_schedule_survives_the_reload_after_relocation():
    ctx = _make(Net(reports=[_report(NEW)]))
    assert await _run(ctx, 180.0)
    # przeładowanie: nowy Relocator tego wpisu, łącze dalej milczy — następna próba po 5 min, nie od razu
    again = rl.Relocator(ctx.hass, ctx.entry, FakeConn(ctx.clock), control_change=lambda o, n: False,
                         onboarding_active=lambda: False, search=ctx.net.search, clash=ctx.net.clash,
                         adapters=ctx.net.adapters, udp_probe=ctx.net.udp_probe, sweep_fn=ctx.net.sweep,
                         clock=ctx.clock, utcnow=lambda: NOW)
    ctx.rel = again
    assert not await _run(ctx, 400.0)
    assert await _run(ctx, 480.0)
    assert again.state.relocations == 1 and again.state.last_outcome == "same_host"


# ── jedno naraz, blokada, anulowanie ────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_parallel_runs():
    block = asyncio.Event()
    ctx = _make(Net(swept=[], block=block))
    ctx.clock.t = 180.0
    ctx.rel.check()
    first = ctx.rel.task
    await asyncio.sleep(0)
    ctx.clock.t = 10_000.0
    ctx.rel.check()
    assert ctx.rel.task is first and len(ctx.net.sweep_calls) == 1
    block.set()
    await first


@pytest.mark.asyncio
async def test_unload_cancels_the_search_and_nothing_probes_afterwards():
    block = asyncio.Event()
    net = Net(reports=[_report(NEW)], block=block)
    ctx = _make(net)
    ctx.clock.t = 180.0
    ctx.rel.check()
    task = ctx.rel.task
    await asyncio.sleep(0)
    assert len(net.sweep_calls) == 1
    ctx.rel.stop()
    block.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    calls = (len(net.udp_calls), len(net.sweep_calls), len(net.search_calls))
    for at in (10_000.0, 20_000.0):
        ctx.rel.check()
        ctx.clock.t = at
    assert ctx.rel.task is task
    assert (len(net.udp_calls), len(net.sweep_calls), len(net.search_calls)) == calls
    assert net.search_calls == [] and ctx.hass.config_entries.updates == []
    assert rl.search_lock(ctx.hass, "e1").locked() is False


@pytest.mark.asyncio
async def test_manual_search_holding_the_lock_skips_the_run_without_counting_it():
    ctx = _make(Net(swept=[]))
    lock = rl.search_lock(ctx.hass, "e1")
    await lock.acquire()
    try:
        assert await _run(ctx, 180.0)
        assert ctx.net.sweep_calls == [] and ctx.rel.state.attempts == 0
    finally:
        lock.release()
    assert await _run(ctx, 210.0)
    assert len(ctx.net.sweep_calls) == 1 and ctx.rel.state.attempts == 1


@pytest.mark.asyncio
async def test_manual_search_waits_for_a_running_relocation(make_hass, monkeypatch):
    hass = make_hass()
    entry = SimpleNamespace(entry_id="e1", options={})
    calls = []

    async def fake_search(hass_, entry_, profiles, **kw):
        calls.append(kw)
        return []
    monkeypatch.setattr(ds, "async_search", fake_search)
    monkeypatch.setattr(ds, "load_profiles", lambda: [GW_V])
    lock = rl.search_lock(hass, "e1")
    await lock.acquire()
    manual = asyncio.ensure_future(rt_mod.async_direct_search(hass, entry, port=502, unit_id=1))
    for _ in range(5):
        await asyncio.sleep(0)
    assert calls == [] and not manual.done()
    lock.release()
    assert await manual == [] and len(calls) == 1
    made = []
    monkeypatch.setattr(ds, "make_transport", lambda cfg, **kw: made.append(cfg))
    calls[0]["transport_factory"](TransportConfig(kind="modbus_tcp", host=NEW, port=8899, unit=247))
    assert (made[0].port, made[0].unit) == (502, 1)


@pytest.mark.asyncio
async def test_target_changed_during_the_search_is_not_overwritten():
    net = Net(reports=[_report(NEW)])
    ctx = _make(net)

    async def search(candidates, *, port, unit_id):
        ctx.entry.options = {**ctx.entry.options, "direct_target": _target(unit_id=1)}
        return list(net.reports)
    ctx.rel._search = search
    assert await _run(ctx, 180.0)
    assert ctx.hass.config_entries.updates == [] and ctx.rel.state.last_outcome == rl.TARGET_CHANGED


@pytest.mark.asyncio
async def test_write_that_would_be_a_control_change_is_blocked():
    ctx = _make(Net(reports=[_report(NEW)]), control_change=lambda old, new: True)
    assert await _run(ctx, 180.0)
    assert ctx.hass.config_entries.updates == [] and ctx.rel.state.last_outcome == rl.BLOCKED
    assert ctx.rel.state.relocations == 0


# ── kiedy w ogóle działa ────────────────────────────────────────────────────


def _rt(conn, options):
    entry = FakeEntry(options)
    hass = SimpleNamespace(data={})
    hass.config_entries = FakeEntries(entry)
    rt = SimpleNamespace(direct=conn, unsubs=[], executor=None, relocator=None)
    return hass, entry, rt


def test_not_started_without_a_direct_link_in_trial_or_entity_mode():
    clock = Clock()
    for conn, opts in ((None, {"control_mode": "direct", "direct_target": _target()}),
                       (FakeConn(clock, trial=True), {"direct_trial": True, "direct_target": _target()}),
                       (FakeConn(clock), {"control_mode": "entities", "direct_target": _target()})):
        hass, entry, rt = _rt(conn, opts)
        assert rt_mod.start_relocator(hass, entry, rt) is None and rt.unsubs == []


@pytest.mark.asyncio
async def test_started_in_direct_mode_and_stopped_with_the_entry():
    hass, entry, rt = _rt(FakeConn(Clock()), {"control_mode": "direct", "direct_target": _target()})
    rel = rt_mod.start_relocator(hass, entry, rt)
    assert isinstance(rel, rl.Relocator) and rt.relocator is rel and rel.stop in rt.unsubs
    for unsub in rt.unsubs:
        unsub()
    rel.check()
    assert rel.task is None


@pytest.mark.asyncio
async def test_nothing_happens_while_onboarding_runs():
    clock = Clock()
    hass, entry, rt = _rt(FakeConn(clock), {"control_mode": "direct", "direct_target": _target()})
    pending = asyncio.get_running_loop().create_future()
    hass.data[rt_mod.ONBOARDING_KEY] = {"e1": (pending, "s1")}
    rel = rt_mod.start_relocator(hass, entry, rt)
    rel._clock, rel._started_at = clock, 0.0
    clock.t = 10_000.0
    rel.check()
    assert rel.task is None
    pending.set_result(None)                             # onboarding skończony — wyszukiwanie może ruszyć
    net = Net(swept=[])
    rel._adapters, rel._sweep, rel._udp_probe = net.adapters, net.sweep, net.udp_probe
    rel.check()
    assert rel.task is not None
    await rel.task
    assert len(net.sweep_calls) == 1


# ── direct_search: lista kandydatów ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_async_search_probes_the_given_candidates_without_broadcast(make_hass, monkeypatch):
    hass = make_hass()
    seen = []

    async def fake_discover(candidates, profiles, **kw):
        seen.append(list(candidates))
        return []

    async def no_broadcast():
        raise AssertionError("broadcast must not run with an explicit candidate list")
    monkeypatch.setattr(ds, "discover", fake_discover)
    cands = [Candidate(NEW, "sweep", None, ("goodwe_udp",))]
    out = await ds.async_search(hass, SimpleNamespace(entry_id="e1"), [GW_V], candidates=cands,
                                udp_probe=no_broadcast)
    assert out == [] and seen == [cands]


# ── diagnostyka i logi bez adresów ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_logs_never_carry_addresses(caplog):
    caplog.set_level(logging.DEBUG)
    ctx = _make(Net(reports=[_report(NEW), _report(OTHER, fp=FOREIGN_FP)]))
    assert await _run(ctx, 180.0)
    assert ctx.rel.state.last_outcome == "relocated"
    for addr in (OLD, NEW, OTHER, HA_IP):
        assert addr not in caplog.text


@pytest.mark.asyncio
async def test_diagnostics_show_counters_without_addresses(make_hass, goodwe_udp_sim, issues):  # noqa: F811
    from tests.control.test_executor_direct import Harness, gw_target
    from tests.test_diagnostics_direct import _diag
    ctx = _make(Net(reports=[_report(NEW), _report(OTHER, fp=FOREIGN_FP)]))
    assert await _run(ctx, 180.0)
    target = gw_target(goodwe_udp_sim)
    h = await Harness(make_hass, GW_V, target).start()
    try:
        h.hass.data[rl.STATE_KEY] = {h.entry.entry_id: ctx.rel.state}
        out = await _diag(h)
        reloc = out["control"]["direct"]["relocation"]
        assert reloc["relocations"] == 1 and reloc["last_relocate_outcome"] == "relocated"
        assert reloc["last_relocate_at"] == NOW.isoformat() and reloc["last_relocate_candidates"] == 2
        blob = json.dumps(out)
        for addr in (OLD, NEW, OTHER, HA_IP, goodwe_udp_sim.host):
            assert addr not in blob
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_diagnostics_without_relocation_state(make_hass, goodwe_udp_sim, issues):  # noqa: F811
    from tests.control.test_executor_direct import Harness, gw_target
    from tests.test_diagnostics_direct import _diag
    h = await Harness(make_hass, GW_V, gw_target(goodwe_udp_sim)).start()
    try:
        assert (await _diag(h))["control"]["direct"]["relocation"] is None
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_tcp_connect_closes_the_socket_and_sends_nothing(monkeypatch):
    closed, written = [], []

    async def wait_closed():
        return None
    writer = SimpleNamespace(close=lambda: closed.append(True), write=written.append, wait_closed=wait_closed)

    async def accepted(host, port):
        return object(), writer

    async def refused(host, port):
        raise ConnectionRefusedError()
    monkeypatch.setattr(asyncio, "open_connection", accepted)
    assert await rl.tcp_connect(NEW, 502, 0.5) is True and closed == [True] and written == []
    monkeypatch.setattr(asyncio, "open_connection", refused)
    assert await rl.tcp_connect(NEW, 502, 0.5) is False

