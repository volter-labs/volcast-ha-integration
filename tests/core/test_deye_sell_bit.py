"""Sprzedaż z baterii w profilu okien czasowych: bit Sell w słowie flag programu (Deye: bit 5 w 172–177).

Okno sprzedaży = bit Sell ustawiony, docelowy SoC okna = podłoga rozładowania (cel slotu, nie niżej niż
rezerwa), moc okna = moc rozładowania slotu. Trybu pracy ani limitu eksportu nie piszemy. Bit Sell jest
odczyt-modyfikacja-zapis jak bit ładowania z sieci: pozostałe bity słowa (np. bit 1 Gen) zostają.
Bramka: bez `capabilities.sell_from_battery` slot sprzedaży wykonuje się jako samokonsumpcja (jak dotąd).
Tryb zapasowy, plan przeterminowany i tryb bazowy: bit Sell zawsze wyczyszczony.
"""
import copy
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.volcast.core.control.tou_cycle import _toward_safety
from custom_components.volcast.core.control.tou_writes import tou_restore_writes, tou_snapshot
from custom_components.volcast.core.engines.time_window import baseline_programs, compress, program_diff
from custom_components.volcast.core.modbus.reading import build_reading, current_programs
from custom_components.volcast.core.modbus.writer import _fresh_value
from custom_components.volcast.core.params import Params, TouProgram
from custom_components.volcast.core.profile import PROFILES_DIR, load_builtin, profile_from_dict
from custom_components.volcast.core.profile_schema import validate_profile
from custom_components.volcast.core.registers import RegisterImage, encode_writes
from custom_components.volcast.core.slot import parse_schedule
from tests.core.golden import T0
from tests.core.profile_fixtures import tw_profile
from tests.sim.fixtures import deye_words

WAW = ZoneInfo("Europe/Warsaw")
DAY0 = datetime(2026, 9, 1, 22, tzinfo=timezone.utc)       # 00:00 czasu warszawskiego
DEYE = load_builtin("deye-sg")
GEN = 0b10                                                 # bit 1 (Gen) — nie nasz, musi zostać
SELL = 1 << 5


def _deye_raw() -> dict:
    return json.loads((PROFILES_DIR / "deye-sg.json").read_text(encoding="utf-8"))


def _selling() -> object:
    raw = _deye_raw()
    raw["capabilities"]["sell_from_battery"] = True
    return profile_from_dict(raw, origin="package")


DEYE_SELL = _selling()


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


SELF = {"mode": "self_consume", "price_pln_kwh": 0.5}
SELL_SLOT = {"mode": "discharge", "discharge_purpose": "sell", "power_w": 3000, "soc_target": 40,
             "price_pln_kwh": 1.5}


def _plan(hours, fallback=None):
    slots = [{"from": _iso(DAY0 + timedelta(hours=h)), "to": _iso(DAY0 + timedelta(hours=h + 1)), **f}
             for h, f in hours]
    return parse_schedule({"schedule_id": "t", "slots": slots,
                           "fallback": fallback or {"mode": "self_consume", "soc_reserve": 15}})


def _sell_day():
    return _plan([(h, SELL_SLOT if h in (19, 20) else SELF) for h in range(24)])


def _run(profile, schedule=None, reserve=10.0):
    return compress(schedule or _sell_day(), DAY0, profile, soc_reserve=reserve, rated_power_w=8000.0, tz=WAW)


# ── profil ────────────────────────────────────────────────────────────────


def test_deye_profile_declares_sell_bit_but_keeps_capability_off():
    tp = DEYE.raw["write"]["tou_program"]
    assert tp["sell"] == {"addr": 172, "bit": 5}
    assert tp["grid_charge"] == {"addr": 172, "bit": 0}
    assert DEYE.intent("sell") == {"grid_charge": False, "sell": True, "soc": "target_or_max", "power": "slot"}
    assert DEYE.raw["capabilities"]["sell_from_battery"] is False      # bramka do etapu „sprzedaż” na żywo
    assert "sell" in DEYE.tou_field_order
    # nadal bez trybu pracy i limitu eksportu (142, 143, 145, 266, 343 nie są zapisywane)
    assert set(DEYE.raw["write"]) == {"tou_program", "tou_enable"}


