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
