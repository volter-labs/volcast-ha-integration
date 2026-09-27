"""Dobowa symulacja cyklu okien czasowych: zapisy NVM mieszczą się w połowie budżetu profilu.

Kotwica dobowa sprawia, że starty programów zmieniają się tylko ze zmianą planu; każda
sekwencja przepisania kosztuje dwie ramki włącznika (OFF, ON), więc liczba sekwencji na
dobę musi być mała. Porażka = zmiana przydziału okien, nie podniesienie budżetu.
"""
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from custom_components.volcast.core.control.cycle import ControlMemory
from custom_components.volcast.core.control.tou_cycle import commit_tou
from custom_components.volcast.core.control.tou_writes import run_tou_writes
from custom_components.volcast.core.slot import parse_schedule
from custom_components.volcast.core.write_sequence import OK
from tests.sim.fixtures import deye_words

from .tou_helpers import DEYE, decide, iso, reading

GOLDEN = Path(__file__).resolve().parents[2] / "golden" / "goodwe_et"
STEP = timedelta(minutes=5)


def _plan_live_days(days=3):
    """`plan_live.json` powtórzony na kolejne doby (plan odświeżany co godzinę ma zawsze jutro)."""
    raw = json.loads((GOLDEN / "plan_live.json").read_text())
    slots = []
    last_end = None
    for d in range(days):
        for s in raw["slots"]:
            frm, to = (datetime.fromisoformat(s[k].replace("Z", "+00:00")) + timedelta(days=d)
                       for k in ("from", "to"))
            if last_end is not None and frm < last_end:
                continue                                 # plan jest dłuższy niż doba — bez nakładek
            slots.append({**s, "from": iso(frm), "to": iso(to)})
            last_end = to
    return parse_schedule({"schedule_id": raw["schedule_id"], "slots": slots, "fallback": raw["fallback"]})


def _quarter_plan(issued: datetime):
    """Plan 15-minutowy na 48 h od pełnej godziny wydania; okno taniego ładowania przesuwa się
    o 15 min z doby na dobę, a wieczorny postój ma inną długość w dni parzyste."""
    base = issued.replace(minute=0, second=0, microsecond=0)
    slots = []
    for q in range(48 * 4):
        t = base + timedelta(minutes=15 * q)
        loc = t.astimezone(timezone(timedelta(hours=2)))
        day = loc.toordinal()
        minute = loc.hour * 60 + loc.minute
        charge_from = 120 + 15 * (day % 3)
        stand_to = 19 * 60 + (30 if day % 2 else 0)
        if charge_from <= minute < charge_from + 180:
            f = {"mode": "charge", "charge_source": "grid", "power_w": 3000 + 250 * (day % 2),
                 "soc_target": 90, "price_pln_kwh": 0.2}
        elif 17 * 60 <= minute < stand_to:
            f = {"mode": "idle", "price_pln_kwh": 1.1}
        else:
            f = {"mode": "self_consume", "price_pln_kwh": 0.5 + 0.01 * (minute // 60)}
        slots.append({"from": iso(t), "to": iso(t + timedelta(minutes=15)), **f})
    return parse_schedule({"schedule_id": f"q{base:%H}", "slots": slots,
                           "fallback": {"mode": "self_consume", "soc_reserve": 10}})


def _simulate(plan_at, start: datetime) -> Counter:
    words = deye_words()
    memory = ControlMemory.for_profile(DEYE)
    frames: Counter = Counter()

    def write(w):
        words[w.addr] = w.value
        frames[w.key] += 1
        return OK

    plan = None
    for i in range(24 * 12):
        now = start + STEP * i
        if plan is None or now.minute == 0:
            plan = plan_at(now)                          # odświeżenie co godzinę
        d, _ = decide(plan, reading(words), memory=memory, now=now, now_mono=1000.0 + 300.0 * i)
        assert d.status in ("write", "idle"), d.reason
        if d.status == "write":
            rep = run_tou_writes(d.writes, write, pre_held=d.pre_held)
            commit_tou(d, rep, memory, 1000.0 + 300.0 * i, now_wall=now.timestamp())
    assert memory.budget.hit is False
    return frames


@pytest.mark.parametrize("name", ["plan_live", "quarter_hour"])
def test_tou_day_write_count_within_budget(name):
    if name == "plan_live":
        plans = _plan_live_days()
        start = plans.slots[0].start
        frames = _simulate(lambda now: plans, start)
    else:
        start = datetime(2026, 9, 1, 4, tzinfo=timezone.utc)
        frames = _simulate(_quarter_plan, start)
    budget = DEYE.nvm_budget
    assert frames, "symulacja nie zapisała nic — test niczego nie sprawdza"
    assert max(frames.values()) < budget.per_key / 2, frames.most_common(3)
    assert sum(frames.values()) < budget.total / 2, sum(frames.values())
