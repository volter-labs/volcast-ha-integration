"""Sprzedaż z mocą `slot_live_export`: nastawa eksportu liczona w cyklu z odczytów PV i poboru.

Moc slotu sprzedaży to moc baterii; cykl zamienia ją PO strażnikach i PRZED dopasowaniem
do encji oraz throttlingiem na nastawę eksportu `bateria + PV − dom` (czysta funkcja
`sell_xset`). Każda inna intencja i każdy inny rodzaj mocy działa bez zmian.
"""
import copy
import math
from dataclasses import replace

import pytest

from custom_components.volcast.core.control.cycle import (BLOCKED, DRY_RUN, WRITE, ControlMemory, EntityContext,
                                                          Limits, Telemetry, commit, decide_cycle)
from custom_components.volcast.core.control.group_writes import GroupReport
from custom_components.volcast.core.control.live_export import (LIVE_EXPORT_KIND, NOTE_SELL_BELOW_MIN,
                                                                NOTE_SELL_NO_LOAD, NOTE_SELL_XSET)
from custom_components.volcast.core.profile import profile_from_dict
from custom_components.volcast.core.write_sequence import WriteReport

from .test_cycle import ATTRS, GW, MAPPED, NOW, OPEN, SELL, UNITS, at, plan, slot

SELL_513 = plan(slot("10:00", "11:00", **SELL, power_w=513, export_limit_w=1520))
FRESH = dict(pv=978.0, pv_age=10.0, load=319.0, load_age=10.0)


def tick(memory=None, *, schedule=SELL_513, pv=None, pv_age=None, load=None, load_age=None,
         soc=60.0, readings=None, gates=OPEN, attrs=ATTRS, now_utc=NOW, now_mono=1000.0,
         profile=GW, owner_values=None, rated=8000.0):
    memory = memory or ControlMemory.for_profile(profile)
    d = decide_cycle(
        profile=profile, schedule=schedule, now_utc=now_utc, now_mono=now_mono,
        tele=Telemetry(soc=soc, soc_age_s=10.0, battery_temp_c=25.0, pv_power_w=pv, pv_age_s=pv_age,
                       load_power_w=load, load_age_s=load_age),
        limits=Limits(rated_power_w=rated),
        ents=EntityContext(domain="goodwe", mapped=MAPPED, units=UNITS, attrs=attrs,
                           readings=readings or {}, owner_values=owner_values or {}),
        gates=gates, memory=memory)
    return d, memory


def power(d):
    return {w.key: w.data for w in d.writes}.get("power_w", {}).get("value")


def xset_notes(d):
    return [n for n in d.notes if n.startswith(NOTE_SELL_XSET)]


def written(d):
    return WriteReport(written=[w.key for w in d.writes])


# ── Przeliczenie ──

def test_goodwe_sell_intent_is_the_live_export_kind():
    # Zmiana nazwy intencji albo rodzaju w profilu musi przerwać ten test, nie wyłączyć przeliczenia po cichu.
    assert GW.power_kind("sell") == LIVE_EXPORT_KIND == "slot_live_export"


def test_sell_with_pv_surplus_writes_battery_plus_pv_minus_load():
    d, _ = tick(**FRESH)
    assert d.status == WRITE and d.intent == "sell"
    assert d.flat["power_w"] == 1172.0 and power(d) == 1172.0
    assert {"option": "sell_power"} in [w.data for w in d.writes]
    assert xset_notes(d) == ["sell_xset:battery=513,pv=978,load=319,xset=1172"]
    assert d.summary()["notes"] == list(d.notes) and "power_w" in d.summary()["would_write"]


def test_night_sell_covers_the_house_first():
    d, _ = tick(schedule=plan(slot("10:00", "11:00", **SELL, power_w=2000)),
                pv=0.0, pv_age=5.0, load=319.0, load_age=5.0)
    assert power(d) == 1681.0


def test_ceiling_is_rated_power_without_an_enabled_limiter():
    d, _ = tick(schedule=plan(slot("10:00", "11:00", **SELL, power_w=7900)), pv=900.0, pv_age=1.0,
                load=100.0, load_age=1.0)
    assert power(d) == 8000.0


