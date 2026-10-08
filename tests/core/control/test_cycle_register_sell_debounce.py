"""Sprzedaż w trybie bezpośrednim bez migania tryb neutralny ↔ wymuszony na chwilowym błędzie odczytu.

* pojedyncza nieważna próbka PV albo poboru: przez JEDEN cykl ostatnia ważna para (PV, pobór),
  dopiero druga z rzędu = slot w trybie neutralnym;
* puste okno szczytu (pierwszy cykl po restarcie albo > 10 min bez ważnych próbek): PV, które
  dopycha nastawę do mocy znamionowej, nie jest przyjmowane bez drugiej zgodnej próbki — nastawa
  bez tego PV (bateria − pobór), tryb sprzedaży zostaje.
"""
from __future__ import annotations

import pytest

from custom_components.volcast.core.control.cycle import ControlMemory
from custom_components.volcast.core.control.live_export import (NOTE_SELL_NO_READING, NOTE_SELL_PV_UNCONFIRMED,
                                                                NOTE_SELL_READING_HELD)

from .test_cycle_register_sell import RATED, _cycle


def _run(profile, memory, i, **kw):
    return _cycle(profile, memory=memory, now_mono=1000.0 + 60.0 * i, minute=i, **kw)[0]


@pytest.mark.parametrize("bad", [dict(pv=None), dict(load=None), dict(pv=2 * RATED + 1.0), dict(load_age=301.0)])
def test_single_invalid_sample_holds_the_last_valid_pair_for_one_cycle(goodwe_profile, bad):
    memory = ControlMemory.for_profile(goodwe_profile)
    first = _run(goodwe_profile, memory, 0, pv=200.0, load=700.0)
    assert first.flat["mode"] == "sell_power" and first.flat["power_w"] == pytest.approx(2500.0)
    held = _run(goodwe_profile, memory, 1, **bad)
    assert held.flat["mode"] == "sell_power" and held.flat["power_w"] == pytest.approx(2500.0)
    assert NOTE_SELL_READING_HELD in held.notes and "degraded" not in held.notes
    second = _run(goodwe_profile, memory, 2, **bad)
    assert second.flat["mode"] == goodwe_profile.neutral_mode and NOTE_SELL_NO_READING in second.notes


def test_valid_sample_between_blips_resets_the_hold(goodwe_profile):
    memory = ControlMemory.for_profile(goodwe_profile)
    for i, kw in enumerate([dict(), dict(pv=None), dict(), dict(pv=None), dict()]):
        d = _run(goodwe_profile, memory, i, **kw)
        assert d.flat["mode"] == "sell_power", (i, d.notes)


def test_invalid_sample_without_a_previous_valid_one_degrades_at_once(goodwe_profile):
    d = _run(goodwe_profile, ControlMemory.for_profile(goodwe_profile), 0, pv=None)
    assert d.flat["mode"] == goodwe_profile.neutral_mode and NOTE_SELL_NO_READING in d.notes


def test_empty_window_pv_pushing_to_the_rated_ceiling_needs_a_second_sample(goodwe_profile):
    memory = ControlMemory.for_profile(goodwe_profile)
    first = _run(goodwe_profile, memory, 0, power_w=5000.0, pv=4000.0, load=300.0)
    assert first.flat["mode"] == "sell_power" and NOTE_SELL_PV_UNCONFIRMED in first.notes
    assert first.flat["power_w"] == pytest.approx(4700.0)        # bez nieprzyjętego PV: bateria − dom
    second = _run(goodwe_profile, memory, 1, power_w=5000.0, pv=4000.0, load=300.0)
    assert second.flat["power_w"] == pytest.approx(RATED) and NOTE_SELL_PV_UNCONFIRMED not in second.notes


def test_empty_window_glitch_never_reaches_the_ceiling(goodwe_profile):
    # Restart, a pierwsza próbka PV to błąd (ponad zapas, ale w granicy wiarygodności 2 × znamionowa).
    d = _run(goodwe_profile, ControlMemory.for_profile(goodwe_profile), 0, power_w=3000.0, pv=12000.0, load=300.0)
    assert d.flat["power_w"] == pytest.approx(2700.0)


def test_empty_window_without_pv_push_is_accepted_at_once(goodwe_profile):
    # Bateria (przycięta do mocy znamionowej) − dom: PV nic nie dokłada — nie ma czego potwierdzać.
    d = _run(goodwe_profile, ControlMemory.for_profile(goodwe_profile), 0, power_w=9000.0, pv=0.0, load=300.0)
    assert d.flat["power_w"] == pytest.approx(RATED - 300.0) and NOTE_SELL_PV_UNCONFIRMED not in d.notes


def test_window_after_ten_minutes_without_valid_samples_counts_as_empty(goodwe_profile):
    memory = ControlMemory.for_profile(goodwe_profile)
    _run(goodwe_profile, memory, 0, power_w=5000.0, pv=4000.0, load=300.0)
    late = _run(goodwe_profile, memory, 11, power_w=5000.0, pv=4000.0, load=300.0)   # 11 min później
    assert NOTE_SELL_PV_UNCONFIRMED in late.notes and late.flat["power_w"] == pytest.approx(4700.0)


def test_empty_window_pv_pushing_to_the_export_limit_needs_a_second_sample(goodwe_profile):
    # Limit eksportu poniżej mocy znamionowej: pułap to min(znamionowa, limit), nie sama znamionowa.
    memory = ControlMemory.for_profile(goodwe_profile)
    first = _run(goodwe_profile, memory, 0, power_w=2500.0, pv=4000.0, load=300.0, export_limit_w=3000)
    assert first.flat["mode"] == "sell_power" and NOTE_SELL_PV_UNCONFIRMED in first.notes
    assert first.flat["power_w"] == pytest.approx(2200.0)        # bez nieprzyjętego PV: bateria − dom
    second = _run(goodwe_profile, memory, 1, power_w=2500.0, pv=4000.0, load=300.0, export_limit_w=3000)
    assert second.flat["power_w"] == pytest.approx(3000.0) and NOTE_SELL_PV_UNCONFIRMED not in second.notes


def test_old_valid_pair_is_not_held(goodwe_profile):
    # Ostatnia ważna para sprzed kilku cykli (cykle nie dochodziły do decyzji) — nie przetrzymujemy jej.
    memory = ControlMemory.for_profile(goodwe_profile)
    _run(goodwe_profile, memory, 0, pv=200.0, load=700.0)
    late = _run(goodwe_profile, memory, 5, pv=None)              # 5 min później
    assert late.flat["mode"] == goodwe_profile.neutral_mode and NOTE_SELL_NO_READING in late.notes
    assert NOTE_SELL_READING_HELD not in late.notes


def test_held_pair_expires_after_its_max_age():
    from custom_components.volcast.core.control.live_export import HELD_PAIR_MAX_AGE_S, LiveExportMemory
    mem = LiveExportMemory().with_pair(200.0, 700.0, 1000.0).with_pair(None, None, 1060.0)
    assert mem.held_pair(1060.0) == (200.0, 700.0)
    assert mem.held_pair(1000.0 + HELD_PAIR_MAX_AGE_S) is None
