"""PV przed siecią w cyklu: slot `charge_grid` przy żywej nadwyżce PV ≥ moc slotu idzie w trybie
ładowania z PV (`charge_pv` → `auto`, bez mocy), zamiast kupować z sieci stałą mocą slotu.

Czysta logika faz jest w `pv_first` (osobne testy); tu — wpięcie w `_decide`: walidacja odczytów
po stronie cyklu, podmiana trybu po strażnikach, pamięć w `ControlMemory`, noty i zachowanie
mechanizmów, których korekta nie zmienia (budżet NVM, przejęcie, pauza, próba na sucho).
"""
from dataclasses import replace
from datetime import timedelta

import pytest

from custom_components.volcast.control import executor as executor_mod
from custom_components.volcast.control.verification import window_schedule
from custom_components.volcast.core.control import cycle as cyc
from custom_components.volcast.core.control import pv_first as pvf
from custom_components.volcast.core.control.cycle import (BLOCKED, DRY_RUN, ERROR, IDLE, WRITE, ControlMemory,
                                                          EntityContext, Limits, Telemetry, commit,
                                                          decide_cycle)
from custom_components.volcast.core.control.pv_first import (NOTE_HELD, NOTE_NO_BATTERY, NOTE_PV_FIRST,
                                                             PHASE_GRID, PHASE_PV, PvFirstMemory)
from custom_components.volcast.core.control.target import RegisterTarget
from custom_components.volcast.core.write_sequence import WriteReport
from tests.core.golden import T0

from tests.control.test_executor_direct import GW_V, Harness, gw_target, issues  # noqa: F401 — fixture

from .conftest import LIVE_ZERO, MODE_REG, SOC_REG, goodwe_reading
from .test_cycle import ATTRS, GW, MAPPED, OPEN, SELL, UNITS, at, plan, slot

GRID = dict(mode="charge", charge_source="grid")
# Poprzedni slot (autokonsumpcja) i slot zakupu z sieci 950 W.
GRID_950 = plan(slot("09:00", "10:00", mode="self_consume"), slot("10:00", "11:00", **GRID, power_w=950))
SURPLUS = dict(pv=3500.0, load=500.0)          # nadwyżka 3000 W
SMALL = dict(pv=1000.0, load=500.0)            # nadwyżka 500 W < moc slotu
CLOUD = dict(pv=1100.0, load=500.0)            # nadwyżka 600 W < 950 − 200


class Sim:
    """Cykle co 60 s na jednym falowniku (encje): udany zapis zmienia odczyt urządzenia."""

    def __init__(self, schedule=GRID_950, *, start="09:59:00", device=None, memory=None, rated=8000.0,
                 max_charge=0.0, allowed=True, count_budget=False):
        self.schedule = schedule
        self.count_budget = count_budget
        self.max_charge = max_charge
        self.allowed = allowed
        self.now = at(start)
        self.mono = 1000.0
        self.mem = memory or ControlMemory.for_profile(GW)
        self.device = dict(device or {"mode": "auto", "power_w": 0.0})
        self.rated = rated

    def tick(self, *, pv=None, load=None, battery=None, pv_age=5.0, load_age=5.0, battery_age=5.0,
             gates=OPEN, soc_age=10.0, apply=True):
        d = decide_cycle(
            profile=GW, schedule=self.schedule, now_utc=self.now, now_mono=self.mono,
            tele=Telemetry(soc=60.0, soc_age_s=soc_age, battery_temp_c=25.0, pv_power_w=pv, pv_age_s=pv_age,
                           load_power_w=load, load_age_s=load_age, battery_w=battery,
                           battery_age_s=battery_age),
            limits=Limits(rated_power_w=self.rated, max_charge_w=self.max_charge),
            ents=EntityContext(domain="goodwe", mapped=MAPPED, units=UNITS, attrs=ATTRS,
                               readings=dict(self.device)),
            gates=gates, memory=self.mem, pv_first_allowed=self.allowed)
        if apply and d.status == WRITE:
            keys = [w.key for w in d.writes]
            commit(d, WriteReport(written=keys), self.mem, self.mono,
                   now_wall=self.now.timestamp() if self.count_budget else None)
            self.device.update({k: d.flat[k] for k in keys})
        self.now += timedelta(seconds=60)
        self.mono += 60.0
        return d

    def ticks(self, n, **kw):
        return [self.tick(**kw) for _ in range(n)]


