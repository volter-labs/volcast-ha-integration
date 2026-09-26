"""Zapis grupowy: tryb i jego nastawa mocy nigdy nie rozjeżdżają się na urządzeniu."""
import asyncio
import random
from dataclasses import dataclass

import pytest

from custom_components.volcast.core.control.cycle import (WRITE, ControlMemory, EntityContext, Gates,
                                                          Limits, Telemetry, commit, decide_cycle)
from custom_components.volcast.core.control.group_writes import (GroupReport, async_run_group_writes,
                                                                 order_group, power_first,
                                                                 run_group_writes)
from custom_components.volcast.core.profile import load_builtin
from custom_components.volcast.core.slot import parse_schedule
from custom_components.volcast.core.write_sequence import DENIED, ERROR, OK, UNSUPPORTED

GW = load_builtin("goodwe-et")
MAPPED = {"mode": "select.ems_mode", "power_w": "number.ems_power", "soc_min": "number.dod",
          "soc_max": "number.soc_upper", "export_limit_w": "number.export_limit",
          "export_limit_enabled": "switch.export_limit"}
UNITS = {"power_w": "W", "soc_min": "%", "soc_max": "%", "export_limit_w": "W"}
ATTRS = {"number.ems_power": {"min": 0, "max": 10000, "step": 1},
         "number.dod": {"min": 0, "max": 99, "step": 1},
         "number.soc_upper": {"min": 10, "max": 100, "step": 1},
         "number.export_limit": {"min": 0, "max": 10000, "step": 1}}
OPEN = Gates(consent=True, local_switch=True, control_mode="entities", verified=True)
NOW = "2026-09-27T10:30:00+00:00"


@dataclass(frozen=True)
class W:
    key: str
    data: dict


# ── Kolejność grupy ──

@pytest.mark.parametrize("new,prev,first", [
    (0.0, 3000.0, True),        # wejście w postój: moc najpierw (stan przejściowy: sprzedaż 0 W)
    (3000.0, 0.0, False),       # wyjście z postoju: tryb najpierw (sprzedaż na 0 W)
    (2500.0, 3000.0, True),     # sprzedaż 3000 → ładowanie 2500: moc najpierw
    (3000.0, 1500.0, False),    # ładowanie 1500 → sprzedaż 3000: tryb najpierw
    (0.0, None, True),
    (2000.0, None, False),      # nieznana poprzednia moc: tryb najpierw (postój z dużym Xset to znana pułapka)
])
def test_power_first_rule(new, prev, first):
    assert power_first(new, prev) is first


def test_order_group_puts_rest_first_then_group_in_safe_order():
    ws = [W("soc_max", {}), W("power_w", {}), W("export_limit_enabled", {}), W("mode", {})]
    assert [w.key for w in order_group(ws, power_first=True)] == \
        ["soc_max", "export_limit_enabled", "power_w", "mode"]
    assert [w.key for w in order_group(ws, power_first=False)] == \
        ["soc_max", "export_limit_enabled", "mode", "power_w"]


# ── Wykonawca grupowy (bez cyklu) ──

def _writer(outcomes):
    calls = []

    def write(w):
        calls.append((w.key, w.data.get("v")))
        o = outcomes.get((w.key, w.data.get("v")), outcomes.get(w.key, OK))
        if o == "raise":
            raise RuntimeError("transport")
        return o
    return write, calls


def test_non_group_failure_skips_whole_group():
    ws = [W("export_limit_enabled", {}), W("power_w", {"v": 3000}), W("mode", {"v": "sell_power"})]
    write, calls = _writer({"export_limit_enabled": ERROR})
    rep = run_group_writes(ws, write)
    assert [c[0] for c in calls] == ["export_limit_enabled"]
    assert rep.failed == ["export_limit_enabled"] and rep.mode_held and rep.group_skipped
    assert rep.written == []


def test_non_group_unsupported_also_skips_group_this_tick():
    ws = [W("soc_max", {}), W("mode", {"v": "sell_power"}), W("power_w", {"v": 3000})]
    write, calls = _writer({"soc_max": UNSUPPORTED})
    rep = run_group_writes(ws, write)
    assert [c[0] for c in calls] == ["soc_max"] and rep.unsupported == ["soc_max"] and rep.mode_held


@pytest.mark.parametrize("outcome", [ERROR, DENIED, UNSUPPORTED, "raise"])
def test_first_group_member_not_ok_skips_second(outcome):
    ws = [W("mode", {"v": "sell_power"}), W("power_w", {"v": 3000})]
    write, calls = _writer({"mode": outcome})
    rep = run_group_writes(ws, write)
    assert [c[0] for c in calls] == ["mode"] and rep.written == [] and not rep.error


