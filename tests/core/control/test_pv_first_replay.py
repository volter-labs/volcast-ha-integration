"""Odtworzenie nagranego epizodu: slot `charge_grid` 947 W przy nadwyżce PV rosnącej do ~6 kW.

Fixture (zanonimizowany, minutowe średnie względem początku slotu) przechodzi przez `decide_cycle`
tick po ticku. Testowana jest decyzja/zapisy każdego ticku, nie fizyka falownika: odczyty biorą
się z nagrania niezależnie od tego, co cykl zapisał. Ticki sprzed początku slotu pomijamy
(to kontekst; wejście zaczyna się jak po restarcie, bez poprzedniego ticku).
"""
import json
from pathlib import Path

from custom_components.volcast.core.control.cycle import WRITE
from custom_components.volcast.core.control.pv_first import PHASE_GRID, PHASE_PV

from .test_cycle import plan, slot
from .test_cycle_pv_first import GRID, Sim

SLOT_W = 947.0
RATED_W = 10000.0
FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "pv_first_replay_goodwe_et.json"


def slot_rows():
    rows = json.loads(FIXTURE.read_text())["rows"]
    return [dict(r) for r in rows if r["t_s"] >= 0]


def replay(rows, *, allowed=True):
    sim = Sim(plan(slot("12:00", "13:00", **GRID, power_w=SLOT_W)), start="12:00:00", rated=RATED_W,
              max_charge=RATED_W, allowed=allowed)
    out = [sim.tick(pv=float(r["pv_w"]), load=float(r["load_w"]), battery=float(r["battery_w"]))
           for r in rows]
    return sim, out


def mode_writes(decisions):
    return [i for i, d in enumerate(decisions) if d.status == WRITE and "mode" in [w.key for w in d.writes]]


def test_fixture_precondition_surplus_stays_above_slot_power():
    rows = slot_rows()
    assert len(rows) == 60 and rows[0]["t_s"] == 0
    assert all(r["pv_w"] - r["load_w"] >= SLOT_W for r in rows)


def test_recorded_episode_goes_pv_early_and_stays():
    rows = slot_rows()
    sim, decisions = replay(rows)
    # Bez poprzedniego ticku wejście zajmuje 4 ticki (indeks 3 = 3 min po starcie slotu).
    first_pv = next(i for i, d in enumerate(decisions) if d.intent == "charge_pv")
    assert first_pv == 3
    assert decisions[first_pv].status == WRITE
    assert decisions[first_pv].flat["mode"] == "auto" and "power_w" not in decisions[first_pv].flat
    assert all(d.intent == "charge_pv" for d in decisions[first_pv:])
    assert sim.mem.pv_first.phase == PHASE_PV
    # Zapisy trybu: wejście w ładowanie ze slotu + przejście na auto.
    assert len(mode_writes(decisions)) <= 4

    # Sanity korzyści, nie fizyka: eksport przy ładowaniu stałą mocą slotu (kontrfaktyk) vs model w fazie pv
    # (bateria może pobrać do mocy znamionowej).
    counterfactual = [max(0.0, r["pv_w"] - r["load_w"] - SLOT_W) for r in rows]
    modelled = [max(0.0, r["pv_w"] - r["load_w"] - RATED_W) if d.intent == "charge_pv" else counterfactual[i]
                for i, (r, d) in enumerate(zip(rows, decisions))]
    gain = sum(counterfactual) / len(rows) - sum(modelled) / len(rows)
    assert gain >= 1000.0


def test_without_wiring_the_episode_stays_in_grid_phase():
    # Kontrola RED: z wyłączonym PV-first nagranie nigdy nie przechodzi na charge_pv.
    _, decisions = replay(slot_rows(), allowed=False)
    assert not any(d.intent == "charge_pv" for d in decisions)


def test_cloud_variant_returns_to_grid_and_reenters():
    rows = slot_rows()
    for r in rows[20:30]:
        r["pv_w"] = r["load_w"] + 300.0       # nadwyżka 300 W < 947 − 200
        r["battery_w"] = -SLOT_W
    sim, decisions = replay(rows)
    intents = [d.intent for d in decisions]
    assert intents[3] == "charge_pv"
    # Piąty kolejny niski tick (indeks 24) wraca do fazy grid i do ładowania stałą mocą.
    assert intents[23] == "charge_pv" and intents[24] == "charge_grid"
    assert decisions[24].flat["mode"] == "charge_battery" and decisions[24].flat["power_w"] == SLOT_W
    # Nadwyżka wraca od indeksu 30: trzeci tick (indeks 32) wchodzi w pv ponownie.
    assert intents[31] == "charge_grid" and intents[32] == "charge_pv"
    assert sim.mem.pv_first.phase == PHASE_PV
    # Zapisy trybu: start slotu, auto, powrót, auto. Histereza ogranicza je do 4 (bez migotania).
    assert len(mode_writes(decisions)) <= 4
