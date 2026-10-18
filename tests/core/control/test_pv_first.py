"""PV przed siecią w slocie `charge_grid`: czysta logika fazy (`core/control/pv_first.py`)."""
from __future__ import annotations

import copy
import math
from datetime import datetime, timezone

import pytest

from custom_components.volcast.core.control import pv_first as pf
from custom_components.volcast.core.control.pv_first import PvFirstMemory, applies, step
from custom_components.volcast.core.profile import load_builtin, profile_from_dict

T0 = datetime(2026, 10, 18, 11, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 10, 18, 13, 0, tzinfo=timezone.utc)
KEY = (T0, T1, "charge_grid")
NEXT_KEY = (T1, T2, "charge_grid")
P = 950.0
HOUR = 3600.0
TICK = 60.0


def run(ticks, *, memory=None, key=KEY, power_w=P, duration_s=HOUR, start=1000.0):
    """Ticki co 60 s: każdy element to (pv, load, battery) albo słownik z polem `t`.

    Domyślna pamięć to cykl działający przed slotem (poprzedni tick 60 s przed `start`, inny
    klucz). Zwraca listę wyników `step` po kolei.
    """
    mem = PvFirstMemory(last_mono=start - TICK) if memory is None else memory
    out = []
    now = start
    for item in ticks:
        if isinstance(item, dict):
            now = item.get("t", now)
            pv, load, bat = item.get("pv"), item.get("load"), item.get("battery")
        else:
            pv, load, bat = item
        res = step(memory=mem, key=key, power_w=power_w, duration_s=duration_s,
                   pv_w=pv, load_w=load, battery_w=bat, now_mono=now)
        out.append(res)
        mem = res.memory
        now += TICK
    return out


def at(t, tick):
    return dict(t=t, **dict(zip(("pv", "load", "battery"), tick)))


HIGH = (3500.0, 500.0, -950.0)       # nadwyżka 3000 W ≥ moc slotu
LOW = (1000.0, 500.0, -950.0)        # nadwyżka 500 W < P − 200
DEAD = (1300.0, 500.0, -950.0)       # nadwyżka 800 W: między P − 200 a P
NONE = (None, None, -950.0)          # nieważny odczyt PV/poboru


def phases(results):
    return [r.phase for r in results]


def enter_pv():
    """Ticki doprowadzające do fazy `pv`: trzeci tick pokrywa 180 s nadwyżki."""
    return [HIGH] * 3


# ── Wejście ──

def test_enters_pv_after_3_ticks_of_surplus_not_after_2():
    res = run([HIGH] * 3)
    assert phases(res) == ["grid", "grid", "pv"]


def test_without_a_previous_tick_entry_needs_a_fourth_tick():
    # np. po restarcie HA: pierwszy odczyt nie ma przedziału, który by pokrywał
    res = run([HIGH] * 4, memory=PvFirstMemory())
    assert phases(res) == ["grid", "grid", "grid", "pv"]


def test_entry_timer_restarts_when_surplus_dips_below_slot_power():
    res = run([HIGH, HIGH, DEAD, HIGH, HIGH, HIGH])
    assert phases(res) == ["grid"] * 5 + ["pv"]


def test_surplus_exactly_at_slot_power_counts_as_enough():
    exact = (1450.0, 500.0, -950.0)
    assert phases(run([exact] * 3))[-1] == "pv"


# ── Wyjście ──

def test_drop_shorter_than_5_min_keeps_pv_and_5_low_ticks_return_to_grid():
    res = run(enter_pv() + [LOW] * 5)
    assert phases(res)[2:] == ["pv", "pv", "pv", "pv", "pv", "grid"]


def test_dead_zone_between_power_minus_margin_and_power_never_returns_to_grid():
    res = run(enter_pv() + [DEAD] * 10)
    assert set(phases(res)[2:]) == {"pv"}


def test_dead_zone_resets_the_exit_timer():
    res = run(enter_pv() + [LOW] * 4 + [DEAD] + [LOW] * 4)
    assert set(phases(res)[2:]) == {"pv"}


def test_surplus_exactly_power_minus_margin_is_not_below():
    edge = (1250.0, 500.0, -950.0)                      # 750 W = P − 200
    assert set(phases(run(enter_pv() + [edge] * 8))[2:]) == {"pv"}
    under = (1249.0, 500.0, -950.0)                     # 749 W
    assert phases(run(enter_pv() + [under] * 5))[-1] == "grid"


# ── Cel energii ──

def test_energy_target_reached_keeps_pv_despite_surplus_drop():
    # slot 3 min × 950 W = 47,5 Wh; bateria ładuje 3000 W → 50 Wh po minucie
    strong = (3500.0, 500.0, -3000.0)
    res = run([strong] * 3 + [LOW] * 10, duration_s=180.0)
    assert set(phases(res)[2:]) == {"pv"}
    assert pf.NOTE_TARGET in res[-1].notes
    assert res[-1].memory.charged_wh >= 47.5