# ── kodowanie bitu ────────────────────────────────────────────────────────


def _program(sell, grid=False):
    return TouProgram(start_min=0, power_w=3000.0, soc=40.0, grid_charge=grid, sell=sell)


def test_encode_sell_window_keeps_gen_bit():
    cur = RegisterImage.from_blocks({172: [GEN] * 6})
    ws = {w.key: w for w in encode_writes(Params(tou=(_program(True),)), DEYE, current=cur)}
    assert ws["tou.1.sell"].addr == 172
    assert ws["tou.1.sell"].value == 0b100010                 # Gen + Sell
    assert ws["tou.1.grid_charge"].value == GEN               # bit 0 zdjęty, Gen został


def test_encode_composes_both_bits_of_one_word():
    # Ładowanie z sieci → sprzedaż w jednym przebiegu: drugi zapis tego słowa niesie wynik pierwszego.
    cur = RegisterImage.from_blocks({172: [GEN | 1] * 6})
    ws = encode_writes(Params(tou=(_program(True),)), DEYE, current=cur)
    by_key = {w.key: w.value for w in ws}
    assert by_key["tou.1.grid_charge"] == GEN
    assert by_key["tou.1.sell"] == GEN | SELL                 # bez starego bitu 0


def test_encode_clears_owner_sell_bit_in_our_window():
    cur = RegisterImage.from_blocks({172: [GEN | SELL] * 6})
    ws = {w.key: w for w in encode_writes(Params(tou=(_program(False),)), DEYE, current=cur)}
    assert ws["tou.1.sell"].value == GEN


def test_sell_bit_needs_a_fresh_word():
    from custom_components.volcast.core.registers import RegisterError
    with pytest.raises(RegisterError):
        encode_writes(Params(tou=(_program(True),)), DEYE, keys={"tou.1.sell"})


def test_writer_fresh_value_masks_only_the_sell_bit():
    assert _fresh_value(DEYE, "tou.3.sell", SELL, GEN | 1) == GEN | 1 | SELL
    assert _fresh_value(DEYE, "tou.3.sell", GEN, GEN | 1 | SELL) == GEN | 1
    # bit ładowania z sieci dalej tylko swój bit
    assert _fresh_value(DEYE, "tou.3.grid_charge", 0, GEN | 1 | SELL) == GEN | SELL


# ── kompresja planu ───────────────────────────────────────────────────────


def test_compress_sell_slot_with_capability_gives_sell_window():
    r = _run(DEYE_SELL)
    assert r.degraded == {}
    sells = [w for w in r.windows if w.intent == "sell"]
    assert len(sells) == 1
    w = sells[0]
    assert (w.program.sell, w.program.grid_charge, w.program.soc, w.program.power_w) == (True, False, 40.0, 3000.0)
    progs = [p for p in r.programs if p.sell]
    assert len(progs) == 1 and progs[0].start_min == 19 * 60
    assert all(p.sell is False for p in r.programs if p is not progs[0])


def test_sell_window_floor_never_below_reserve():
    r = _run(DEYE_SELL, reserve=50.0)
    assert [p.soc for p in r.programs if p.sell] == [50.0]


def test_sell_window_without_target_does_not_discharge():
    # Brak celu slotu = podłoga 100 % (nie rozładowuje) — nigdy „do rezerwy” w ciemno.
    plan = _plan([(h, {**SELL_SLOT, "soc_target": None} if h == 19 else SELF) for h in range(24)])
    assert [p.soc for p in _run(DEYE_SELL, plan).programs if p.sell] == [100.0]