def test_disabled_owner_limiter_is_not_a_ceiling():
    owner = {"export_limit_enabled": 0.0, "export_limit_w": 500.0}
    d, _ = tick(schedule=plan(slot("10:00", "11:00", **SELL, power_w=2000)), owner_values=owner, **FRESH)
    assert power(d) == 2000.0 + 978.0 - 319.0


def test_enabled_owner_limiter_is_the_ceiling():
    owner = {"export_limit_enabled": 1.0, "export_limit_w": 500.0}
    d, _ = tick(schedule=plan(slot("10:00", "11:00", **SELL, power_w=2000)), owner_values=owner, **FRESH)
    assert power(d) == 500.0


@pytest.mark.parametrize("slot_kw", [
    dict(export_allowed=False),                 # plan: zakaz eksportu (ogranicznik włączony, 0 W)
    dict(price_pln_kwh=-0.2),                   # strażnik I-4: cena <= 0 → zakaz
])
def test_export_ban_gives_zero_export(slot_kw):
    d, _ = tick(schedule=plan(slot("10:00", "11:00", **{**SELL, **slot_kw}, power_w=2000)), **FRESH)
    assert d.flat["export_limit_w"] == 0.0 and d.flat["export_limit_enabled"] == 1.0
    assert d.flat["mode"] == "sell_power" and power(d) == 0.0
    assert xset_notes(d) == ["sell_xset:battery=2000,pv=978,load=319,xset=0"]


# ── Świeżość i wiarygodność odczytów ──

@pytest.mark.parametrize("pv,pv_age", [
    (978.0, math.inf),        # brak znacznika czasu
    (978.0, 300.5),           # starszy niż max_state_age_s
    (978.0, -10.0),           # przesunięcie zegara (≤ −5 s nie jest zerowane)
    (978.0, math.nan),
    (978.0, None),
    (None, None),             # brak mapowania / odczytu
    (-5.0, 10.0),             # ujemny odczyt = nieważny, nie 0
    (16001.0, 10.0),          # ponad 2 × moc znamionowa = nieważny
    (math.inf, 10.0),
])
def test_invalid_pv_falls_back_to_battery_minus_load(pv, pv_age):
    d, mem = tick(pv=pv, pv_age=pv_age, load=319.0, load_age=10.0)
    assert power(d) == 513.0 - 319.0
    assert mem.live_export.last_load_w == 319.0


def test_reading_exactly_at_the_age_limit_is_fresh():
    d, _ = tick(pv=978.0, pv_age=300.0, load=319.0, load_age=300.0)
    assert power(d) == 1172.0


@pytest.mark.parametrize("load,load_age", [
    (400.0, math.inf), (400.0, 301.0), (400.0, -30.0), (None, None),
    (-50.0, 10.0),            # chwilowo ujemny pobór liczony (PV + bateria − sieć) — nieważny
    (16001.0, 10.0),
])
def test_invalid_load_uses_last_known_valid_load(load, load_age):
    mem = ControlMemory.for_profile(GW)
    tick(mem, pv=978.0, pv_age=10.0, load=250.0, load_age=10.0)        # ostatni ważny pobór: 250 W
    d, mem = tick(mem, pv=978.0, pv_age=10.0, load=load, load_age=load_age)
    assert power(d) == 513.0 - 250.0 and NOTE_SELL_NO_LOAD not in d.notes
    assert mem.live_export.last_load_w == 250.0                         # nieważny odczyt nie nadpisuje


@pytest.mark.parametrize("load,load_age", [
    (None, None), (319.0, math.inf), (319.0, 1000.0), (-50.0, 10.0), (99999.0, 10.0)])
def test_no_load_ever_known_suspends_selling(load, load_age):
    d, mem = tick(pv=978.0, pv_age=10.0, load=load, load_age=load_age)
    assert d.status == WRITE and d.flat["mode"] == "sell_power" and power(d) == 0.0
    assert NOTE_SELL_NO_LOAD in d.notes and mem.live_export.last_load_w is None


def test_negative_load_is_not_clamped_to_zero():
    # Pobór −50 W przycięty do 0 dałby 513 + 978 = 1491 W — więcej eksportu z baterii.
    d, _ = tick(pv=978.0, pv_age=10.0, load=-50.0, load_age=10.0)
    assert power(d) == 0.0