def test_energy_target_reached_keeps_pv_despite_invalid_readings():
    strong = (3500.0, 500.0, -3000.0)
    res = run([strong] * 3 + [NONE] * 3, duration_s=180.0)
    assert set(phases(res)[2:]) == {"pv"}
    assert all(pf.NOTE_TARGET in r.notes for r in res[3:])


def test_target_crossed_in_grid_after_a_pv_phase_returns_to_pv_until_slot_end():
    # PV 15 ticków po 2 kW, chmury → powrót do sieci; zakup z sieci dobija do celu 950 Wh
    sunny = (3500.0, 500.0, -2000.0)
    res = run([sunny] * 15 + [LOW] * 40)
    crossed = next(i for i, r in enumerate(res) if r.memory.charged_wh >= 950.0)
    assert "grid" in phases(res)[15:crossed]
    assert set(phases(res)[crossed:]) == {"pv"}
    assert all(pf.NOTE_TARGET in r.notes for r in res[crossed:])


def test_pure_grid_slot_never_leaves_grid_even_when_meter_reads_above_setpoint():
    # bateria mierzy 5 % ponad nastawę — cel „osiągnięty” przed końcem slotu, ale bez fazy pv
    res = run([(1000.0, 500.0, -1000.0)] * 60)
    assert res[-1].memory.charged_wh >= 950.0
    assert set(phases(res)) == {"grid"}
    assert all(pf.NOTE_TARGET not in r.notes for r in res)


def test_energy_target_counts_only_charging_power():
    # rozładowanie (dodatnia moc) nie odejmuje i nie dodaje energii
    res = run([(3500.0, 500.0, 2000.0)] * 3 + [(3500.0, 500.0, -600.0)] * 2)
    assert res[-1].memory.charged_wh == pytest.approx(20.0)        # 2 × 60 s × 600 W


def test_energy_is_integrated_between_ticks_but_not_on_the_first_tick_of_a_slot():
    res = run([HIGH] * 3)
    assert res[0].memory.charged_wh == 0.0
    assert res[-1].memory.charged_wh == pytest.approx(2 * 950.0 / 60.0)


def test_gap_longer_than_150_s_is_not_integrated():
    res = run([at(0.0, HIGH), at(200.0, HIGH), at(260.0, HIGH)], start=0.0)
    assert res[1].memory.charged_wh == 0.0
    assert res[2].memory.charged_wh == pytest.approx(950.0 / 60.0)


def test_gap_longer_than_150_s_restarts_the_entry_timer():
    # odczyt po luce nie pokrywa luki: zegar od 200 s, wejście dopiero przy 380 s
    res = run([at(t, HIGH) for t in (0.0, 200.0, 260.0, 320.0, 380.0)], start=0.0)
    assert phases(res) == ["grid"] * 4 + ["pv"]


def test_missing_battery_reading_disables_target_with_note_but_not_phase_logic():
    no_bat = (3500.0, 500.0, None)
    res = run([no_bat] * 3 + [(1000.0, 500.0, None)] * 5, duration_s=60.0)
    assert phases(res) == ["grid"] * 2 + ["pv"] * 5 + ["grid"]
    assert all(pf.NOTE_NO_BATTERY in r.notes for r in res)
    assert all(pf.NOTE_TARGET not in r.notes for r in res)
    assert res[-1].memory.charged_wh == 0.0


@pytest.mark.parametrize("battery", [math.nan, -math.inf, -100_000.0, -60_000.0, 60_000.0])
def test_implausible_battery_reading_counts_as_missing(battery):
    res = run([HIGH, (3500.0, 500.0, battery)])
    assert res[-1].memory.charged_wh == 0.0 and pf.NOTE_NO_BATTERY in res[-1].notes


def test_large_but_plausible_battery_reading_is_integrated():
    res = run([HIGH, (3500.0, 500.0, -20_000.0)])
    assert res[-1].memory.charged_wh == pytest.approx(20_000.0 / 60.0)


# ── Nieważny odczyt PV / poboru ──

def test_single_invalid_reading_holds_pv_phase():
    res = run(enter_pv() + [NONE, HIGH])
    assert phases(res)[2:] == ["pv", "pv", "pv"]
    assert pf.NOTE_HELD in res[3].notes


def test_two_invalid_readings_in_a_row_fall_back_to_grid():
    res = run(enter_pv() + [NONE, NONE])
    assert phases(res)[2:] == ["pv", "pv", "grid"]
    assert pf.NOTE_NO_READING in res[4].notes