def pv_notes(d):
    return [n for n in d.notes if n.startswith(NOTE_PV_FIRST + ":")]


def keys(d):
    return [w.key for w in d.writes]


def same_decision(a, b):
    return (a.status, a.reason, a.intent, a.flat, [w.data for w in a.writes], keys(a), a.notes,
            a.summary()) == (b.status, b.reason, b.intent, b.flat, [w.data for w in b.writes], keys(b),
                             b.notes, b.summary())


def exhaust(sim, key):
    for _ in range(sim.mem.budget.per_key):
        sim.mem.budget.note(key, sim.now.timestamp() - 60)


def enter_pv(sim, **kw):
    """Tick w poprzednim slocie, potem 3 ticki nadwyżki: trzeci przechodzi w fazę `pv`."""
    sim.tick(**SURPLUS, **kw)                                   # 09:59 — autokonsumpcja
    first, second, third = sim.ticks(3, **SURPLUS, **kw)        # 10:00, 10:01, 10:02
    return first, second, third


# ── Wejście w fazę `pv` ──

def test_surplus_above_slot_power_for_three_ticks_switches_to_auto_without_power():
    sim = Sim()
    first, second, third = enter_pv(sim)
    assert first.status == WRITE and first.intent == "charge_grid"
    assert first.flat["mode"] == "charge_battery" and first.flat["power_w"] == 950.0
    assert second.status == IDLE and second.intent == "charge_grid"       # zegar wejścia biegnie
    assert third.status == WRITE and third.intent == "charge_pv"
    assert keys(third) == ["mode"] and third.writes[0].data == {"option": "auto"}
    assert "power_w" not in third.flat
    assert pv_notes(third) == ["pv_first:surplus=3000,slot=950,charged_wh=0,phase=pv"]
    assert third.summary()["intent"] == "charge_pv" and NOTE_NO_BATTERY in third.notes
    assert sim.mem.pv_first.phase == PHASE_PV
    # W fazie `pv` nic więcej nie idzie: falownik ma już `auto`.
    fourth = sim.tick(**SURPLUS)
    assert (fourth.status, fourth.intent) == (IDLE, "charge_pv") and pv_notes(fourth)


def test_after_restart_without_a_previous_tick_entry_takes_the_fourth_tick():
    sim = Sim(start="10:00:00")
    decisions = sim.ticks(4, **SURPLUS)
    assert [d.intent for d in decisions] == ["charge_grid"] * 3 + ["charge_pv"]


def test_entry_timer_is_visible_in_notes_before_the_switch():
    # Epizod „nadwyżka idzie do sieci przy zakupie" musi być widoczny w telemetrii od pierwszego ticku.
    sim = Sim()
    first, second, _ = enter_pv(sim)
    assert pv_notes(first) == ["pv_first:surplus=3000,slot=950,charged_wh=0,phase=grid"]
    assert pv_notes(second) == ["pv_first:surplus=3000,slot=950,charged_wh=0,phase=grid"]