# ── Moc znamionowa nieznana (brak opcji w konfiguracji): pułapem jest zakres encji mocy ──

NO_POWER_RANGE = {**ATTRS, "number.ems_power": {"step": 1}}


@pytest.mark.parametrize("rated", [0.0, math.nan, -1.0])
def test_unknown_rated_power_uses_the_power_entity_range(rated):
    d, _ = tick(rated=rated, **FRESH)
    assert power(d) == 1172.0 and "sell_no_rated" not in d.notes


def test_unknown_rated_power_night_sell():
    d, _ = tick(rated=0.0, schedule=plan(slot("10:00", "11:00", **SELL, power_w=2000)),
                pv=0.0, pv_age=5.0, load=319.0, load_age=5.0)
    assert power(d) == 1681.0


def test_unknown_rated_power_never_exceeds_the_entity_max():
    attrs = {**ATTRS, "number.ems_power": {"min": 0, "max": 6000, "step": 1}}
    d, _ = tick(rated=0.0, attrs=attrs, schedule=plan(slot("10:00", "11:00", **SELL, power_w=5000)),
                pv=4000.0, pv_age=5.0, load=100.0, load_age=5.0)
    assert power(d) == 6000.0 and xset_notes(d)[0].endswith("xset=6000")


def test_unknown_rated_power_reads_the_entity_range_in_its_unit():
    units = {**UNITS, "power_w": "kW"}
    attrs = {**ATTRS, "number.ems_power": {"min": 0, "max": 3, "step": 0.1}}
    memory = ControlMemory.for_profile(GW)
    d = decide_cycle(
        profile=GW, schedule=plan(slot("10:00", "11:00", **SELL, power_w=5000)), now_utc=NOW, now_mono=1000.0,
        tele=Telemetry(soc=60.0, soc_age_s=10.0, battery_temp_c=25.0, pv_power_w=0.0, pv_age_s=5.0,
                       load_power_w=0.0, load_age_s=5.0),
        limits=Limits(rated_power_w=0.0),
        ents=EntityContext(domain="goodwe", mapped=MAPPED, units=units, attrs=attrs, readings={}),
        gates=OPEN, memory=memory)
    assert d.flat["power_w"] == 3000.0 and power(d) == 3.0


def test_known_rated_power_is_the_ceiling_and_the_entity_max_still_clips():
    small = tick(rated=1000.0, **FRESH)[0]
    assert power(small) == 1000.0
    attrs = {**ATTRS, "number.ems_power": {"min": 0, "max": 5000, "step": 1}}
    big = tick(rated=8000.0, attrs=attrs, schedule=plan(slot("10:00", "11:00", **SELL, power_w=7000)),
               pv=1000.0, pv_age=5.0, load=0.0, load_age=5.0)[0]
    assert xset_notes(big)[0].endswith("xset=8000") and power(big) == 5000.0 and "power_w" in big.adjusted


def test_no_rated_power_and_no_entity_range_suspends_selling_visibly():
    d, _ = tick(rated=0.0, attrs=NO_POWER_RANGE, **FRESH)
    # Bez zakresu encji mocy nic nie idzie (jak przy każdej intencji z mocą) — ale widać dlaczego.
    assert (d.status, d.reason, d.writes) == (BLOCKED, "entity_range_unknown", [])
    assert "sell_no_rated" in d.notes and d.live_export.xset_w == 0.0 and d.live_export.no_rated


@pytest.mark.parametrize("slot_kw", [
    dict(mode="charge", charge_source="grid", power_w=1500),
    dict(mode="discharge", power_w=2000),
    dict(mode="idle"),
])
def test_unknown_entity_range_blocks_other_powered_intents_exactly_as_before(slot_kw):
    sched = plan(slot("10:00", "11:00", price_pln_kwh=0.8, **slot_kw))
    plain, _ = tick(schedule=sched, rated=0.0, attrs=NO_POWER_RANGE)
    live, _ = tick(schedule=sched, rated=0.0, attrs=NO_POWER_RANGE, **FRESH)
    assert live == plain and (plain.status, plain.reason, plain.notes) == (BLOCKED, "entity_range_unknown", ())


