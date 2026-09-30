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
# Świeże PV 0 W i pobór 0 W: nastawa eksportu sprzedaży równa mocy baterii ze slotu.
HOUSE_IDLE = dict(pv_power_w=0.0, pv_age_s=1.0, load_power_w=0.0, load_age_s=1.0)


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
    write, calls = _writer({"mode": DENIED})
    rep = run_group_writes(ws, write, restore={"power_w": W("power_w", {"v": 3000})})
    assert calls == [("power_w", 0), ("mode", "battery_standby"), ("power_w", 3000)]
    assert rep.written == [] and rep.restored == ["power_w"] and rep.error
    assert rep.failed == ["mode"] and not rep.mode_held


def test_failed_restore_is_reported_and_first_stays_written():
    ws = [W("mode", {"v": "sell_power"}), W("power_w", {"v": 3000})]
    write, _ = _writer({"power_w": "raise", ("mode", "battery_standby"): "raise"})
    rep = run_group_writes(ws, write, restore={"mode": W("mode", {"v": "battery_standby"})},
                           ambiguous_safe=("mode",))
    assert rep.written == ["mode"] and rep.restore_failed == ["mode"] and rep.error
    assert rep.errors == {"power_w": "RuntimeError", "mode:restore": "RuntimeError"}


def test_missing_restore_is_a_failed_restore():
    ws = [W("mode", {"v": "sell_power"}), W("power_w", {"v": 3000})]
    write, _ = _writer({"power_w": DENIED})
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
    write, calls = _writer({"mode": DENIED})

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


def tick(dev, mem, sched, t, hide=()):
    from datetime import datetime
    d = decide_cycle(profile=GW, schedule=sched, now_utc=datetime.fromisoformat(NOW), now_mono=t,
                     tele=Telemetry(soc=60.0, soc_age_s=5.0, battery_temp_c=25.0, **HOUSE_IDLE),
                     limits=Limits(rated_power_w=8000.0),
                     ents=EntityContext(domain="goodwe", mapped=MAPPED, units=UNITS, attrs=ATTRS,
                                        readings={k: v for k, v in dev.state.items() if k not in hide}),
                     gates=OPEN, memory=mem)
    rep = None
    if d.status == WRITE:
        rep = run_group_writes(d.writes, dev.write, restore=d.restore,
                               ambiguous_safe=d.restore_ambiguous_safe)
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
    dev = Device(mode="sell_power", power=3000.0, fail={"mode": DENIED})
    mem = ControlMemory.for_profile(GW)
    d, rep = tick(dev, mem, schedule(mode="idle"), 60.0)
    assert [w.key for w in d.writes] == ["power_w", "mode"]
    assert rep.restored == ["power_w"] and rep.error and dev.pair() == ("sell_power", 3000.0)


def test_power_failure_after_mode_restores_mode():
    dev = Device(fail={"power_w": DENIED})
    mem = ControlMemory.for_profile(GW)
    d, rep = tick(dev, mem, schedule(mode="discharge", discharge_purpose="sell", power_w=3000), 60.0)
    assert [w.key for w in d.writes] == ["mode", "power_w"]
    assert rep.restored == ["mode"] and dev.pair() == ("battery_standby", 0.0)
    # Grupa odczekuje odwrót, potem próbuje całości od nowa.
    dev.fail = {}
    tick(dev, mem, schedule(mode="discharge", discharge_purpose="sell", power_w=3000), 120.0)
    assert dev.pair() == ("battery_standby", 0.0)
    tick(dev, mem, schedule(mode="discharge", discharge_purpose="sell", power_w=3000), 361.0)
    assert dev.pair() == ("sell_power", 3000.0)


def _check_pair(before, after, planned, rep):
    """Tryb i moc: stan sprzed, stan z planu albo — tylko po nieudanym cofnięciu — mniejsza moc."""
    if after in (before, planned):
        return
    assert rep is not None and (rep.restore_failed or rep.restore_held), (before, after, planned)
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