def test_compress_sell_slot_without_capability_is_self_consume_as_today():
    r = _run(DEYE)
    assert r.degraded == {"sell": 2}
    assert not any(w.intent == "sell" for w in r.windows)
    assert all(p.sell is False for p in r.programs)
    # ten sam wynik co profil bez bitu Sell (dotychczasowe zachowanie)
    old = _deye_raw()
    del old["write"]["tou_program"]["sell"]
    old["tou"]["field_order"] = ["soc", "power_w", "grid_charge", "start"]
    old["intents"]["sell"] = None
    legacy = _run(profile_from_dict(old, origin="package"))
    assert [(p.start_min, p.power_w, p.soc, p.grid_charge) for p in r.programs] == \
        [(p.start_min, p.power_w, p.soc, p.grid_charge) for p in legacy.programs]
    assert r.lost_value_pln == legacy.lost_value_pln and r.degraded == legacy.degraded


def test_day_anchor_also_gates_sell():
    r = compress(_sell_day(), DAY0, DEYE, soc_reserve=10.0, rated_power_w=8000.0, tz=WAW, anchor="day")
    assert all(p.sell is False for p in r.programs)
    r = compress(_sell_day(), DAY0, DEYE_SELL, soc_reserve=10.0, rated_power_w=8000.0, tz=WAW, anchor="day")
    assert sum(1 for p in r.programs if p.sell) == 1


def test_fallback_slots_never_sell():
    # Plan przeterminowany: aktywne sloty zastępuje slot zapasowy — bit Sell wyczyszczony wszędzie.
    stale = _sell_day().active_as_fallback()
    assert all(p.sell is False for p in _run(DEYE_SELL, stale).programs)
    # Slot zapasowy niosący sprzedaż (kształt od chmury) też nie sprzedaje.
    from dataclasses import replace
    sch = _sell_day()
    marked = replace(sch, slots=tuple(replace(s, from_fallback=True) for s in sch.slots))
    r = _run(DEYE_SELL, marked)
    assert all(p.sell is False for p in r.programs)


def test_baseline_programs_clear_sell():
    assert all(p.sell is False for p in baseline_programs(DEYE_SELL, 10.0, 8000.0))


def test_program_diff_sees_sell_change():
    a = (_program(False),)
    b = (_program(True),)
    assert program_diff(b, a) == {"tou.1.sell"}


# ── odczyt, migawka, powrót, bezpieczeństwo ─────────────────────────────


def _reading(**over):
    words = deye_words()
    words.update({int(a): v for a, v in over.items()})
    return build_reading(DEYE, RegisterImage(words), at_mono=0.0, at_utc=T0)


def test_reading_decodes_sell_bit():
    rd = _reading(**{"173": GEN | SELL})
    assert rd.programs[1].sell is True and rd.programs[0].sell is False
    assert rd.device["tou.2.sell"] == 1.0 and rd.device["tou.1.sell"] == 0.0
    assert current_programs(DEYE, rd.image)[1].sell is True


def test_restore_returns_owner_sell_bit():
    owner = _reading(**{"174": GEN | SELL})
    snap = tou_snapshot(owner, DEYE)
    assert snap["programs"][2][3] == GEN | SELL                # surowe słowo — format migawki bez zmian
    ours = _reading(**{"174": GEN, "146": 0xFF})
    writes = tou_restore_writes(DEYE, snap, ours, soc_reserve=10.0, rated_power_w=10000.0)
    sell = [w for w in writes if w.key == "tou.3.sell"]
    assert len(sell) == 1 and sell[0].value == GEN | SELL


def test_device_selling_where_plan_does_not_is_toward_safety():
    dev = _reading(**{"172": SELL}).programs                  # program 1 urządzenia sprzedaje
    planned = tuple(TouProgram(p.start_min, p.power_w, p.soc, p.grid_charge, sell=False) for p in dev)
    assert _toward_safety(dev, planned, DEYE)
    same = tuple(TouProgram(p.start_min, p.power_w, p.soc, p.grid_charge, sell=p.sell) for p in dev)
    assert not _toward_safety(dev, same, DEYE)


