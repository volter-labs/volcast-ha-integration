"""Cel rejestrowy: nastawa bez rejestru (sonda: brak, nieczytelna, niezweryfikowana) degraduje akcję
dokładnie jak brak encji w trybie encji (`cycle._degrade`) — tabela: akcja × brakująca nastawa."""
import pytest

from custom_components.volcast.core.control.cycle import (IDLE, WRITE, ControlMemory, Gates, Limits, Telemetry,
                                                          decide_cycle)
from custom_components.volcast.core.control.target import RegisterTarget
from tests.core.golden import T0

from .conftest import LIVE_ZERO, MODE_REG, SOC_REG, goodwe_reading, one_slot

SLOTS = {
    "charge_grid": dict(mode="charge", charge_source="grid", power_w=3000, soc_target=90),
    "discharge_forced": dict(mode="discharge", power_w=2500, soc_target=40),
    "sell": dict(mode="discharge", discharge_purpose="sell", power_w=2500, soc_target=40),
    "standby": dict(mode="idle"),
    "self_consume": dict(mode="self_consume"),
    "charge_pv": dict(mode="charge", charge_source="pv"),
}
MODE_OF = {"charge_grid": "charge_battery", "discharge_forced": "discharge_battery", "sell": "sell_power",
           "standby": "battery_standby", "self_consume": "auto", "charge_pv": "auto"}
PAIR = ("export_limit_w", "export_limit_enabled")
NEUTRAL = "neutral"
# Tabela z rozpoznania (sekcja d): brakująca nastawa → akcja idzie (`goes`) albo schodzi do trybu neutralnego.
TABLE = {
    "charge_grid":      {"power_w": NEUTRAL, "soc_min": "goes", "export_limit_w": "goes", "soc_max": "goes"},
    "discharge_forced": {"power_w": NEUTRAL, "soc_min": NEUTRAL, "export_limit_w": "goes", "soc_max": "goes"},
    "sell":             {"power_w": NEUTRAL, "soc_min": NEUTRAL, "export_limit_w": "goes", "soc_max": "goes"},
    "standby":          {"power_w": NEUTRAL, "soc_min": "goes", "export_limit_w": "goes", "soc_max": "goes"},
    "self_consume":     {"power_w": "goes", "soc_min": "goes", "export_limit_w": "goes", "soc_max": "goes"},
    "charge_pv":        {"power_w": "goes", "soc_min": "goes", "export_limit_w": "goes", "soc_max": "goes"},
}


def _run(profile, schedule, *, unavailable=frozenset(), unreadable=frozenset()):
    # Falownik w trybie wymuszonym właściciela (discharge_battery) — każdy plan coś zmienia.
    reading = goodwe_reading(profile, **{str(MODE_REG): 12, str(SOC_REG): 80})
    memory = ControlMemory.for_profile(profile)
    memory.unsupported = set(unreadable) | set(unavailable)
    return decide_cycle(profile=profile, schedule=schedule, now_utc=T0, now_mono=1000.0,
                        tele=Telemetry(soc=80.0, soc_age_s=5.0, battery_temp_c=25.0, **LIVE_ZERO),
                        limits=Limits(rated_power_w=8000.0),
                        gates=Gates(consent=True, local_switch=True, control_mode="direct", verified=True),
                        memory=memory,
                        target=RegisterTarget(reading, unreadable=unreadable, unavailable=unavailable))


def test_missing_keys_follow_write_order_and_include_unreadable(goodwe_profile, reading_auto):
    t = RegisterTarget(reading_auto, unreadable=frozenset({"soc_max"}),
                       unavailable=frozenset({"mode", "export_limit_enabled", "not_a_write_key"}))
    assert t.missing_keys(goodwe_profile) == ("soc_max", "export_limit_enabled", "mode")
    assert RegisterTarget(reading_auto).missing_keys(goodwe_profile) == ()


@pytest.mark.parametrize("action", sorted(TABLE))
@pytest.mark.parametrize("key", ["power_w", "soc_min", "export_limit_w", "soc_max"])
def test_action_degrades_per_missing_setting(goodwe_profile, action, key):
    missing = frozenset(PAIR) if key == "export_limit_w" else frozenset({key})
    d = _run(goodwe_profile, one_slot(**SLOTS[action]), unavailable=missing)
    keys = {w.key for w in d.writes}
    assert d.status == WRITE, (d.status, d.reason)
    assert not missing & keys and not missing & set(d.flat)
    if TABLE[action][key] == NEUTRAL:
        assert d.flat["mode"] == "auto" and "degraded" in d.notes and "power_w" not in d.flat
    else:
        assert d.flat["mode"] == MODE_OF[action] and "degraded" not in d.notes


@pytest.mark.parametrize("action", ["discharge_forced", "sell"])
def test_discharge_with_export_ban_degrades_without_the_export_pair(goodwe_profile, action):
    ban = one_slot(**SLOTS[action], export_allowed=False)
    d = _run(goodwe_profile, ban, unavailable=frozenset({"export_limit_enabled"}))
    assert d.status == WRITE and d.flat["mode"] == "auto" and "degraded" in d.notes
    assert not set(PAIR) & {w.key for w in d.writes}


def test_charge_with_export_ban_without_the_pair_still_charges(goodwe_profile):
    d = _run(goodwe_profile, one_slot(**SLOTS["charge_grid"], export_allowed=False),
             unavailable=frozenset({"export_limit_w"}))
    assert d.status == WRITE and d.flat["mode"] == "charge_battery" and "degraded" not in d.notes


@pytest.mark.parametrize("action", sorted(TABLE))
def test_missing_mode_register_means_no_control(goodwe_profile, action):
    d = _run(goodwe_profile, one_slot(**SLOTS[action]), unavailable=frozenset({"mode"}))
    assert (d.status, d.reason) == (IDLE, "missing_entities") and d.writes == []
    assert "mode" in d.unmapped


def test_unreadable_soc_max_degrades_nothing(goodwe_profile):
    # GW8KN-ET nie ma rejestru 47760 — górny próg wypada, ładowanie idzie.
    d = _run(goodwe_profile, one_slot(**SLOTS["charge_grid"]), unreadable=frozenset({"soc_max"}))
    assert d.status == WRITE and d.flat["mode"] == "charge_battery" and "degraded" not in d.notes
    assert "soc_max" in d.dropped_unsupported and {"mode", "power_w"} <= {w.key for w in d.writes}
