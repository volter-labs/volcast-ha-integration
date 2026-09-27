"""Dobowa symulacja cyklu okien czasowych: zapisy NVM mieszczą się w połowie budżetu profilu.

Kotwica dobowa sprawia, że starty programów zmieniają się tylko ze zmianą planu; każda
sekwencja przepisania kosztuje dwie ramki włącznika (OFF, ON), więc liczba sekwencji na
dobę musi być mała. Porażka = zmiana przydziału okien, nie podniesienie budżetu.
"""
import json
import random
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


def _varying_plan(issued: datetime, jitter: bool):
    """Plan godzinowy na 48 h od wydania: każda doba inna (okno ładowania i postoju losowane
    z ziarnem doby), a przy `jitter` każde odświeżenie ma drobny szum mocy i celu SoC."""
    base = issued.replace(minute=0, second=0, microsecond=0)
    j = issued.hour % 3 if jitter else 0
    slots = []
    for q in range(48):
        t = base + timedelta(hours=q)
        loc = t.astimezone(timezone(timedelta(hours=2)))
        rnd = random.Random(loc.toordinal())
        cs = rnd.choice([1, 2, 3])
        ce = cs + rnd.choice([2, 3])
        ss = rnd.choice([17, 18, 19])
        se = ss + rnd.choice([1, 2, 3])
        if cs <= loc.hour < ce:
            f = {"mode": "charge", "charge_source": "grid", "power_w": 3000 + 100 * j, "soc_target": 90 - j,
                 "price_pln_kwh": 0.2}
        elif ss <= loc.hour < se:
            f = {"mode": "idle", "price_pln_kwh": 1.1}
        else:
            f = {"mode": "self_consume", "price_pln_kwh": 0.5}
        slots.append({"from": iso(t), "to": iso(t + timedelta(hours=1)), **f})
    return parse_schedule({"schedule_id": f"v{base:%H}", "slots": slots,
                           "fallback": {"mode": "self_consume", "soc_reserve": 10}})


def _simulate(plan_at, start: datetime) -> Counter:
    words = deye_words()
    memory = ControlMemory.for_profile(DEYE)
    frames: Counter = Counter()
    clock = [start]

    def write(w):
        # Jak pisarz rejestrów: włącznik już w żądanym stanie → bez ramki; każda ramka w budżecie.
        if w.key == "tou_enable":
            new = (words[146] & ~1) if not w.value & 1 else (words[146] | 1 | 0xFE)
            if new == words[146]:
                return OK
            w = type(w)(w.key, w.addr, new)
        words[w.addr] = w.value
        frames[w.key] += 1
        memory.budget.note(w.key, clock[0].timestamp())
        return OK

    plan = None
    for i in range(24 * 12):
        now = start + STEP * i
        clock[0] = now
        if plan is None or now.minute == 0:
            plan = plan_at(now)                          # odświeżenie co godzinę
        d, _ = decide(plan, reading(words), memory=memory, now=now, now_mono=1000.0 + 300.0 * i)
        assert d.status in ("write", "idle"), d.reason
        if d.status == "write":
            rep = run_tou_writes(d.writes, write, pre_held=d.pre_held)
            commit_tou(d, rep, memory, 1000.0 + 300.0 * i)
    assert memory.budget.hit is False
    assert words[146] & 1, "doba skończyła się z wyłączonym harmonogramem"
    return frames


@pytest.mark.parametrize("name", ["plan_live", "quarter_hour", "days_differ", "hourly_jitter"])
def test_tou_day_write_count_within_budget(name):
    start = datetime(2026, 9, 1, 4, tzinfo=timezone.utc)
    if name == "plan_live":
        plans = _plan_live_days()
        start = plans.slots[0].start
        frames = _simulate(lambda now: plans, start)
    elif name == "quarter_hour":
        frames = _simulate(_quarter_plan, start)
    else:
        frames = _simulate(lambda now: _varying_plan(now, jitter=name == "hourly_jitter"), start)
    budget = DEYE.nvm_budget
    assert frames, "symulacja nie zapisała nic — test niczego nie sprawdza"
    assert max(frames.values()) < budget.per_key / 2, frames.most_common(3)
    assert sum(frames.values()) < budget.total / 2, sum(frames.values())