# ── regresja: profil bez bitu Sell ────────────────────────────────────────


def test_profile_without_sell_field_is_unchanged():
    tw = profile_from_dict(tw_profile(), origin="package")
    r = compress(_sell_day(), DAY0, tw, soc_reserve=10.0, rated_power_w=8000.0, tz=WAW)
    assert all(p.sell is None for p in r.programs)
    assert not any(k.endswith(".sell") for k in r.params().flatten())
    assert TouProgram(0, 1.0, 2.0, False) == TouProgram(0, 1.0, 2.0, False, sell=None)


# ── schemat ───────────────────────────────────────────────────────────────


def _raw_with(**edits):
    raw = _deye_raw()
    for path, value in edits.items():
        node = raw
        *parents, leaf = path.split("__")
        for p in parents:
            node = node[p]
        if value is _DEL:
            del node[leaf]
        else:
            node[leaf] = value
    return raw


_DEL = object()


def test_schema_accepts_deye_with_sell():
    assert validate_profile(_deye_raw()) == []


@pytest.mark.parametrize("edits, needle", [
    ({"write__tou_program__sell": {"addr": 173, "bit": 5}}, "tou_program.sell.addr"),
    ({"write__tou_program__sell": {"addr": 172, "bit": 0}}, "tou_program.sell.bit"),
    ({"write__tou_program__sell": {"addr": 172}}, "tou_program.sell.bit"),
    ({"tou__field_order": ["soc", "power_w", "grid_charge", "start"]}, "field_order"),
    ({"write__tou_program__sell": _DEL}, "intents.sell.sell"),
    ({"intents__sell": {"grid_charge": True, "sell": True, "soc": "target_or_max", "power": "slot"}},
     "intents.sell"),
    ({"intents__charge_grid": {"grid_charge": True, "sell": True, "soc": "target_or_max", "power": "slot"}},
     "intents.charge_grid.sell"),
    ({"intents__sell": {"grid_charge": False, "sell": "yes", "soc": "target_or_max", "power": "slot"}},
     "intents.sell.sell"),
])
def test_schema_rejects_bad_sell(edits, needle):
    errors = validate_profile(_raw_with(**edits))
    assert any(needle in e for e in errors), errors


def test_schema_field_order_with_sell_requires_the_field():
    raw = tw_profile()
    raw["tou"]["field_order"] = ["soc", "power_w", "grid_charge", "sell", "start"]
    assert any("field_order" in e for e in validate_profile(raw))


# ── okno sprzedaży nigdy nie rozlewa się na sloty bez sprzedaży (scalanie do N programów) ──────────