# ── Obcy tryb i odwrót po cofnięciu ──

def test_foreign_mode_is_never_overwritten_by_restore():
    dev = Device()
    mem = ControlMemory.for_profile(GW)
    tick(dev, mem, schedule(mode="idle"), 0.0)                  # nasz postój/0 W w pamięci
    dev.state.update({"mode": "export_ac", "power_w": 1000.0})  # zmiana z zewnątrz, czytelna
    dev.fail = {"power_w": ERROR}
    for t in (60.0, 120.0, 180.0):
        d, rep = tick(dev, mem, schedule(mode="charge", charge_source="grid", power_w=3000), t)
        assert d.takeover is True and rep is None
        assert dev.pair() == ("export_ac", 1000.0)


class Counting(Device):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.nvm = {"mode": 0, "power_w": 0}
        self.flips = 0

    def write(self, w):
        before = GW.modes[self.state["mode"]].direction if self.state["mode"] in GW.modes else None
        o = super().write(w)
        if o == OK and w.key in self.nvm:
            self.nvm[w.key] += 1
            after = GW.modes[self.state["mode"]].direction
            if {before, after} == {"charge", "discharge"}:
                self.flips += 1
        return o


def _soak(dev, start_slot, slot, seconds=3600.0, step=60.0):
    mem = ControlMemory.for_profile(GW)
    tick(dev, mem, schedule(**start_slot), 0.0)                # stan wyjściowy zapisany przez nas
    dev.nvm = {k: 0 for k in dev.nvm}
    dev.flips = 0
    t, errors = 0.0, 0
    while t < seconds:
        t += step
        _, rep = tick(dev, mem, schedule(**slot), t)
        errors += bool(rep is not None and rep.error)
        assert not _standby_charging(dev)
    return mem, errors


def test_soak_failing_power_mode_first_stays_in_budget():
    dev = Counting()
    charge = dict(mode="charge", charge_source="grid", power_w=1500)
    sell = dict(mode="discharge", discharge_purpose="sell", power_w=3000)
    dev.fail = {}
    mem = ControlMemory.for_profile(GW)
    tick(dev, mem, schedule(**charge), 0.0)
    dev.fail = {"power_w": DENIED}
    dev.nvm, dev.flips = {"mode": 0, "power_w": 0}, 0
    t, round_trips = 0.0, 0
    while t < 3600.0:
        t += 60.0
        _, rep = tick(dev, mem, schedule(**sell), t)
        round_trips += bool(rep is not None and rep.restored)
    # budżet I-8: 4 zmiany kierunku na godzinę; każda próba to dwie (tam i z powrotem)
    assert dev.flips <= 4 and dev.nvm["mode"] <= 4 and round_trips <= 2
    assert dev.pair() == ("charge_battery", 1500.0)


def test_soak_failing_mode_power_first_backs_off():
    dev = Counting(mode="sell_power", power=3000.0, fail={"mode": DENIED})
    mem, errors = _soak(dev, dict(mode="discharge", discharge_purpose="sell", power_w=3000),
                        dict(mode="idle"))
    # odwrót 300 → 600 → 1200 → 2400 s: najwyżej 4 próby w godzinie, 2 zapisy mocy na próbę
    assert errors <= 4 and dev.nvm["power_w"] <= 8 and dev.flips == 0
    assert dev.pair() == ("sell_power", 3000.0) and mem.group_backoff_s >= 1200.0


def test_backoff_resets_after_full_group_write():
    dev = Device(mode="sell_power", power=3000.0, fail={"mode": DENIED})
    mem = ControlMemory.for_profile(GW)
    tick(dev, mem, schedule(mode="idle"), 60.0)
    assert mem.group_backoff_s == 300.0 and mem.group_backoff_until == 360.0
    d, _ = tick(dev, mem, schedule(mode="idle"), 120.0)
    assert "group_backoff" in d.notes and not any(w.key in ("mode", "power_w") for w in d.writes)
    dev.fail = {}
    tick(dev, mem, schedule(mode="idle"), 361.0)
    assert dev.pair() == ("battery_standby", 0.0)
    assert mem.group_backoff_s == 0.0 and mem.group_backoff_until is None