def test_direct_target_swaps_the_mode_register_plan_too(goodwe_profile, charge_schedule):
    reading = goodwe_reading(goodwe_profile, **{str(MODE_REG): 11, str(SOC_REG): 80})
    mem = ControlMemory.for_profile(goodwe_profile)
    gates = replace(OPEN, control_mode="direct")
    out = []
    for i in range(4):
        tele = Telemetry(soc=80.0, soc_age_s=5.0, battery_temp_c=25.0,
                         **{**LIVE_ZERO, "pv_power_w": 4500.0, "load_power_w": 500.0})
        out.append(decide_cycle(profile=goodwe_profile, schedule=charge_schedule,
                                now_utc=T0 + timedelta(seconds=60 * i), now_mono=1000.0 + 60 * i, tele=tele,
                                limits=Limits(rated_power_w=8000.0), gates=gates, memory=mem,
                                target=RegisterTarget(reading)))
    assert [d.intent for d in out] == ["charge_grid"] * 3 + ["charge_pv"]
    assert out[0].flat["mode"] == "charge_battery" and out[0].flat["power_w"] == 3000.0
    assert out[3].flat["mode"] == "auto" and "power_w" not in out[3].flat
    assert out[3].flat["soc_max"] == out[0].flat["soc_max"]          # reszta nastaw planu zostaje


# ── Nadwyżka poniżej mocy slotu: dokładnie jak dziś ──

def test_surplus_below_slot_power_gives_decisions_identical_to_no_readings():
    with_pv, baseline = Sim(), Sim()
    for _ in range(12):
        a = with_pv.tick(**SMALL)
        b = baseline.tick()
        assert same_decision(a, b), (a.notes, b.notes)
        assert not pv_notes(a)
    assert with_pv.mem.pv_first.phase == PHASE_GRID
    assert with_pv.device == baseline.device == {"mode": "charge_battery", "power_w": 950.0}


# ── Powrót do zakupu z sieci ──

def test_cloud_for_five_minutes_returns_to_charge_battery_with_slot_power():
    sim = Sim()
    enter_pv(sim)
    assert sim.device["mode"] == "auto"
    held = sim.ticks(4, **CLOUD)                     # 4 ticki pod progiem: faza `pv` trwa
    assert all(d.intent == "charge_pv" and not d.writes for d in held)
    back = sim.tick(**CLOUD)                         # piąty: powrót
    assert back.status == WRITE and back.intent == "charge_grid"
    assert back.flat["mode"] == "charge_battery" and back.flat["power_w"] == 950.0
    # Rejestr mocy nie zmienił się w `auto` — idzie sam tryb (moc już na falowniku).
    assert keys(back) == ["mode"] and sim.device == {"mode": "charge_battery", "power_w": 950.0}
    assert pv_notes(back) == ["pv_first:surplus=600,slot=950,charged_wh=0,phase=grid"]
    # Kolejny tick w fazie `grid` bez zegara wejścia — bez not.
    assert not pv_notes(sim.tick(**CLOUD))


def test_return_writes_power_too_when_the_device_lost_the_setpoint():
    sim = Sim()
    enter_pv(sim)
    sim.device["power_w"] = 0.0                      # falownik wyzerował nastawę w `auto`
    sim.ticks(4, **CLOUD)
    back = sim.tick(**CLOUD)
    assert back.status == WRITE and set(keys(back)) == {"mode", "power_w"}
    assert sim.device == {"mode": "charge_battery", "power_w": 950.0}


# ── Budżet NVM ──

def test_exhausted_mode_budget_keeps_todays_decision_with_the_device_in_auto():
    # Falownik w `auto` na starcie slotu, budżet trybu wyczerpany: jak dziś — wstrzymanie, bez zapisu
    # trybu (także w fazie `pv`: podmiana nie omija budżetu i nie zmienia decyzji).
    with_pv, baseline = Sim(start="10:00:00"), Sim(start="10:00:00")
    for sim in (with_pv, baseline):
        exhaust(sim, "mode")
    for i in range(5):
        a = with_pv.tick(**SURPLUS)
        b = baseline.tick()
        assert (a.status, a.reason, a.intent, a.flat, keys(a)) == (b.status, b.reason, b.intent, b.flat, keys(b))
        assert [n for n in a.notes if not n.startswith(NOTE_PV_FIRST)] == list(b.notes)
        assert "nvm_budget" in a.notes and "mode" not in keys(a)
    # Wejście wstrzymane już przez rezerwę planu (udział w budżecie) — moduł zostaje w `grid`.
    assert with_pv.mem.pv_first.phase == PHASE_GRID
    assert cyc.NOTE_PV_FIRST_SHARE in a.notes and with_pv.device["mode"] == "auto"