@pytest.mark.parametrize("bad", [
    (math.nan, 500.0, -950.0), (3500.0, math.nan, -950.0),
    (math.inf, 500.0, -950.0), (3500.0, -math.inf, -950.0),
])
def test_non_finite_pv_or_load_counts_as_invalid_reading(bad):
    res = run(enter_pv() + [bad, bad])
    assert phases(res)[2:] == ["pv", "pv", "grid"]
    assert pf.NOTE_HELD in res[3].notes and pf.NOTE_NO_READING in res[4].notes


def test_invalid_reading_after_a_long_silence_falls_back_to_grid():
    res = run([at(t, HIGH) for t in (0.0, 60.0, 120.0)] + [at(400.0, NONE)], start=0.0)
    assert phases(res) == ["grid", "grid", "pv", "grid"]


def test_invalid_reading_restarts_the_entry_timer():
    res = run([HIGH, HIGH, NONE, HIGH, HIGH, HIGH])
    assert phases(res) == ["grid"] * 5 + ["pv"]


def test_first_tick_without_reading_stays_on_plan():
    res = run([NONE], memory=PvFirstMemory())
    assert phases(res) == ["grid"] and pf.NOTE_NO_READING in res[0].notes


# ── Zegar ──

def test_clock_going_backwards_restarts_the_exit_timer_without_corrupting_energy():
    ticks = ([at(t, HIGH) for t in (1000.0, 1060.0, 1120.0)] + [at(1180.0, LOW)]
             + [at(t, LOW) for t in (200.0, 260.0, 320.0, 380.0, 440.0, 500.0)])
    res = run(ticks)
    assert phases(res)[2:] == ["pv"] * 7 + ["grid"]
    assert res[4].memory.charged_wh == res[3].memory.charged_wh
    assert all(r.memory.charged_wh >= 0.0 for r in res)


def test_clock_going_backwards_restarts_the_entry_timer():
    res = run([at(1000.0, HIGH), at(1060.0, HIGH), at(500.0, HIGH), at(560.0, HIGH),
               at(620.0, HIGH), at(680.0, HIGH)])
    assert phases(res) == ["grid"] * 5 + ["pv"]


# ── Klucz slotu ──

def test_new_slot_key_starts_from_fresh_memory():
    first = run(enter_pv())
    mem = first[-1].memory
    assert mem.phase == "pv" and mem.pv_seen and mem.charged_wh > 0.0
    res = run([HIGH], memory=mem, key=NEXT_KEY, start=1180.0)
    assert res[0].phase == "grid"
    assert res[0].memory.key == NEXT_KEY and res[0].memory.charged_wh == 0.0
    assert not res[0].memory.pv_seen


# ── Nota ──

def test_main_note_carries_surplus_slot_charged_energy_and_phase():
    res = run(enter_pv())
    assert res[-1].notes[0] == "pv_first:surplus=3000,slot=950,charged_wh=32,phase=pv"
    assert run([NONE])[0].notes[0].startswith("pv_first:surplus=-,slot=950,")


def test_invalid_slot_power_stays_on_plan():
    res = run([HIGH] * 5, power_w=0.0)
    assert set(phases(res)) == {"grid"}


# ── Profile ──

GW = load_builtin("goodwe-et")


def _with_intents(**changes):
    raw = copy.deepcopy(dict(GW.raw))
    intents = dict(raw["intents"])
    intents.update(changes)
    return profile_from_dict({**raw, "intents": intents}, origin="package")


def test_applies_to_goodwe_et_grid_charge():
    assert applies(GW, "charge_grid") is True


@pytest.mark.parametrize("intent", ["charge_pv", "sell", "discharge_forced", "self_consume", "standby"])
def test_does_not_apply_to_other_intents(intent):
    assert applies(GW, intent) is False


def test_does_not_apply_when_grid_charge_has_no_slot_power():
    prof = _with_intents(charge_grid={"mode": "charge_battery", "power": "none"})
    assert applies(prof, "charge_grid") is False


class _FakeProfile:
    """Walidator wymaga dziś wszystkich intencji w `mode_setpoint` — brak `charge_pv` tylko na atrapie."""
    control_model = "mode_setpoint"

    def __init__(self, kinds):
        self._kinds = kinds

    def power_kind(self, name):
        return self._kinds.get(name)


def test_does_not_apply_without_a_pv_charge_intent():
    assert applies(_FakeProfile({"charge_grid": "slot"}), "charge_grid") is False
    assert applies(_FakeProfile({"charge_grid": "slot", "charge_pv": "none"}), "charge_grid") is True


def test_does_not_apply_to_builtin_profiles_without_slot_power():
    assert applies(load_builtin("huawei-sun2000"), "charge_grid") is False


def test_does_not_apply_to_time_window_profiles():
    assert applies(load_builtin("deye-sg"), "charge_grid") is False