# ── Histereza i pamięć zapisanej nastawy ──

def _written_tick(mem, **kw):
    d, mem = tick(mem, **kw)
    assert d.status == WRITE
    commit(d, written(d), mem, 1000.0)
    return d, mem


def test_commit_remembers_the_written_setpoint_for_the_slot():
    d, mem = _written_tick(None, **FRESH)
    assert mem.live_export.written_w == 1172.0
    assert mem.live_export.written_for == (at("10:00:00"), at("11:00:00"), "sell")


def test_small_change_keeps_the_written_setpoint_and_writes_nothing():
    _, mem = _written_tick(None, **FRESH)
    dev = {"mode": "sell_power", "power_w": 1172.0, "export_limit_w": 1520.0, "export_limit_enabled": 1.0}
    d, _ = tick(mem, pv=1100.0, pv_age=5.0, load=319.0, load_age=5.0, readings=dev, now_mono=1200.0)
    assert d.flat["power_w"] == 1172.0 and d.writes == [] and xset_notes(d)[0].endswith("xset=1172")


def test_change_of_150_w_or_more_is_written():
    _, mem = _written_tick(None, **FRESH)
    dev = {"mode": "sell_power", "power_w": 1172.0, "export_limit_w": 1520.0, "export_limit_enabled": 1.0}
    d, _ = tick(mem, pv=1128.0, pv_age=5.0, load=319.0, load_age=5.0, readings=dev, now_mono=1200.0)
    assert power(d) == 1322.0 and [w.key for w in d.writes] == ["power_w"]


def test_fresh_zero_is_never_held_by_hysteresis():
    mem = ControlMemory.for_profile(GW)
    _written_tick(mem, schedule=plan(slot("10:00", "11:00", **SELL, power_w=100)),
                  pv=0.0, pv_age=5.0, load=0.0, load_age=5.0)
    assert mem.live_export.written_w == 100.0
    d, _ = tick(mem, schedule=plan(slot("10:00", "11:00", **SELL, power_w=100)),
                pv=0.0, pv_age=5.0, load=200.0, load_age=5.0, now_mono=1200.0,
                readings={"mode": "sell_power", "power_w": 100.0})
    assert power(d) == 0.0


def test_slot_change_resets_the_written_setpoint():
    two = plan(slot("10:00", "10:30", **SELL, power_w=513, export_limit_w=1520),
               slot("10:30", "11:00", **SELL, power_w=513, export_limit_w=1520))
    mem = ControlMemory.for_profile(GW)
    _written_tick(mem, schedule=two, now_utc=at("10:10:00"), **FRESH)
    assert mem.live_export.written_w == 1172.0
    dev = {"mode": "sell_power", "power_w": 1172.0, "export_limit_w": 1520.0, "export_limit_enabled": 1.0}
    d, _ = tick(mem, schedule=two, now_utc=at("10:40:00"), pv=1100.0, pv_age=5.0, load=319.0,
                load_age=5.0, readings=dev, now_mono=1200.0)
    assert power(d) == 1294.0                  # w tym samym slocie zostałoby 1172
    assert mem.live_export.written_for is None


@pytest.mark.parametrize("report", [
    GroupReport(failed=["power_w"], ambiguous=["power_w"]),
    GroupReport(written=["power_w"], failed=["mode"], restored=["power_w"]),
    GroupReport(unsupported=["power_w"]),
    GroupReport(written=["power_w"], failed=["mode"], restore_failed=["power_w"]),
])
def test_failed_write_resets_the_written_setpoint(report):
    _, mem = _written_tick(None, **FRESH)
    d, _ = tick(mem, pv=1400.0, pv_age=5.0, load=319.0, load_age=5.0, now_mono=1200.0)
    commit(d, report, mem, 1200.0)
    assert mem.live_export.written_for is None and mem.live_export.written_w is None


def test_power_not_written_this_cycle_keeps_the_previous_setpoint():
    _, mem = _written_tick(None, **FRESH)
    d, _ = tick(mem, pv=1400.0, pv_age=5.0, load=319.0, load_age=5.0, now_mono=1200.0)
    commit(d, WriteReport(written=[]), mem, 1200.0)     # np. interwał I-6 — nic nie poszło
    assert mem.live_export.written_w == 1172.0