def test_exhausted_mode_budget_never_triggers_a_baseline_restore_from_charge_battery():
    # Falownik już ładuje z sieci mocą slotu; podmiana na `auto` przy wyczerpanym budżecie trybu
    # nie może wywołać powrotu do trybu bazowego poza budżetem — stan ustalony jak dziś.
    sim = Sim()
    sim.tick(**SURPLUS)                              # 09:59
    assert sim.tick(**SURPLUS).flat["mode"] == "charge_battery"     # 10:00 — zapis planu
    sim.tick(**SURPLUS)                              # 10:01
    exhaust(sim, "mode")
    third = sim.tick(**SURPLUS)                      # 10:02 — faza `pv`, ale budżet
    assert (third.status, third.reason, third.intent) == (IDLE, "nothing_to_write", "charge_grid")
    assert sim.mem.pv_first.phase == PHASE_GRID and cyc.NOTE_PV_FIRST_SHARE in third.notes
    assert sim.device["mode"] == "charge_battery"


def test_exhausted_mode_budget_while_in_pv_keeps_the_plan_as_the_last_line():
    # Już w `pv` (trwanie bez rezerwy), budżet trybu wyczerpany: plan bez podmiany, bez zapisu i bez powrotu
    # do trybu bazowego — falownik zostaje w neutralnym `auto`.
    sim = Sim()
    enter_pv(sim)
    exhaust(sim, "mode")
    d = sim.tick(**SURPLUS)
    assert (d.status, d.intent, d.writes) == (IDLE, "charge_grid", [])
    assert cyc.NOTE_PV_FIRST_BUDGET in d.notes and "nvm_budget" in d.notes
    assert sim.mem.pv_first.phase == PHASE_PV and sim.device["mode"] == "auto"


def test_exhausted_power_budget_still_allows_the_mode_only_switch():
    sim = Sim()
    sim.tick(**SURPLUS)
    sim.ticks(2, **SURPLUS)
    exhaust(sim, "power_w")
    d = sim.tick(**SURPLUS)
    assert (d.status, d.intent, keys(d)) == (WRITE, "charge_pv", ["mode"])
    assert sim.device["mode"] == "auto"


# ── Bez korekty ──

def test_sell_slot_is_never_corrected():
    sim = Sim(plan(slot("10:00", "11:00", **SELL, power_w=2000)), start="10:00:00")
    for d in sim.ticks(6, **SURPLUS):
        assert d.intent == "sell" and not pv_notes(d)
    assert sim.mem.pv_first.key is None and sim.mem.pv_first.phase == PHASE_GRID


def test_charge_pv_slot_is_never_corrected():
    sim = Sim(plan(slot("10:00", "11:00", mode="charge", charge_source="pv")), start="10:00:00")
    for d in sim.ticks(6, **SURPLUS):
        assert d.intent == "charge_pv" and not pv_notes(d)
    assert sim.mem.pv_first == PvFirstMemory(last_mono=sim.mono - 60.0)


class _FallbackGrid:
    """Plan, którego slot zapasowy to zakup z sieci (klucz zmienia się co tick)."""

    def __init__(self):
        self._real = plan(slot("10:00", "11:00", **GRID, power_w=950))
        self.fallback = self._real.fallback

    def effective_slot(self, moment):
        s = self._real.slots[0]
        return replace(s, start=moment, end=moment + timedelta(hours=1)), True


def test_fallback_slot_is_never_corrected():
    sim = Sim(_FallbackGrid(), start="10:00:00")
    for d in sim.ticks(6, **SURPLUS):
        assert d.intent == "charge_grid" and d.fallback and not pv_notes(d)
    assert sim.device["mode"] == "charge_battery" and sim.mem.pv_first.key is None


