"""Sprzedaż w trybie bezpośrednim: nastawa eksportu liczona na żywo z odczytu rejestrów.

Moc slotu sprzedaży to moc BATERII; tryb `sell_power` czyta nastawę 47512 jako eksport do
sieci ponad pokrycie domu. Cykl na celu rejestrowym liczy więc nastawę jak implementacja
referencyjna: `bateria + PV − dom`, przycięte do `[0, min(moc znamionowa, limit eksportu)]`,
wynik ujemny = 0 W przy zachowanym trybie sprzedaży (bateria kryje sam dom). Odczyt PV albo
poboru brakujący, stary albo niewiarygodny = slot w trybie neutralnym (bez zgadywania).
Strefa martwa: histereza 150 W względem ostatnio ZAPISANEJ nastawy i szczyt poboru netto
z ostatnich `DIRECT_PEAK_WINDOW_S` — spadek eksportu idzie od razu, wzrost po oknie.
"""
from __future__ import annotations

import random
from datetime import timedelta

import pytest

from custom_components.volcast.core.control.cycle import (
    WRITE, ControlMemory, Gates, Limits, Telemetry, commit, decide_cycle)
from custom_components.volcast.core.control.live_export import (
    DIRECT_PEAK_WINDOW_S, NOTE_SELL_NO_RATED, NOTE_SELL_NO_READING)
from custom_components.volcast.core.control.target import RegisterTarget
from custom_components.volcast.core.engines.sell_xset import SELL_XSET_HYSTERESIS_W
from custom_components.volcast.core.slot import parse_schedule
from custom_components.volcast.core.write_sequence import WriteReport
from tests.core.golden import T0

from .conftest import MODE_REG, POWER_REG, SOC_REG, goodwe_reading

RATED = 8000.0
EXP_EN_REG, EXP_W_REG = 47509, 47510


def _schedule(power_w: float, **kw):
    iso = lambda t: t.isoformat().replace("+00:00", "Z")  # noqa: E731
    slot = {"from": iso(T0 - timedelta(minutes=30)), "to": iso(T0 + timedelta(hours=2)),
            "price_pln_kwh": 1.2, "mode": "discharge", "discharge_purpose": "sell",
            "power_w": power_w, "soc_target": 10, "export_allowed": True, "export_limit_w": 8000, **kw}
    return parse_schedule({"schedule_id": "s-sell", "slots": [slot],
                           "fallback": {"mode": "self_consume", "soc_reserve": 8},
                           "control_enabled": True})


def _tele(pv, load, *, pv_age=5.0, load_age=5.0):
    return Telemetry(soc=80.0, soc_age_s=5.0, battery_temp_c=25.0,
                     pv_power_w=pv, pv_age_s=None if pv is None else pv_age,
                     load_power_w=load, load_age_s=None if load is None else load_age)


def _regs(profile):
    return {MODE_REG: profile.mode_value("sell_power"), POWER_REG: 1000, SOC_REG: 80,
            EXP_EN_REG: 1, EXP_W_REG: 8000, 45356: 10}


def _cycle(profile, *, power_w=3000.0, pv=0.0, load=500.0, memory=None, regs=None, now_mono=1000.0,
           rated=RATED, pv_age=5.0, load_age=5.0, minute=0, **slot_kw):
    memory = memory if memory is not None else ControlMemory.for_profile(profile)
    regs = regs if regs is not None else _regs(profile)
    reading = goodwe_reading(profile, **{str(a): v for a, v in regs.items()})
    d = decide_cycle(profile=profile, schedule=_schedule(power_w, **slot_kw),
                     now_utc=T0 + timedelta(minutes=minute), now_mono=now_mono,
                     tele=_tele(pv, load, pv_age=pv_age, load_age=load_age),
                     limits=Limits(rated_power_w=rated),
                     gates=Gates(consent=True, local_switch=True, control_mode="direct", verified=True),
                     memory=memory, target=RegisterTarget(reading))
    return d, memory


def _power(d):
    return next((w.value for w in d.writes if w.key == "power_w"), None)