def test_round_trip_counts_in_throttle_and_limiter():
    dev = Device()
    mem = ControlMemory.for_profile(GW)
    tick(dev, mem, schedule(mode="charge", charge_source="grid", power_w=1500), 0.0)
    dev.fail = {"power_w": DENIED}
    d, rep = tick(dev, mem, schedule(mode="discharge", discharge_purpose="sell", power_w=3000), 60.0)
    assert rep.restored == ["mode"]
    assert mem.last_written["mode"] == "charge_battery"
    assert mem.limiter._changes and len(mem.limiter._changes) == 2      # tam i z powrotem
    assert mem.throttle.pending({"mode": "sell_power"}, 90.0) == {"mode"}


# ── Niejednoznaczny ERROR: zapis mógł dojść ──

class Landing(Device):
    """ERROR po zapisie, który DOSZEDŁ (zgubione potwierdzenie) — dla wskazanych kluczy albo losowo."""

    def __init__(self, *a, landed=(), landed_rate=0.0, **k):
        super().__init__(*a, **k)
        self.landed = set(landed)
        self.landed_rate = landed_rate

    def write(self, w):
        o = super().write(w)
        if o == OK and (w.key in self.landed or (self.rng is not None and self.rng.random() < self.landed_rate)):
            self.landed.discard(w.key)          # jednorazowo
            return ERROR
        return o


def test_ambiguous_error_holds_unsafe_restore():
    ws = [W("mode", {"v": "charge_battery"}), W("power_w", {"v": 3000})]
    for outcome in (ERROR, "raise"):
        write, calls = _writer({"power_w": outcome})
        rep = run_group_writes(ws, write, restore={"mode": W("mode", {"v": "battery_standby"})})
        assert [c[0] for c in calls] == ["mode", "power_w"]            # bez cofnięcia
        assert rep.restore_held == ["mode"] and rep.written == ["mode"] and rep.error
        assert rep.restored == [] and rep.restore_failed == []


def test_ambiguous_error_restores_only_safe_key():
    ws = [W("mode", {"v": "charge_battery"}), W("power_w", {"v": 3000})]
    write, calls = _writer({"power_w": ERROR})
    rep = run_group_writes(ws, write, restore={"mode": W("mode", {"v": "sell_power"})},
                           ambiguous_safe=("mode",))
    assert calls[-1] == ("mode", "sell_power") and rep.restored == ["mode"]


def test_async_twin_holds_ambiguous_restore():
    ws = [W("power_w", {"v": 0}), W("mode", {"v": "battery_standby"})]
    write, calls = _writer({"mode": ERROR})

    async def awrite(w):
        return write(w)

    rep = asyncio.run(async_run_group_writes(ws, awrite, restore={"power_w": W("power_w", {"v": 3000})}))
    assert [c[0] for c in calls] == ["power_w", "mode"] and rep.restore_held == ["power_w"]


@pytest.mark.parametrize("start,slot,landed,end", [
    ((("battery_standby", 0.0)), dict(mode="charge", charge_source="grid", power_w=3000), "power_w",
     ("charge_battery", 3000.0)),
    ((("sell_power", 3000.0)), dict(mode="idle"), "mode", ("battery_standby", 0.0)),
    ((("charge_battery", 1500.0)), dict(mode="discharge", discharge_purpose="sell", power_w=3000), "power_w",
     ("sell_power", 3000.0)),
])
def test_landed_error_never_restored_into_grid_charging(start, slot, landed, end):
    dev = Landing(mode=start[0], power=start[1], landed=[landed])
    mem = ControlMemory.for_profile(GW)
    d, rep = tick(dev, mem, schedule(**slot), 60.0)
    assert rep.restore_held and dev.pair() == end and not _standby_charging(dev)
    assert mem.group_backoff_until is None                 # bez odwrotu
    for t in (120.0, 180.0):
        tick(dev, mem, schedule(**slot), t)
        assert dev.pair() == end