def test_second_failure_restores_first_and_reports_error():
    ws = [W("power_w", {"v": 0}), W("mode", {"v": "battery_standby"})]
    write, calls = _writer({"mode": ERROR})
    rep = run_group_writes(ws, write, restore={"power_w": W("power_w", {"v": 3000})})
    assert calls == [("power_w", 0), ("mode", "battery_standby"), ("power_w", 3000)]
    assert rep.written == [] and rep.restored == ["power_w"] and rep.error
    assert rep.failed == ["mode"] and not rep.mode_held


def test_failed_restore_is_reported_and_first_stays_written():
    ws = [W("mode", {"v": "sell_power"}), W("power_w", {"v": 3000})]
    write, _ = _writer({"power_w": "raise", ("mode", "battery_standby"): "raise"})
    rep = run_group_writes(ws, write, restore={"mode": W("mode", {"v": "battery_standby"})})
    assert rep.written == ["mode"] and rep.restore_failed == ["mode"] and rep.error
    assert rep.errors == {"power_w": "RuntimeError", "mode:restore": "RuntimeError"}


def test_missing_restore_is_a_failed_restore():
    ws = [W("mode", {"v": "sell_power"}), W("power_w", {"v": 3000})]
    write, _ = _writer({"power_w": ERROR})
    rep = run_group_writes(ws, write)
    assert rep.restore_failed == ["mode"] and rep.error


def test_all_ok_writes_everything_in_given_order():
    ws = [W("soc_min", {}), W("mode", {"v": "sell_power"}), W("power_w", {"v": 3000})]
    write, calls = _writer({})
    rep = run_group_writes(ws, write)
    assert [c[0] for c in calls] == ["soc_min", "mode", "power_w"]
    assert rep.written == ["soc_min", "mode", "power_w"] and not rep.error and isinstance(rep, GroupReport)


def test_async_twin_same_semantics():
    ws = [W("power_w", {"v": 0}), W("mode", {"v": "battery_standby"})]
    write, calls = _writer({"mode": ERROR})

    async def awrite(w):
        return write(w)

    rep = asyncio.run(async_run_group_writes(ws, awrite, restore={"power_w": W("power_w", {"v": 3000})}))
    assert calls == [("power_w", 0), ("mode", "battery_standby"), ("power_w", 3000)] and rep.restored == ["power_w"]


# ── Urządzenie modelowe + pełna pętla cyklu ──

class Device:
    def __init__(self, mode="battery_standby", power=0.0, fail=None, rng=None, fail_rate=0.0):
        self.state = {"mode": mode, "power_w": power, "export_limit_enabled": 0.0,
                      "export_limit_w": 0.0, "soc_min": 10.0, "soc_max": 100.0}
        self.fail = fail or {}
        self.rng = rng
        self.fail_rate = fail_rate

    def outcome(self, key):
        if key in self.fail:
            return self.fail[key]
        if self.rng is not None and self.rng.random() < self.fail_rate:
            return self.rng.choice([ERROR, DENIED, UNSUPPORTED, "raise"])
        return OK

    def write(self, w):
        o = self.outcome(w.key)
        if o == "raise":
            raise RuntimeError("transport")
        if o != OK:
            return o
        if w.key == "mode":
            self.state["mode"] = w.data["option"]
        elif w.key == "export_limit_enabled":
            self.state[w.key] = 1.0 if w.service == "turn_on" else 0.0
        elif w.key == "soc_min":
            self.state[w.key] = round(100.0 - w.data["value"], 1)       # encja DoD
        else:
            self.state[w.key] = float(w.data["value"])
        return OK

    def pair(self):
        return self.state["mode"], self.state["power_w"]


def schedule(**slot):
    s = {"from": "2026-09-27T10:00:00Z", "to": "2026-09-27T11:00:00Z", "price_pln_kwh": 0.8, **slot}
    return parse_schedule({"schedule_id": "s", "slots": [s],
                           "fallback": {"mode": "self_consume", "soc_reserve": 10}, "control_enabled": True})


SHAPES = [
    dict(mode="discharge", discharge_purpose="sell", power_w=2000),
    dict(mode="discharge", discharge_purpose="sell", power_w=3000, export_allowed=False),
    dict(mode="discharge", discharge_purpose="sell", power_w=2500),
    dict(mode="idle"),
    dict(mode="charge", charge_source="grid", power_w=1500),
    dict(mode="charge", charge_source="grid", power_w=2500, soc_target=90),
    dict(mode="self_consume"),
]


def tick(dev, mem, sched, t):
    from datetime import datetime
    d = decide_cycle(profile=GW, schedule=sched, now_utc=datetime.fromisoformat(NOW), now_mono=t,
                     tele=Telemetry(soc=60.0, soc_age_s=5.0, battery_temp_c=25.0),
                     limits=Limits(rated_power_w=8000.0),
                     ents=EntityContext(domain="goodwe", mapped=MAPPED, units=UNITS, attrs=ATTRS,
                                        readings=dict(dev.state)),
                     gates=OPEN, memory=mem)
    rep = None
    if d.status == WRITE:
        rep = run_group_writes(d.writes, dev.write, restore=d.restore)
        commit(d, rep, mem, t)
    return d, rep


