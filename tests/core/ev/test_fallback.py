"""Reguła awaryjna ładowarki: pełna moc, gdy plan jest stary, a termin blisko."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.volcast.core.ev.fallback import (
    FALLBACK_MARGIN_S, FALLBACK_PLAN_AGE_S, should_force)
from custom_components.volcast.core.ev.model import LoadsMeta

NOW = datetime(2026, 9, 23, 4, 0, tzinfo=timezone.utc)
OLD = FALLBACK_PLAN_AGE_S + 1


def _meta(**kw):
    # 11 kWh przy 11 kW = 1 h ładowania; termin 06:00 → okno awaryjne od 04:30
    base = dict(session_id="s1", load_key="dev1", control_enabled=True, fallback_allowed=True, remaining_kwh=11.0,
                max_power_w=11000.0, ready_by=NOW + timedelta(hours=2), plan_generated_at=NOW - timedelta(hours=8))
    base.update(kw)
    return LoadsMeta(**base)


def test_thresholds():
    assert FALLBACK_PLAN_AGE_S == 90 * 60 and FALLBACK_MARGIN_S == 30 * 60


@pytest.mark.parametrize("now,age,expected", [
    (NOW + timedelta(minutes=30), OLD, True),                  # dokładnie na granicy okna
    (NOW + timedelta(minutes=29), OLD, False),                 # minutę przed oknem
    (NOW + timedelta(hours=3), OLD, True),                     # po terminie, cel dalej nieosiągnięty
    (NOW + timedelta(minutes=45), FALLBACK_PLAN_AGE_S, False),  # plan nie STARSZY niż 90 min
    (NOW + timedelta(minutes=45), 60.0, False),                # świeży plan
])
def test_window_and_plan_age(now, age, expected):
    assert should_force(now, _meta(), age, "Available") is expected


@pytest.mark.parametrize("kw", [
    dict(remaining_kwh=0.0), dict(remaining_kwh=-5.0), dict(remaining_kwh=None),
    dict(max_power_w=0.0), dict(max_power_w=-1.0), dict(max_power_w=None),
    dict(ready_by=None), dict(control_enabled=False),
    dict(remaining_kwh=float("nan")), dict(max_power_w=float("inf")),
])
def test_invalid_or_finished_meta_never_forces(kw):
    # zły kształt metadanych = brak wymuszenia (nigdy dzielenie przez zero)
    assert should_force(NOW + timedelta(hours=3), _meta(**kw), OLD, "Available") is False


def test_zero_step_meta_does_not_matter():
    assert should_force(NOW + timedelta(hours=1), _meta(step=0.0, min=0.0), OLD, "Available") is True


@pytest.mark.parametrize("status", [None, "unavailable", "unknown", 5])
def test_unreadable_status_never_forces(status):
    assert should_force(NOW + timedelta(hours=1), _meta(), OLD, status) is False


@pytest.mark.parametrize("age", [None, float("nan"), float("inf"), -10.0, "x", True])
def test_unknown_plan_age_never_forces(age):
    assert should_force(NOW + timedelta(hours=1), _meta(), age, "Available") is False


def test_no_meta_or_naive_now_never_forces():
    assert should_force(NOW + timedelta(hours=1), None, OLD, "Available") is False
    assert should_force(datetime(2026, 9, 23, 5, 0), _meta(), OLD, "Available") is False


def test_absurd_need_is_already_in_window():
    # 10^6 kWh przy 1 W: przelicznik bez przepełnienia daty, okno trwa
    assert should_force(NOW, _meta(remaining_kwh=1e6, max_power_w=1.0), OLD, "Available") is True


def test_fallback_not_allowed_by_the_cloud_never_forces():
    # pauza użytkownika albo „tylko PV”: chmura zabrania ładowania awaryjnego z sieci
    assert should_force(NOW + timedelta(hours=1), _meta(fallback_allowed=False), OLD, "Available") is False
    bare = LoadsMeta(session_id="s1", load_key="dev1", control_enabled=True, remaining_kwh=11.0,
                     max_power_w=11000.0, ready_by=NOW)
    assert bare.fallback_allowed is False                  # domyślnie zamknięte
    assert should_force(NOW + timedelta(hours=1), bare, OLD, "Available") is False


@pytest.mark.parametrize("raw,expected", [
    ({"fallback_allowed": True}, True), ({"fallback_allowed": False}, False), ({}, False),
    ({"fallback_allowed": "true"}, False), ({"fallback_allowed": 1}, False), ({"fallback_allowed": None}, False),
])
def test_parse_fallback_allowed_is_strict(raw, expected):
    from custom_components.volcast.core.ev.model import parse_loads
    _, meta = parse_loads({"loads": [], "loads_meta": {"session_id": "s1", "load_key": "dev1", **raw}})
    assert meta.fallback_allowed is expected


def test_window_scales_with_power():
    # 22 kWh przy 11 kW = 2 h → okno od 03:30 (już trwa o 04:00); przy 22 kW = 1 h → od 04:30
    assert should_force(NOW, _meta(remaining_kwh=22.0), OLD, "Available") is True
    assert should_force(NOW, _meta(remaining_kwh=22.0, max_power_w=22000.0), OLD, "Available") is False