# ── formuła i pułap ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("battery,pv,load,limit,expected", [
    (3000.0, 200.0, 700.0, 8000, 2500.0),     # dom > PV: bateria kryje różnicę, eksport mniejszy
    (3000.0, 1500.0, 500.0, 8000, 4000.0),    # dom < PV: nadwyżka PV dochodzi do eksportu
    (3893.0, 0.0, 500.0, 8000, 3393.0),       # noc: wektor złoty parytetu
    (500.0, 0.0, 1200.0, 8000, 0.0),          # wynik ujemny → 0 W, tryb sprzedaży zostaje
    (5000.0, 4000.0, 300.0, 8000, 8000.0),    # ponad moc znamionową → pułap 8000 W
    (5000.0, 0.0, 300.0, 3000, 3000.0),       # limit eksportu kontraktu niższy niż znamionowa
])
def test_sell_setpoint_formula_and_clamp(goodwe_profile, battery, pv, load, limit, expected):
    d, _ = _cycle(goodwe_profile, power_w=battery, pv=pv, load=load, export_limit_w=limit)
    assert d.status == WRITE
    assert d.flat["mode"] == "sell_power"
    assert d.flat["power_w"] == pytest.approx(expected)
    assert _power(d) == round(expected)
    assert d.live_export is not None and d.live_export.xset_w == pytest.approx(expected)
    assert any(n.startswith("sell_xset:") for n in d.notes)


def test_negative_result_keeps_sell_mode_with_zero_setpoint(goodwe_profile):
    # Jak referencja: Xset 0, tryb SELL_POWER zostaje — bateria kryje sam dom, eksport 0.
    d, _ = _cycle(goodwe_profile, power_w=699.0, pv=0.0, load=1200.0)
    assert d.flat["mode"] == "sell_power" and d.flat["power_w"] == 0.0
    assert "degraded" not in d.notes


def test_owner_limiter_on_the_inverter_caps_export_when_the_plan_is_silent(goodwe_profile):
    regs = {**_regs(goodwe_profile), EXP_EN_REG: 1, EXP_W_REG: 2000}
    d, _ = _cycle(goodwe_profile, power_w=4000.0, pv=0.0, load=300.0, regs=regs,
                  export_allowed=None, export_limit_w=None)
    assert d.flat["power_w"] == pytest.approx(2000.0)


# ── odczyt brakujący, stary, niewiarygodny → tryb neutralny ─────────────────────────────────

@pytest.mark.parametrize("kw", [
    dict(load=None),                               # brak poboru (np. brak rejestru mocy baterii)
    dict(pv=None),                                 # brak PV
    dict(load_age=301.0),                          # pobór starszy niż max_state_age_s (300 s)
    dict(pv_age=301.0),
    dict(load=-50.0),                              # pobór liczony ujemny — niewiarygodny
    dict(pv=2 * RATED + 1.0),                      # PV ponad 2 × moc znamionowa
    dict(load=float("nan")),
])
def test_missing_stale_or_implausible_reading_degrades_to_neutral(goodwe_profile, kw):
    d, memory = _cycle(goodwe_profile, **kw)
    assert d.flat["mode"] == goodwe_profile.neutral_mode
    assert "power_w" not in d.flat and _power(d) is None
    assert "degraded" in d.notes and NOTE_SELL_NO_READING in d.notes
    assert d.live_export is not None and d.live_export.no_reading and d.live_export.degraded
    assert memory.live_export.written_w is None


def test_unknown_rated_power_degrades_to_neutral(goodwe_profile):
    d, _ = _cycle(goodwe_profile, rated=0.0)
    assert d.flat["mode"] == goodwe_profile.neutral_mode and "power_w" not in d.flat
    assert NOTE_SELL_NO_RATED in d.notes and d.live_export.degraded


def test_reading_returns_selling_resumes(goodwe_profile):
    memory = ControlMemory.for_profile(goodwe_profile)
    bad, _ = _cycle(goodwe_profile, load=None, memory=memory)
    assert bad.flat["mode"] == goodwe_profile.neutral_mode
    good, _ = _cycle(goodwe_profile, memory=memory, now_mono=1060.0, minute=1)
    assert good.flat["mode"] == "sell_power" and good.flat["power_w"] == pytest.approx(2500.0)


# ── strefa martwa (NVM) ─────────────────────────────────────────────────────────────────────

def _apply(regs, d):
    for w in d.writes:
        regs[w.addr] = w.value


def _run_hour(profile, loads, *, power_w=3000.0, pv=0.0):
    """Godzina cykli co 60 s z zapisem udanym (pamięć jak po prawdziwym zapisie)."""
    memory = ControlMemory.for_profile(profile)
    regs = _regs(profile)
    power_writes, xsets = 0, []
    for i, load in enumerate(loads):
        now = 1000.0 + 60.0 * i
        d, _ = _cycle(profile, power_w=power_w, pv=pv, load=load, memory=memory, regs=regs,
                      now_mono=now, minute=i)
        assert "nvm_budget" not in d.notes
        if d.status == WRITE:
            commit(d, WriteReport(written=[w.key for w in d.writes]), memory, now)
            _apply(regs, d)
            power_writes += sum(1 for w in d.writes if w.key == "power_w")
        xsets.append(float(regs[POWER_REG]))
    return power_writes, xsets