def test_takeover_blocks_without_correction():
    sim = Sim()
    enter_pv(sim)
    sim.device["mode"] = "export_ac"                 # ktoś inny ustawił tryb spoza profilu
    d = sim.tick(**SURPLUS)
    assert (d.status, d.reason, d.writes, d.takeover) == (BLOCKED, "foreign_mode", [], True)


def test_paused_is_dry_run_without_writes():
    sim = Sim()
    sim.mem.paused_until = 1e9
    decisions = [sim.tick(**SURPLUS) for _ in range(4)]
    assert all(d.status == DRY_RUN for d in decisions) and sim.device == {"mode": "auto", "power_w": 0.0}
    assert decisions[-1].intent == "charge_pv"       # diagnostyka pokazuje, co by poszło


# ── Wiarygodność mocy baterii po stronie cyklu ──

def _charged_after_two_ticks(**battery_kw):
    sim = Sim(start="10:00:00")
    sim.ticks(2, **SMALL, **battery_kw)
    return sim.mem.pv_first.charged_wh


def test_fresh_plausible_battery_is_integrated():
    assert _charged_after_two_ticks(battery=-900.0) == 15.0         # 900 W × 60 s


def test_stale_battery_is_passed_as_missing():
    assert _charged_after_two_ticks(battery=-900.0, battery_age=301.0) == 0.0
    assert _charged_after_two_ticks(battery=-900.0, battery_age=None) == 0.0


def test_battery_above_rated_power_is_passed_as_missing():
    assert _charged_after_two_ticks(battery=-8001.0) == 0.0
    assert _charged_after_two_ticks(battery=-7999.0) > 0.0


def test_implausible_battery_shows_the_no_battery_note_in_pv_phase():
    sim = Sim()
    *_, third = enter_pv(sim, battery=-9000.0)
    assert third.intent == "charge_pv" and NOTE_NO_BATTERY in third.notes
    sim2 = Sim()
    *_, third2 = enter_pv(sim2, battery=-900.0)
    assert NOTE_NO_BATTERY not in third2.notes


# ── Nieważne odczyty PV / poboru ──

def test_one_invalid_reading_holds_the_phase_two_return_to_the_plan():
    sim = Sim()
    enter_pv(sim)
    held = sim.tick(pv=None, load=500.0)
    assert held.intent == "charge_pv" and not held.writes and NOTE_HELD in held.notes
    back = sim.tick(pv=3500.0, load=500.0, pv_age=301.0)           # przestarzałe PV = nieważne
    assert back.status == WRITE and back.intent == "charge_grid" and back.flat["mode"] == "charge_battery"


def test_implausible_pv_is_invalid():
    sim = Sim()
    enter_pv(sim)
    sim.tick(pv=17000.0, load=500.0)                 # ponad 2 × moc znamionowa
    assert sim.tick(pv=17000.0, load=500.0).intent == "charge_grid"


# ── Kolejne sloty zakupu ──

def test_consecutive_grid_slots_restart_the_counters_at_the_boundary():
    sched = plan(slot("09:00", "10:00", mode="self_consume"), slot("10:00", "10:30", **GRID, power_w=950),
                 slot("10:30", "11:00", **GRID, power_w=950))
    sim = Sim(sched)
    enter_pv(sim)
    sim.now, sim.mono = at("10:29:00"), sim.mono + 26 * 60.0     # przeskok do 10:29 (luka w tickach)
    sim.tick(**SURPLUS)                              # 10:29 — po luce faza `pv` trwa
    assert sim.mem.pv_first.phase == PHASE_PV
    boundary = sim.tick(**SURPLUS)                   # 10:30 — nowy slot, liczniki od zera
    assert boundary.intent == "charge_grid" and boundary.flat["mode"] == "charge_battery"
    assert sim.mem.pv_first.key == (at("10:30:00"), at("11:00:00"), "charge_grid")
    again = sim.ticks(2, **SURPLUS)                  # 10:31, 10:32 — znów `pv` po 3 tickach
    assert [d.intent for d in again] == ["charge_grid", "charge_pv"]