def test_landed_error_with_safe_restore_is_corrected_next_tick():
    # sprzedaż 1500 → ładowanie 3000 (tryb pierwszy): powrót do sprzedaży jest bezpieczny,
    # więc cofamy; moc jednak doszła — następny cykl poprawia sam tryb, mimo odwrotu grupy.
    dev = Landing(mode="sell_power", power=1500.0, landed=["power_w"])
    mem = ControlMemory.for_profile(GW)
    charge = schedule(mode="charge", charge_source="grid", power_w=3000)
    d, rep = tick(dev, mem, charge, 60.0)
    assert d.restore_ambiguous_safe == ("mode",) and rep.restored == ["mode"]
    assert dev.pair() == ("sell_power", 3000.0) and mem.in_backoff(120.0)
    d2, _ = tick(dev, mem, charge, 120.0)
    assert [w.key for w in d2.writes] == ["mode"] and dev.pair() == ("charge_battery", 3000.0)


def test_unlanded_error_leaves_reduced_command_and_retries_without_backoff():
    dev = Device(fail={"power_w": ERROR})
    mem = ControlMemory.for_profile(GW)
    charge = schedule(mode="charge", charge_source="grid", power_w=3000)
    _, rep = tick(dev, mem, charge, 60.0)
    assert rep.restore_held == ["mode"] and dev.pair() == ("charge_battery", 0.0)
    dev.fail = {}
    d2, _ = tick(dev, mem, charge, 120.0)
    assert [w.key for w in d2.writes] == ["power_w"] and dev.pair() == ("charge_battery", 3000.0)


def test_backoff_only_when_both_members_must_change():
    dev = Device(mode="sell_power", power=3000.0, fail={"mode": DENIED})
    mem = ControlMemory.for_profile(GW)
    tick(dev, mem, schedule(mode="idle"), 60.0)
    assert mem.in_backoff(120.0) and dev.pair() == ("sell_power", 3000.0)
    dev.fail = {}
    dev.state["power_w"] = 0.0                              # np. zapis, który jednak doszedł
    d, _ = tick(dev, mem, schedule(mode="idle"), 120.0)
    assert [w.key for w in d.writes] == ["mode"] and "group_backoff" not in d.notes
    assert dev.pair() == ("battery_standby", 0.0)


def test_backoff_caps_at_one_hour():
    dev = Device(mode="sell_power", power=3000.0, fail={"mode": DENIED})
    mem = ControlMemory.for_profile(GW)
    t = 0.0
    for _ in range(7):
        t = (mem.group_backoff_until or t) + 1.0
        tick(dev, mem, schedule(mode="idle"), t)
    assert mem.group_backoff_s == 3600.0


def test_backoff_expires_when_clock_goes_backwards():
    dev = Device(mode="sell_power", power=3000.0, fail={"mode": DENIED})
    mem = ControlMemory.for_profile(GW)
    tick(dev, mem, schedule(mode="idle"), 1000.0)
    assert mem.in_backoff(1100.0) and not mem.in_backoff(10.0)


def test_soak_ambiguous_error_no_round_trips():
    dev = Counting()
    mem = ControlMemory.for_profile(GW)
    tick(dev, mem, schedule(mode="charge", charge_source="grid", power_w=1500), 0.0)
    dev.fail = {"power_w": ERROR}
    dev.nvm, dev.flips = {"mode": 0, "power_w": 0}, 0
    t = 0.0
    while t < 3600.0:
        t += 60.0
        tick(dev, mem, schedule(mode="discharge", discharge_purpose="sell", power_w=3000), t)
        assert not _standby_charging(dev)
    # jeden przełącz trybu (sprzedaż na starej, mniejszej mocy), bez rund i bez odwrotu
    assert dev.flips == 1 and dev.nvm == {"mode": 1, "power_w": 0}
    assert dev.pair() == ("sell_power", 1500.0) and mem.group_backoff_until is None