def _sell_minutes_ok(schedule, programs, t0, tz, step=5):
    """Każda pora doby, w której aktywny program ma bit Sell, przypada (wg najbliższego wystąpienia od t0,
    jak liczy silnik) na zaplanowany slot sprzedaży — nie zapasowy, nie inną intencję. Zwraca naruszenia."""
    from custom_components.volcast.core.engines.mode_setpoint import slot_intent
    ordered = sorted(programs, key=lambda p: p.start_min)
    loc0 = t0.astimezone(tz)
    m0, d0 = loc0.hour * 60 + loc0.minute, loc0.date()
    bad = []
    for m in range(0, 1440, step):
        prog = ordered[-1]
        for p in ordered:
            if p.start_min <= m:
                prog = p
        if not prog.sell:
            continue
        day = d0 if m >= m0 else d0 + timedelta(days=1)
        inst = datetime.combine(day, datetime.min.time(), tzinfo=tz).replace(hour=m // 60, minute=m % 60)
        s = schedule.slot_for(inst.astimezone(timezone.utc))
        if s is None or s.from_fallback or slot_intent(s)[0] != "sell":
            bad.append(m)
    return bad


def _morning_plan():
    # Teraz 07:00, plan tylko na dziś 07–24 (ceny jutra jeszcze nieznane): 00–07 to sloty zapasowe.
    pat = {7: SELL_SLOT, 8: SELL_SLOT, 19: SELL_SLOT}
    ch = {"mode": "charge", "charge_source": "grid", "power_w": 2000, "soc_target": 90, "price_pln_kwh": 0.2}
    for h in (11, 12, 15, 16):
        pat[h] = ch
    for h in (13, 17):
        pat[h] = {"mode": "idle", "price_pln_kwh": 0.9}
    return _plan([(h, pat.get(h, SELF)) for h in range(7, 24)])


@pytest.mark.parametrize("anchor", ["day", "now"])
def test_merge_never_extends_sell_over_fallback(anchor):
    plan = _morning_plan()
    now = DAY0 + timedelta(hours=7)
    r = compress(plan, now, DEYE_SELL, soc_reserve=10.0, rated_power_w=8000.0, tz=WAW, anchor=anchor)
    assert r.merges                                                 # więcej okien niż programów
    t0 = plan.slot_for(now).start
    assert _sell_minutes_ok(plan, r.programs, t0, WAW) == []


def _random_plan(rng):
    import random  # noqa: F401 — rng podany z zewnątrz
    first = rng.randrange(0, 24)
    last = rng.randrange(first + 1, 25)
    hours = []
    for h in range(first, last):
        k = rng.random()
        if k < 0.3:
            f = {"mode": "discharge", "discharge_purpose": "sell", "power_w": rng.choice([1000, 3000, 6000]),
                 "soc_target": rng.choice([None, 20, 40, 70])}
        elif k < 0.5:
            f = {"mode": "charge", "charge_source": "grid", "power_w": rng.choice([1000, 3000]), "soc_target": 90}
        elif k < 0.6:
            f = {"mode": "idle"}
        elif k < 0.65:
            continue                                                 # dziura w planie = slot zapasowy
        else:
            f = {"mode": "self_consume"}
        if rng.random() < 0.85:
            f["price_pln_kwh"] = round(rng.uniform(-0.2, 2.0), 3)
        hours.append((h, f))
    return _plan(hours or [(first, SELF)]), first


@pytest.mark.parametrize("seed", range(300))
def test_property_sell_bit_only_over_planned_sell_slots(seed):
    import random
    rng = random.Random(seed)
    plan, first = _random_plan(rng)
    now = DAY0 + timedelta(hours=rng.randrange(0, 24), minutes=rng.choice([0, 20, 45]))
    for anchor in ("day", "now"):
        r = compress(plan, now, DEYE_SELL, soc_reserve=10.0, rated_power_w=8000.0, tz=WAW, anchor=anchor)
        cov = plan.slot_for(now)
        loc = now.astimezone(WAW)
        t0 = cov.start if cov is not None else loc.replace(minute=loc.minute - loc.minute % 5, second=0)
        assert _sell_minutes_ok(plan, r.programs, t0, WAW) == [], (seed, anchor)


def test_autumn_repeated_hour_prefers_program_without_sell():
    from datetime import date
    from tests.core.test_time_window_dst import plan_from

    def pattern(loc):
        if loc.hour == 2:
            return {**SELL_SLOT} if loc.utcoffset() == timedelta(hours=2) else SELF
        return SELF
    plan = plan_from(pattern, date(2026, 10, 22))
    now = datetime(2026, 10, 24, 10, tzinfo=timezone.utc)
    r = compress(plan, now, DEYE_SELL, soc_reserve=10.0, rated_power_w=10000.0, tz=WAW, anchor="day")
    active = [p for p in sorted(r.programs, key=lambda p: p.start_min) if p.start_min <= 150][-1]
    assert active.sell is False


def test_schema_sell_bit_rejected_with_ha_program_entities():
    raw = _deye_raw()
    raw["ha"]["integrations"][0]["entities"]["tou_1_soc"] = {"domain": "number", "unique_id_regex": "^x_tou1_soc$"}
    assert any("tou_program.sell" in e and "bezpośredni" in e for e in validate_profile(raw))