# ── Pamięć na każdej ścieżce ──

def test_dry_run_updates_the_memory():
    sim = Sim()
    closed = replace(OPEN, consent=False)
    *_, third = enter_pv(sim, gates=closed)
    assert third.status == DRY_RUN and third.flat["mode"] == "auto"
    assert sim.mem.pv_first.phase == PHASE_PV and sim.device["mode"] == "auto"


def test_blocked_before_the_correction_keeps_the_memory():
    sim = Sim()
    enter_pv(sim)
    before = sim.mem.pv_first
    d = sim.tick(**SURPLUS, soc_age=400.0)           # I-9: SoC przestarzały
    assert d.status == BLOCKED and sim.mem.pv_first is before


def test_error_keeps_the_memory():
    sim = Sim()
    enter_pv(sim)
    before = sim.mem.pv_first
    d = decide_cycle(profile=GW, schedule=sim.schedule, now_utc=sim.now, now_mono=sim.mono,
                     tele=Telemetry(soc=60.0, soc_age_s=10.0, battery_temp_c=25.0), limits=Limits(8000.0),
                     gates=OPEN, memory=sim.mem)          # ani encji, ani celu
    assert d.status == ERROR and sim.mem.pv_first is before


def test_new_memory_starts_empty():
    assert ControlMemory.for_profile(GW).pv_first == PvFirstMemory()


# ── Okno próbne weryfikacji urządzenia ──

def _window_sims(allowed):
    window = window_schedule(at("10:00:00"), minutes=15, power_w=1000, base=None)
    return Sim(window, start="10:00:00", allowed=allowed), Sim(window, start="10:00:00", allowed=allowed)


def test_verification_window_is_never_corrected():
    # Okno mierzy wymuszone ładowanie z sieci (`charge_battery` + moc okna) — PV przed siecią by je zafałszowało.
    with_pv, baseline = _window_sims(allowed=False)
    for _ in range(8):
        a = with_pv.tick(**SURPLUS)
        b = baseline.tick()
        assert same_decision(a, b), (a.notes, b.notes)
        assert a.intent == "charge_grid" and not pv_notes(a)
    assert with_pv.device == {"mode": "charge_battery", "power_w": 1000.0}
    assert with_pv.mem.pv_first.key is None and with_pv.mem.pv_first.phase == PHASE_GRID


def test_same_window_without_the_flag_switches():
    sim, _ = _window_sims(allowed=True)
    assert [d.intent for d in sim.ticks(4, **SURPLUS)] == ["charge_grid"] * 3 + ["charge_pv"]


def test_the_flag_resets_the_memory_of_a_running_pv_phase():
    sim = Sim()
    enter_pv(sim)
    sim.allowed = False
    d = sim.tick(**SURPLUS)
    assert d.status == WRITE and d.flat == {"mode": "charge_battery", "power_w": 950.0}
    assert sim.mem.pv_first.key is None


@pytest.mark.asyncio
async def test_executor_disables_pv_first_only_while_a_verification_window_runs(
        make_hass, goodwe_udp_sim, goodwe_bank, issues, monkeypatch):  # noqa: F811
    seen = []
    real = executor_mod.decide_cycle

    def spy(**kw):
        seen.append(kw["pv_first_allowed"])
        return real(**kw)

    monkeypatch.setattr(executor_mod, "decide_cycle", spy)
    h = await Harness(make_hass, GW_V, gw_target(goodwe_udp_sim)).start()
    try:
        await h.ex.async_tick()
        h.ex.start_verification_window(window_schedule(h.utc(), minutes=15, power_w=500, base=h.ex.schedule))
        await h.ex.async_tick()
        assert seen[0] is True and seen[-1] is False
        await h.ex.async_verification_restore()
        await h.cycle()
        assert seen[-1] is True
    finally:
        await h.close()