@pytest.mark.parametrize("seed", range(60))
def test_fuzz_landed_errors_never_leave_standby_with_power(seed):
    rng = random.Random(5000 + seed)
    dev = Landing(rng=rng, fail_rate=0.1, landed_rate=0.05)
    mem = ControlMemory.for_profile(GW)
    t, sched = 0.0, schedule(**SHAPES[0])
    for _ in range(80):
        t += rng.choice([5.0, 30.0, 60.0, 90.0])
        if rng.random() < 0.3:
            sched = schedule(**rng.choice(SHAPES))
        tick(dev, mem, sched, t)
        assert not _standby_charging(dev), (seed, t, dev.pair())


class LandingRestore(Device):
    """Drugi zapis mocy (cofnięcie) dochodzi, ale zwraca ERROR."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.power_writes = 0

    def write(self, w):
        o = super().write(w)
        if w.key == "power_w" and o == OK:
            self.power_writes += 1
            if self.power_writes == 2:
                return ERROR
        return o


def test_ambiguous_restore_failure_is_not_trusted_without_reading():
    dev = LandingRestore(mode="sell_power", power=3000.0, fail={"mode": DENIED})
    mem = ControlMemory.for_profile(GW)
    _, rep = tick(dev, mem, schedule(mode="idle"), 60.0)
    assert rep.restore_failed == ["power_w"] and dev.pair() == ("sell_power", 3000.0)
    assert "power_w" in mem.uncertain
    dev.fail = {}
    d, _ = tick(dev, mem, schedule(mode="idle"), 400.0, hide=("mode", "power_w"))
    assert [w.key for w in d.writes] == ["power_w", "mode"]
    assert dev.pair() == ("battery_standby", 0.0)


# ── Klucze niepewne po ERROR ──

class Scripted(Device):
    """Wynik zapisu z listy per klucz: 'ok', 'landed' (doszedł, ERROR), 'error', 'denied'."""

    def __init__(self, *a, script=None, **k):
        super().__init__(*a, **k)
        self.script = {key: list(v) for key, v in (script or {}).items()}

    def write(self, w):
        step = self.script.get(w.key, []).pop(0) if self.script.get(w.key) else "ok"
        if step == "error":
            return ERROR
        if step == "denied":
            return DENIED
        super().write(w)
        return ERROR if step == "landed" else OK


def test_stale_memory_never_restores_standby_with_power():
    # sprzedaż 3000 (nasz zapis) → postój: moc 0 dochodzi z ERROR, potem tryb dochodzi
    # z ERROR, a w cyklu bez odczytów tryb zostaje odrzucony.
    dev = Scripted(script={"power_w": ["ok", "landed"], "mode": ["ok", "landed", "denied"]})
    mem = ControlMemory.for_profile(GW)
    sell, idle = schedule(mode="discharge", discharge_purpose="sell", power_w=3000), schedule(mode="idle")
    tick(dev, mem, sell, 0.0)
    assert dev.pair() == ("sell_power", 3000.0)
    tick(dev, mem, idle, 60.0)
    assert dev.pair() == ("sell_power", 0.0) and "power_w" in mem.uncertain
    tick(dev, mem, idle, 120.0)
    assert dev.pair() == ("battery_standby", 0.0) and "mode" in mem.uncertain
    tick(dev, mem, idle, 180.0, hide=("mode", "power_w"))
    assert not _standby_charging(dev)


def test_reading_that_clears_doubt_is_the_previous_value():
    # Po odczycie 0 W stary zapis 3000 W nie może ustawić kolejności „moc najpierw".
    dev = Scripted(script={"power_w": ["ok", "landed"], "mode": ["ok", "ok", "denied"]})
    mem = ControlMemory.for_profile(GW)
    tick(dev, mem, schedule(mode="discharge", discharge_purpose="sell", power_w=3000), 0.0)
    tick(dev, mem, schedule(mode="idle"), 60.0)                    # moc 0 doszła z ERROR
    tick(dev, mem, schedule(mode="idle"), 120.0)                   # odczyt 0 W; tryb postoju OK
    assert dev.pair() == ("battery_standby", 0.0) and "power_w" not in mem.uncertain
    assert mem.last_written["power_w"] == 3000.0                   # zapis nasz, stan inny
    d, _ = tick(dev, mem, schedule(mode="discharge", discharge_purpose="sell", power_w=1000), 180.0,
                hide=("power_w",))
    assert [w.key for w in d.writes][-2:] == ["mode", "power_w"]  # moc rośnie z 0: tryb najpierw
    assert not _standby_charging(dev)


def test_landed_error_value_is_rewritten_without_reading():
    # Moc 3000 dochodzi z ERROR; plan wraca do 2000, odczytów brak → 2000 jedzie ponownie.
    dev = Scripted(script={"power_w": ["ok", "landed"]})
    mem = ControlMemory.for_profile(GW)
    tick(dev, mem, schedule(mode="discharge", discharge_purpose="sell", power_w=2000), 0.0)
    assert dev.pair() == ("sell_power", 2000.0)
    tick(dev, mem, schedule(mode="discharge", discharge_purpose="sell", power_w=3000), 60.0)
    assert dev.pair() == ("sell_power", 3000.0) and "power_w" in mem.uncertain
    d, _ = tick(dev, mem, schedule(mode="discharge", discharge_purpose="sell", power_w=2000), 120.0,
                hide=("mode", "power_w"))
    assert [w.key for w in d.writes] == ["power_w"] and dev.pair() == ("sell_power", 2000.0)
    assert "power_w" not in mem.uncertain


def test_uncertain_only_after_error_and_cleared_by_reading():
    dev = Scripted(script={"power_w": ["denied"]})
    mem = ControlMemory.for_profile(GW)
    tick(dev, mem, schedule(mode="discharge", discharge_purpose="sell", power_w=3000), 0.0)
    assert mem.uncertain == set()                                  # DENIED: na pewno nie doszło
    dev.script = {"power_w": ["error"]}
    tick(dev, mem, schedule(mode="discharge", discharge_purpose="sell", power_w=3000), 400.0,
         hide=("power_w",))
    assert "power_w" in mem.uncertain
    tick(dev, mem, schedule(mode="discharge", discharge_purpose="sell", power_w=3000), 410.0)
    assert "power_w" not in mem.uncertain                          # odczyt rozstrzyga


def test_failed_restore_keeps_write_interval():
    # moc 0 zapisana, tryb odrzucony, cofnięcie mocy ERROR — 5 s później plan się zmienia,
    # urządzenie czytelne: moc nie jedzie ponownie przed upływem interwału I-6.
    dev = Scripted(mode="sell_power", power=3000.0, script={"power_w": ["ok", "error"], "mode": ["denied"]})
    mem = ControlMemory.for_profile(GW)
    _, rep = tick(dev, mem, schedule(mode="idle"), 60.0)
    assert rep.restore_failed == ["power_w"] and rep.ambiguous == ["power_w"]
    assert "mode" not in rep.ambiguous                       # DENIED: na pewno nie doszło
    d, _ = tick(dev, mem, schedule(mode="discharge", discharge_purpose="sell", power_w=2000), 65.0)
    assert "power_w" not in [w.key for w in d.writes]


def test_ambiguous_mode_and_mode_restore_count_for_direction_budget():
    dev = Scripted(script={"mode": ["ok", "ok", "landed"], "power_w": ["ok", "denied"]})
    mem = ControlMemory.for_profile(GW)
    tick(dev, mem, schedule(mode="charge", charge_source="grid", power_w=1500), 0.0)
    # sprzedaż 3000: tryb najpierw (OK), moc odrzucona, cofnięcie trybu dochodzi z ERROR
    _, rep = tick(dev, mem, schedule(mode="discharge", discharge_purpose="sell", power_w=3000), 60.0)
    assert rep.restore_failed == ["mode"] and "mode" in rep.ambiguous
    assert len(mem.limiter._changes) == 2                          # tam i (być może) z powrotem


def test_async_twin_raise_is_ambiguous():
    ws = [W("power_w", {"v": 0}), W("mode", {"v": "battery_standby"})]
    write, calls = _writer({"mode": "raise"})

    async def awrite(w):
        return write(w)

    rep = asyncio.run(async_run_group_writes(ws, awrite, restore={"power_w": W("power_w", {"v": 3000})}))
    assert [c[0] for c in calls] == ["power_w", "mode"] and rep.restore_held == ["power_w"]
    assert rep.ambiguous == ["mode"]


def _three_changes_now_charging(mem):
    for i, direction in enumerate(["discharge", "charge", "discharge", "charge"]):
        mem.limiter.record(direction, 10.0 * i)             # 3 zmiany w oknie, bieżące: ładowanie
    assert len(mem.limiter._changes) == 3


def test_round_trip_outside_budget_writes_group_without_mode_restore():
    # Budżet mieści jedną zmianę, nie dwie: grupa idzie, ale bez cofnięcia trybu.
    dev = Device(mode="charge_battery", power=1500.0)
    mem = ControlMemory.for_profile(GW)
    _three_changes_now_charging(mem)
    d, _ = tick(dev, mem, schedule(mode="discharge", discharge_purpose="sell", power_w=3000), 100.0)
    assert "I-8" not in d.notes and [w.key for w in d.writes] == ["mode", "power_w"]
    assert "mode" not in d.restore and "mode" not in d.restore_ambiguous_safe
    assert dev.pair() == ("sell_power", 3000.0) and len(mem.limiter._changes) == 4


def test_round_trip_outside_budget_second_member_refused_stays_reduced():
    # Moc odrzucona: bez cofnięcia trybu falownik sprzedaje na starej, mniejszej mocy
    # — bez drugiego przełączenia, budżet nie jest przekroczony.
    dev = Scripted(mode="charge_battery", power=1500.0, script={"power_w": ["denied"]})
    mem = ControlMemory.for_profile(GW)
    _three_changes_now_charging(mem)
    _, rep = tick(dev, mem, schedule(mode="discharge", discharge_purpose="sell", power_w=3000), 100.0)
    assert rep.restored == [] and rep.restore_failed == ["mode"]
    assert dev.pair() == ("sell_power", 1500.0) and len(mem.limiter._changes) == 4


def test_round_trip_outside_budget_with_unknown_power_is_held():
    dev = Device(mode="charge_battery", power=1500.0)
    mem = ControlMemory.for_profile(GW)
    _three_changes_now_charging(mem)
    d, _ = tick(dev, mem, schedule(mode="discharge", discharge_purpose="sell", power_w=3000), 100.0,
                hide=("power_w",))
    assert "I-8" in d.notes and dev.pair() == ("charge_battery", 1500.0)


def test_budget_of_one_reaches_direction_change_in_one_tick():
    from custom_components.volcast.core.guard_state import DirectionLimiter
    dev = Device(mode="charge_battery", power=1000.0)
    mem = ControlMemory.for_profile(GW)
    mem.limiter = DirectionLimiter(1)
    tick(dev, mem, schedule(mode="charge", charge_source="grid", power_w=1000), 0.0)
    mem.limiter.record("charge", 0.0)                        # bieżące: ładowanie, okno puste
    tick(dev, mem, schedule(mode="discharge", discharge_purpose="sell", power_w=2000), 60.0)
    assert dev.pair() == ("sell_power", 2000.0)


def test_four_mode_first_direction_changes_fit_in_an_hour():
    dev = Device(mode="charge_battery", power=500.0)
    mem = ControlMemory.for_profile(GW)
    tick(dev, mem, schedule(mode="charge", charge_source="grid", power_w=500), 0.0)
    mem.limiter.record("charge", 0.0)
    plans = [dict(mode="discharge", discharge_purpose="sell", power_w=1000),
             dict(mode="charge", charge_source="grid", power_w=1500),
             dict(mode="discharge", discharge_purpose="sell", power_w=2000),
             dict(mode="charge", charge_source="grid", power_w=2500)]
    for i, p in enumerate(plans):
        d, _ = tick(dev, mem, schedule(**p), 600.0 * (i + 1))      # co 10 min, rosnąca moc
        assert "I-8" not in d.notes and dev.pair()[1] == float(p["power_w"]), (i, d.notes, dev.pair())
    assert len(mem.limiter._changes) == 4


@pytest.mark.parametrize("seed", range(40))
def test_fuzz_landed_errors_keep_direction_budget(seed):
    rng = random.Random(9000 + seed)
    dev = Landing(rng=rng, fail_rate=0.1, landed_rate=0.2)
    mem = ControlMemory.for_profile(GW)
    t, sched, flips, last = 0.0, schedule(**SHAPES[0]), [], None
    for _ in range(200):
        t += rng.choice([5.0, 30.0, 60.0, 90.0])
        if rng.random() < 0.3:
            sched = schedule(**rng.choice(SHAPES))
        hide = ("mode", "power_w") if rng.random() < 0.3 else ()
        tick(dev, mem, sched, t, hide=hide)
        assert not _standby_charging(dev), (seed, t, dev.pair())
        direction = GW.modes[dev.state["mode"]].direction
        if direction in ("charge", "discharge"):
            if last is not None and direction != last:
                flips.append(t)
            last = direction
        assert sum(1 for x in flips if t - 3600.0 < x <= t) <= 4, (seed, t, flips)


def test_neutral_round_trip_leaves_direction_unknown():
    # auto 1000 W → ładowanie 3000: tryb najpierw, moc odrzucona, powrót do auto.
    # Falownik przez chwilę ładował, a tryb neutralny sam potrafi rozładowywać — ostatni
    # kierunek jest więc nieznany; liczymy zachowawczo następną zmianę w każdą stronę.
    dev = Scripted(mode="auto", power=1000.0, script={"power_w": ["denied"]})
    mem = ControlMemory.for_profile(GW)
    mem.limiter.record("discharge", 0.0)
    _, rep = tick(dev, mem, schedule(mode="charge", charge_source="grid", power_w=3000), 60.0)
    assert rep.restored == ["mode"] and dev.pair() == ("auto", 1000.0)
    changes = len(mem.limiter._changes)
    mem.limiter.record("charge", 70.0)                       # prawdziwa zmiana z rozładowania
    assert len(mem.limiter._changes) == changes + 1


def test_ambiguous_mode_write_leaves_direction_unknown():
    dev = Scripted(script={"mode": ["ok", "error"]})
    mem = ControlMemory.for_profile(GW)
    tick(dev, mem, schedule(mode="discharge", discharge_purpose="sell", power_w=2000), 0.0)
    _, rep = tick(dev, mem, schedule(mode="charge", charge_source="grid", power_w=3000), 60.0)
    assert rep.ambiguous == ["mode"] and dev.state["mode"] == "sell_power"
    changes = len(mem.limiter._changes)
    mem.limiter.record("charge", 70.0)                       # mogło już być ładowanie — liczy się
    assert len(mem.limiter._changes) == changes + 1