def _hourly_share(profile) -> float:
    budget = profile.raw["write_policy"]["nvm_budget"]
    return budget["per_key"] / budget["window_h"]           # 144 / 24 h = 6 zapisów na godzinę


@pytest.mark.parametrize("seed", range(10))
def test_fluctuating_house_load_stays_within_the_hourly_nvm_share(goodwe_profile, seed):
    # Dom 800 W ± 300 W co minutę przez godzinę sprzedaży: 47512 nie częściej niż średnio
    # pozwala budżet NVM (per_key na dobę), a bateria nigdy nie oddaje więcej niż plan + histereza.
    rng = random.Random(seed)
    loads = [800.0 + rng.uniform(-300.0, 300.0) for _ in range(60)]
    writes, xsets = _run_hour(goodwe_profile, loads)
    assert writes <= _hourly_share(goodwe_profile)
    for xset, load in zip(xsets, loads):
        assert xset + load <= 3000.0 + SELL_XSET_HYSTERESIS_W   # moc baterii = eksport + dom − PV


def test_alternating_load_writes_once(goodwe_profile):
    loads = [800.0 + (300.0 if i % 2 else -300.0) for i in range(60)]
    writes, _ = _run_hour(goodwe_profile, loads)
    assert writes <= 2


def test_large_load_rise_lowers_export_on_the_next_cycle(goodwe_profile):
    loads = [500.0] * 10 + [2500.0] * 5
    _, xsets = _run_hour(goodwe_profile, loads)
    assert xsets[9] == 2500.0
    assert xsets[10] == 500.0                     # dom +2 kW → eksport −2 kW od razu


def test_large_load_drop_raises_export_after_the_peak_window(goodwe_profile):
    window_cycles = int(DIRECT_PEAK_WINDOW_S // 60)
    loads = [2500.0] * 5 + [500.0] * (window_cycles + 5)
    _, xsets = _run_hour(goodwe_profile, loads)
    assert xsets[4] == 500.0
    assert xsets[5] == 500.0                      # szczyt z okna trzyma eksport nisko
    assert xsets[4 + window_cycles + 1] == 2500.0  # po oknie wzrost dochodzi


# ── restart ─────────────────────────────────────────────────────────────────────────────────

def test_restart_does_not_reuse_the_setpoint_held_before(goodwe_profile):
    memory = ControlMemory.for_profile(goodwe_profile)
    regs = _regs(goodwe_profile)
    d, _ = _cycle(goodwe_profile, load=500.0, memory=memory, regs=regs)
    commit(d, WriteReport(written=[w.key for w in d.writes]), memory, 1000.0)
    _apply(regs, d)
    assert regs[POWER_REG] == 2500
    # Ta sama pamięć: zmiana 100 W mieści się w histerezie — bez zapisu.
    same, _ = _cycle(goodwe_profile, load=600.0, memory=memory, regs=regs, now_mono=1060.0, minute=1)
    assert _power(same) is None
    # Restart: pusta pamięć (nic z niej nie jest trwałe) — nastawa z bieżącego odczytu, nie stara.
    fresh, mem2 = _cycle(goodwe_profile, load=600.0, regs=regs, now_mono=5.0, minute=1)
    assert fresh.flat["power_w"] == pytest.approx(2400.0) and _power(fresh) == 2400
    assert mem2.live_export.written_w is None and mem2.live_export.net_samples == ((5.0, 600.0),)


def test_dry_run_keeps_no_written_setpoint(goodwe_profile):
    memory = ControlMemory.for_profile(goodwe_profile)
    d = decide_cycle(profile=goodwe_profile, schedule=_schedule(3000.0), now_utc=T0, now_mono=1000.0,
                     tele=_tele(0.0, 500.0), limits=Limits(rated_power_w=RATED),
                     gates=Gates(consent=False, local_switch=True, control_mode="direct", verified=True),
                     memory=memory, target=RegisterTarget(goodwe_reading(
                         goodwe_profile, **{str(a): v for a, v in _regs(goodwe_profile).items()})))
    assert d.flat["power_w"] == pytest.approx(2500.0)
    assert memory.live_export.written_w is None