def test_dry_run_never_records_the_setpoint_as_written():
    mem = ControlMemory.for_profile(GW)
    before = copy.deepcopy(mem.live_export)
    d, _ = tick(mem, gates=replace(OPEN, consent=False), **FRESH)
    assert d.status == DRY_RUN and power(d) == 1172.0          # diagnostyka pokazuje przeliczenie
    commit(d, written(d), mem, 1000.0)                         # commit decyzji nie-WRITE nic nie robi
    d.summary()
    assert mem.live_export == before


def test_dry_run_does_not_touch_memory_of_a_live_session():
    _, mem = _written_tick(None, **FRESH)
    before = copy.deepcopy(mem.live_export)
    for pv, load in ((0.0, 5000.0), (None, None)):
        tick(mem, gates=replace(OPEN, local_switch=False), pv=pv, pv_age=5.0, load=load, load_age=5.0,
             schedule=plan(slot("10:00", "11:00", mode="self_consume")))
    assert mem.live_export == before


# ── Kiedy NIE przeliczamy ──

def test_reserve_guard_degraded_sell_is_not_converted():
    _, mem = _written_tick(None, **FRESH)
    d, _ = tick(mem, soc=5.0, readings={"mode": "sell_power"}, now_mono=1200.0, **FRESH)
    assert d.guard.invariant == "I-1" and d.flat["mode"] == "auto" and "power_w" not in d.flat
    assert not xset_notes(d) and mem.live_export.written_for is None


@pytest.mark.parametrize("slot_kw", [
    dict(mode="charge", charge_source="grid", power_w=1500),
    dict(mode="charge", charge_source="pv"),
    dict(mode="discharge", power_w=2000),                        # discharge_forced — moc "slot"
    dict(mode="discharge", discharge_purpose="self"),
    dict(mode="idle"),
    dict(mode="self_consume"),
])
def test_other_intents_ignore_live_readings(slot_kw):
    sched = plan(slot("10:00", "11:00", price_pln_kwh=0.8, **slot_kw))
    plain, _ = tick(schedule=sched)
    live, _ = tick(schedule=sched, **FRESH)
    assert live == plain and not xset_notes(live)


def test_sell_with_the_plain_slot_kind_is_not_converted():
    raw = copy.deepcopy(dict(GW.raw))
    raw = {**raw, "intents": {**raw["intents"], "sell": {"mode": "sell_power", "power": "slot"}}}
    legacy = profile_from_dict(copy.deepcopy(raw))
    sched = plan(slot("10:00", "11:00", **SELL, power_w=2000))
    d, _ = tick(schedule=sched, profile=legacy, **FRESH)
    assert power(d) == 2000.0 and not xset_notes(d) and NOTE_SELL_NO_LOAD not in d.notes


# ── Poniżej minimum encji mocy ──

@pytest.mark.parametrize("load", [1900.0, 5000.0])            # nastawa 100 W i 0 W przy minimum 200 W
def test_setpoint_below_entity_minimum_degrades_the_slot_to_neutral(load):
    attrs = {**ATTRS, "number.ems_power": {"min": 200, "max": 10000, "step": 1}}
    mem = ControlMemory.for_profile(GW)
    mem.live_export = replace(mem.live_export, written_for=(at("10:00:00"), at("11:00:00"), "sell"),
                              written_w=150.0)
    d, mem = tick(mem, schedule=plan(slot("10:00", "11:00", **SELL, power_w=2000)), attrs=attrs,
                  pv=0.0, pv_age=5.0, load=load, load_age=5.0)
    assert d.status == WRITE and d.flat["mode"] == "auto" and "power_w" not in d.flat
    assert "degraded" in d.notes and NOTE_SELL_BELOW_MIN in d.notes
    assert mem.live_export.written_for is None


def test_zero_setpoint_is_written_when_the_entity_minimum_is_zero():
    d, _ = tick(schedule=plan(slot("10:00", "11:00", **SELL, power_w=100)),
                pv=0.0, pv_age=5.0, load=500.0, load_age=5.0)
    assert d.flat["mode"] == "sell_power" and power(d) == 0.0 and "degraded" not in d.notes