# ── Błąd korekty nie zatrzymuje sterowania ──

def test_error_inside_pv_first_falls_back_to_the_plan(monkeypatch):
    def boom(**_):
        raise RuntimeError("x")

    with_pv, baseline = Sim(), Sim()
    enter_pv(with_pv)
    for _ in range(4):
        baseline.tick()
    b = baseline.tick()                               # dziś: plan (bez odczytów — bez korekty)
    monkeypatch.setattr(pvf, "step", boom)
    a = with_pv.tick(**SURPLUS)
    assert a.status == WRITE and a.flat == {"mode": "charge_battery", "power_w": 950.0}
    assert a.notes == (*b.notes, "pv_first_error:RuntimeError")
    assert with_pv.mem.pv_first == PvFirstMemory(last_mono=with_pv.mono - 60.0)
    c = with_pv.tick(**SURPLUS)
    assert (c.status, c.writes, c.intent) == (IDLE, [], "charge_grid")


# ── Korekta po strażnikach ──

def test_slot_whose_mode_changed_upstream_is_not_corrected(monkeypatch):
    # Coś przed korektą (strażnik) zmieniło tryb slotu zakupu: plan wykonawcy, nie podmiana na `auto`.
    real = cyc.apply_guards

    def standby(params, ctx, profile):
        out = real(params, ctx, profile)
        return replace(out, params=replace(out.params, mode="battery_standby"))

    monkeypatch.setattr(cyc, "apply_guards", standby)
    sim = Sim()
    *_, third = enter_pv(sim)
    fourth = sim.tick(**SURPLUS)
    for d in (third, fourth):
        assert d.intent == "charge_grid" and d.flat["mode"] == "battery_standby" and not pv_notes(d)
    assert sim.mem.pv_first.key is None


def test_threshold_is_the_power_after_the_charge_cap():
    # Limit ładowania BMS 600 W (I-3) — kupujemy najwyżej 600 W, więc nadwyżka 700 W już je pokrywa.
    sim = Sim(max_charge=600.0)
    small = dict(pv=1200.0, load=500.0)
    sim.tick(**small)
    first, _, third = sim.ticks(3, **small)
    assert first.flat["power_w"] == 600.0
    assert third.intent == "charge_pv"
    assert pv_notes(third) == ["pv_first:surplus=700,slot=600,charged_wh=0,phase=pv"]


# ── Udział w budżecie NVM trybu: wejście tylko z rezerwą dla planu ──

ON_PLAN = {"mode": "charge_battery", "power_w": 950.0}     # falownik już wykonuje slot zakupu


def _prefill(sim, **counts):
    for key, n in counts.items():
        for _ in range(n):
            sim.mem.budget.note(key, sim.now.timestamp() - 60)


def _share_sim(**counts):
    # Start w slocie bez poprzedniego ticku: wejście na czwartym ticku, bez zapisów przed nim.
    sim = Sim(start="10:00:00", device=ON_PLAN)
    _prefill(sim, **counts)
    return sim