def _standby_charging(dev):
    return dev.state["mode"] == "battery_standby" and dev.state["power_w"] > 0


@pytest.mark.parametrize("slot,fail", [
    (dict(mode="discharge", discharge_purpose="sell", power_w=3000, export_allowed=False),
     {"export_limit_enabled": ERROR}),
    (dict(mode="charge", charge_source="grid", power_w=2500, soc_target=90), {"soc_max": DENIED}),
    (dict(mode="discharge", discharge_purpose="sell", power_w=3000), {"mode": ERROR}),
])
def test_failing_write_never_leaves_standby_with_power(slot, fail):
    dev = Device(fail=fail)
    mem = ControlMemory.for_profile(GW)
    for t in (60.0, 120.0, 180.0, 240.0):
        tick(dev, mem, schedule(**slot), t)
        assert dev.pair() == ("battery_standby", 0.0)


def test_mode_failure_after_power_restores_power():
    dev = Device(mode="sell_power", power=3000.0, fail={"mode": ERROR})
    mem = ControlMemory.for_profile(GW)
    d, rep = tick(dev, mem, schedule(mode="idle"), 60.0)
    assert [w.key for w in d.writes] == ["power_w", "mode"]
    assert rep.restored == ["power_w"] and rep.error and dev.pair() == ("sell_power", 3000.0)


def test_power_failure_after_mode_restores_mode():
    dev = Device(fail={"power_w": ERROR})
    mem = ControlMemory.for_profile(GW)
    d, rep = tick(dev, mem, schedule(mode="discharge", discharge_purpose="sell", power_w=3000), 60.0)
    assert [w.key for w in d.writes] == ["mode", "power_w"]
    assert rep.restored == ["mode"] and dev.pair() == ("battery_standby", 0.0)
    # Następny cykl próbuje całej grupy od nowa (nic nie zapamiętane jako zapisane).
    dev.fail = {}
    tick(dev, mem, schedule(mode="discharge", discharge_purpose="sell", power_w=3000), 120.0)
    assert dev.pair() == ("sell_power", 3000.0)


def _check_pair(before, after, planned, rep):
    """Tryb i moc: stan sprzed, stan z planu albo — tylko po nieudanym cofnięciu — mniejsza moc."""
    if after in (before, planned):
        return
    assert rep is not None and rep.restore_failed, (before, after, planned)
    mode_b, pow_b = before
    mode_a, pow_a = after
    if mode_a != mode_b:            # nowy tryb na starej mocy — tylko gdy stara moc mniejsza
        assert pow_a == pow_b and pow_b <= planned[1], (before, after, planned)
    else:                            # stary tryb na nowej mocy — tylko gdy nowa moc mniejsza
        assert pow_a == planned[1] and pow_a <= pow_b, (before, after, planned)


@pytest.mark.parametrize("seed", range(300))
def test_fuzz_single_step_group_consistency(seed):
    rng = random.Random(seed)
    modes = ["auto", "battery_standby", "sell_power", "charge_battery", "discharge_battery"]
    dev = Device(mode=rng.choice(modes), power=rng.choice([0.0, 1000.0, 2500.0, 3000.0]),
                 rng=rng, fail_rate=rng.choice([0.2, 0.5, 0.8]))
    mem = ControlMemory.for_profile(GW)
    before = dev.pair()
    d, rep = tick(dev, mem, schedule(**rng.choice(SHAPES)), 1000.0)
    written = {w.key for w in d.writes}
    planned = (d.flat["mode"] if "mode" in written else before[0],
               d.flat["power_w"] if "power_w" in written else before[1])
    _check_pair(before, dev.pair(), planned, rep)
    if not (before[0] == "battery_standby" and before[1] > 0):
        assert not _standby_charging(dev)


@pytest.mark.parametrize("seed", range(60))
def test_fuzz_many_ticks_never_standby_with_power(seed):
    rng = random.Random(1000 + seed)
    dev = Device(rng=rng, fail_rate=0.1)
    mem = ControlMemory.for_profile(GW)
    t, sched = 0.0, schedule(**SHAPES[0])
    for _ in range(80):
        t += rng.choice([1.0, 5.0, 30.0, 59.9, 60.0, 60.1, 90.0, 240.0])
        if rng.random() < 0.3:
            sched = schedule(**rng.choice(SHAPES))
        before = dev.pair()
        d, rep = tick(dev, mem, sched, t)
        assert not _standby_charging(dev), (seed, t, before, dev.pair())
        if rep is not None:
            written = {w.key for w in d.writes}
            planned = (d.flat["mode"] if "mode" in written else before[0],
                       d.flat["power_w"] if "power_w" in written else before[1])
            _check_pair(before, dev.pair(), planned, rep)
