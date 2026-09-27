"""Wspólne dane testów cyklu okien czasowych (Deye, dane syntetyczne)."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from custom_components.volcast.core.control.cycle import ControlMemory, Gates, Limits, Telemetry
from custom_components.volcast.core.control.tou_cycle import decide_tou_cycle
from custom_components.volcast.core.modbus.reading import build_reading
from custom_components.volcast.core.profile import load_builtin
from custom_components.volcast.core.registers import RegisterImage
from custom_components.volcast.core.slot import parse_schedule
from tests.sim.fixtures import deye_words

DEYE = load_builtin("deye-sg")
WAW = ZoneInfo("Europe/Warsaw")
NOW = datetime(2026, 9, 1, 10, tzinfo=timezone.utc)          # 12:00 w Warszawie
DAY0 = datetime(2026, 8, 31, 22, tzinfo=timezone.utc)         # 2026-09-01 00:00 w Warszawie
GATES = Gates(consent=True, local_switch=True, control_mode="direct", verified=True)
SELF = {"mode": "self_consume", "price_pln_kwh": 0.5}


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def daily_plan(pattern=None, days=3, start=DAY0, reserve=10):
    """Ten sam wzór godzinowy każdej doby lokalnej (od doby przed `start`)."""
    pattern = pattern or {h: ({"mode": "charge", "charge_source": "grid", "power_w": 3000, "soc_target": 90,
                               "price_pln_kwh": 0.2} if 2 <= h < 5 else SELF) for h in range(24)}
    slots = []
    for d in range(-1, days):
        for h in range(24):
            s = start + timedelta(days=d, hours=h)
            slots.append({"from": iso(s), "to": iso(s + timedelta(hours=1)), **pattern[h]})
    return parse_schedule({"schedule_id": "tou", "slots": slots,
                           "fallback": {"mode": "self_consume", "soc_reserve": reserve}})


FRESH_MONO = 1e9          # odczyt rozpoczęty po każdym zapisie testu (zegar monotoniczny testów < 1e9)


def reading(words=None, *, at_mono=FRESH_MONO, **over):
    w = dict(words or deye_words())
    w.update({int(a): v for a, v in over.items()})
    return build_reading(DEYE, RegisterImage(w), at_mono=at_mono, at_utc=NOW)


def decide(schedule, rd, *, memory=None, gates=GATES, now=NOW, now_mono=1000.0, soc=60.0, age=5.0,
           rated=10000.0, owner_word=None):
    memory = memory or ControlMemory.for_profile(DEYE)
    d = decide_tou_cycle(profile=DEYE, schedule=schedule, now_utc=now, now_mono=now_mono, tz=WAW,
                         tele=Telemetry(soc=soc, soc_age_s=age, battery_temp_c=None),
                         limits=Limits(rated_power_w=rated), reading=rd, gates=gates, memory=memory,
                         owner_word=owner_word)
    return d, memory


def apply(words, writes):
    """Symulowany falownik: zapis = słowo w rejestrze."""
    out = dict(words)
    for w in writes:
        out[w.addr] = w.value
    return out