def test_entry_allowed_with_room_for_entry_and_return_in_half_the_mode_budget():
    per_key = GW.nvm_budget.per_key
    sim = _share_sim(mode=per_key // 2 - 2)
    d = sim.ticks(4, **SURPLUS)[-1]
    assert d.status == WRITE and d.intent == "charge_pv" and cyc.NOTE_PV_FIRST_SHARE not in d.notes


def test_entry_refused_over_half_the_mode_budget_keeps_the_plan_and_a_consistent_memory():
    per_key = GW.nvm_budget.per_key
    sim = _share_sim(mode=per_key // 2 - 1)
    decisions = sim.ticks(10, **SURPLUS)
    refused = decisions[3:]
    for d in refused:
        assert (d.status, d.reason, d.intent, d.writes) == (IDLE, "nothing_to_write", "charge_grid", [])
        assert cyc.NOTE_PV_FIRST_SHARE in d.notes and d.flat == ON_PLAN
    # Moduł nie „wierzy”, że jest w `pv`: bez fazy, bez pv_seen; zegar wejścia trwa (wejście, gdy budżet pozwoli).
    mem = sim.mem.pv_first
    assert mem.phase == PHASE_GRID and mem.pv_seen is False and mem.above_since is not None
    assert sim.device == ON_PLAN


def test_entry_refused_over_half_the_total_budget():
    total = GW.nvm_budget.total
    ok = _share_sim(soc_min=140, soc_max=140, export_limit_w=total // 2 - 2 - 280)
    assert ok.ticks(4, **SURPLUS)[-1].intent == "charge_pv"
    no = _share_sim(soc_min=140, soc_max=140, export_limit_w=total // 2 - 1 - 280)
    d = no.ticks(4, **SURPLUS)[-1]
    assert d.intent == "charge_grid" and cyc.NOTE_PV_FIRST_SHARE in d.notes and not d.writes


def test_staying_in_pv_and_returning_ignore_the_share():
    per_key = GW.nvm_budget.per_key
    sim = Sim()
    enter_pv(sim)
    _prefill(sim, mode=per_key - 10)                 # ponad połowę, ale nie wyczerpany
    stay = sim.ticks(3, **SURPLUS)
    assert all(d.intent == "charge_pv" and cyc.NOTE_PV_FIRST_SHARE not in d.notes for d in stay)
    sim.ticks(4, **CLOUD)
    back = sim.tick(**CLOUD)
    assert back.status == WRITE and back.intent == "charge_grid" and sim.device == ON_PLAN


def test_target_reentry_is_an_entry_too():
    # Cel energii osiągnięty po fazie `pv` wraca do `pv` z fazy `grid` — to też zapis trybu, więc też rezerwa.
    per_key = GW.nvm_budget.per_key
    sim = Sim()
    enter_pv(sim, battery=-2000.0)                  # 2 kW: cel 950 Wh po ~29 tickach
    sim.ticks(4, **CLOUD, battery=-2000.0)
    assert sim.tick(**CLOUD, battery=-2000.0).intent == "charge_grid"
    _prefill(sim, mode=per_key // 2)
    decisions = []
    while sim.now < at("10:59:00"):
        decisions.append(sim.tick(**CLOUD, battery=-2000.0))
    assert all(d.intent == "charge_grid" and not d.writes for d in decisions)
    reached = [d for d in decisions if pvf.NOTE_TARGET in d.notes]
    assert len(reached) >= 3 and all(cyc.NOTE_PV_FIRST_SHARE in d.notes for d in reached)
    assert sim.mem.pv_first.phase == PHASE_GRID and sim.device == ON_PLAN


@pytest.mark.parametrize("slot_min", [15, 60])
def test_adversarial_clouds_for_six_hours_spend_at_most_half_the_mode_budget(slot_min):
    # Najgorsze chmury: słońce, gdy faza `grid`, chmura, gdy `pv` — para wejście/powrót co 8 min.
    per_key = GW.nvm_budget.per_key
    start = at("09:00:00")
    slots = [slot((start + timedelta(minutes=m)).strftime("%H:%M"),
                  (start + timedelta(minutes=m + slot_min)).strftime("%H:%M"), **GRID, power_w=950)
             for m in range(0, 360, slot_min)]
    sim = Sim(plan(*slots), start="09:00:00", count_budget=True)
    mode_writes = 0
    for _ in range(360):
        weather = SURPLUS if sim.mem.pv_first.phase == PHASE_GRID else CLOUD
        d = sim.tick(**weather)
        mode_writes += "mode" in keys(d)
    used = sim.mem.budget.counts(sim.now.timestamp())["mode"]
    assert used == mode_writes
    assert per_key // 2 - 4 <= mode_writes <= per_key // 2          # pv-first się zatrzymał na połowie budżetu
