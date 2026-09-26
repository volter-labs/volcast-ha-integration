import math

import pytest

from custom_components.volcast.core.control.latch import ReserveLatch

BAD_PERCENT = [None, math.nan, math.inf, -math.inf, -0.5, 100.5]
BAD_TIME = [None, math.nan, math.inf, -math.inf]


def test_engages_at_reserve_and_holds_inside_band():
    l = ReserveLatch()
    assert l.engaged(10.0, 10.0, now=0.0) is True          # nieostro: równość = ochrona
    assert l.engaged(12.0, 10.0, now=60.0) is True         # w paśmie 3 pp — dalej trzyma
    assert l.engaged(14.0, 10.0, now=120.0) is True        # nad pasmem, ale < 30 min trwania


def test_releases_after_min_duration_above_band():
    l = ReserveLatch()
    l.engaged(10.0, 10.0, now=0.0)
    assert l.engaged(14.0, 10.0, now=1801.0) is False


def test_deep_drop_engages_immediately_even_right_after_release():
    l = ReserveLatch()
    l.engaged(10.0, 10.0, now=0.0)
    l.engaged(14.0, 10.0, now=1801.0)                      # zwolniony
    assert l.engaged(6.0, 10.0, now=1860.0) is True        # < rezerwa − 3 pp: natychmiast


def test_shallow_dip_right_after_release_does_not_reengage():
    l = ReserveLatch()
    l.engaged(10.0, 10.0, now=0.0)
    l.engaged(14.0, 10.0, now=1801.0)
    assert l.engaged(9.0, 10.0, now=1900.0) is False       # płytkie zejście < 2 h od zwolnienia


@pytest.mark.parametrize("low", [10.0, 5.0, 0.0])
def test_write_budget_bounded_for_any_amplitude(low):
    """1440 tików, SoC skacze low↔40 co minutę: ~2 przełączenia na cykl ≥ 2,5 h (≈ 20/dobę).

    `low` przebiega zarówno ścieżkę płytką (== rezerwa), jak i głęboką (< rezerwa −
    deep_pp) — reguła 3 (minimalny odstęp między zwolnieniami) chroni budżet zapisów
    NVM tylko na ścieżce głębokiej; test z samym `low=10.0` (płytki) tego nie widzi.
    """
    l = ReserveLatch()
    flips, prev = 0, None
    for i in range(1440):
        soc = low if i % 2 == 0 else 40.0
        cur = l.engaged(soc, 10.0, now=i * 60.0)
        flips += prev is not None and cur != prev
        prev = cur
    assert flips <= 24


def test_band_edge_at_reserve_plus_band_releases_after_dwell():
    l = ReserveLatch()
    l.engaged(10.0, 10.0, now=0.0)
    assert l.engaged(13.0, 10.0, now=1801.0) is False      # dokładnie rezerwa+pasmo — poza pasmem


def test_band_edge_just_inside_band_holds_after_dwell():
    l = ReserveLatch()
    l.engaged(10.0, 10.0, now=0.0)
    assert l.engaged(12.999, 10.0, now=1801.0) is True     # tuż wewnątrz pasma — trzyma mimo dwell


def test_released_edge_at_deep_threshold_stays_shallow_suppressed():
    l = ReserveLatch()
    l.engaged(10.0, 10.0, now=0.0)
    l.engaged(14.0, 10.0, now=1801.0)                      # zwolniony
    assert l.engaged(7.0, 10.0, now=1802.0) is False       # dokładnie rezerwa−deep — to jeszcze płytko


def test_released_edge_just_past_deep_threshold_engages_immediately():
    l = ReserveLatch()
    l.engaged(10.0, 10.0, now=0.0)
    l.engaged(14.0, 10.0, now=1801.0)                      # zwolniony
    assert l.engaged(6.999, 10.0, now=1802.0) is True      # tuż pod progiem głębokim — natychmiast


def test_fresh_latch_shallow_dip_engages_immediately():
    l = ReserveLatch()
    assert l.engaged(9.0, 10.0, now=0.0) is True           # brak historii — płytkie zejście nie czeka


def test_copy_does_not_mutate_original():
    l = ReserveLatch()
    l.engaged(10.0, 10.0, now=0.0)
    c = l.copy()
    c.engaged(50.0, 10.0, now=99999.0)
    assert l.is_engaged is True and c.is_engaged is False


def test_copy_carries_timestamps_and_original_stays_untouched():
    l = ReserveLatch()
    l.engaged(10.0, 10.0, now=0.0)                         # zalaczony, since=0
    l.engaged(14.0, 10.0, now=1801.0)                      # zwolniony, since=1801, last_release=1801
    c = l.copy()
    assert c._since == l._since == 1801.0
    assert c._last_release == l._last_release == 1801.0
    # bez skopiowanych znaczników kopia potraktowałaby to jak świeży zatrzask i
    # od razu włączyłaby płytkie zejście — z nimi zostaje stłumiona jak oryginał.
    assert c.engaged(9.0, 10.0, now=1900.0) is False
    assert l._since == 1801.0
    assert l._last_release == 1801.0
    assert l.is_engaged is False


@pytest.mark.parametrize("bad_soc", BAD_PERCENT)
def test_invalid_soc_fails_closed_from_engaged_state(bad_soc):
    l = ReserveLatch()
    l.engaged(10.0, 10.0, now=0.0)                         # zalaczony
    since, last_release, was_engaged = l._since, l._last_release, l.is_engaged
    assert l.engaged(bad_soc, 10.0, now=100.0) is True
    assert l.is_engaged == was_engaged
    assert l._since == since
    assert l._last_release == last_release


@pytest.mark.parametrize("bad_soc", BAD_PERCENT)
def test_invalid_soc_fails_closed_from_released_state(bad_soc):
    l = ReserveLatch()
    l.engaged(10.0, 10.0, now=0.0)
    l.engaged(14.0, 10.0, now=1801.0)                      # zwolniony
    since, last_release, was_engaged = l._since, l._last_release, l.is_engaged
    assert l.engaged(bad_soc, 10.0, now=1900.0) is True
    assert l.is_engaged == was_engaged
    assert l._since == since
    assert l._last_release == last_release


@pytest.mark.parametrize("bad_reserve", BAD_PERCENT)
def test_invalid_reserve_fails_closed_from_engaged_state(bad_reserve):
    l = ReserveLatch()
    l.engaged(10.0, 10.0, now=0.0)
    since, last_release, was_engaged = l._since, l._last_release, l.is_engaged
    assert l.engaged(10.0, bad_reserve, now=100.0) is True
    assert l.is_engaged == was_engaged
    assert l._since == since
    assert l._last_release == last_release


@pytest.mark.parametrize("bad_reserve", BAD_PERCENT)
def test_invalid_reserve_fails_closed_from_released_state(bad_reserve):
    l = ReserveLatch()
    l.engaged(10.0, 10.0, now=0.0)
    l.engaged(14.0, 10.0, now=1801.0)                      # zwolniony
    since, last_release, was_engaged = l._since, l._last_release, l.is_engaged
    assert l.engaged(10.0, bad_reserve, now=1900.0) is True
    assert l.is_engaged == was_engaged
    assert l._since == since
    assert l._last_release == last_release


@pytest.mark.parametrize("bad_now", BAD_TIME)
def test_invalid_now_fails_closed_from_engaged_state(bad_now):
    l = ReserveLatch()
    l.engaged(10.0, 10.0, now=0.0)
    since, last_release, was_engaged = l._since, l._last_release, l.is_engaged
    assert l.engaged(10.0, 10.0, now=bad_now) is True
    assert l.is_engaged == was_engaged
    assert l._since == since
    assert l._last_release == last_release


@pytest.mark.parametrize("bad_now", BAD_TIME)
def test_invalid_now_fails_closed_from_released_state(bad_now):
    l = ReserveLatch()
    l.engaged(10.0, 10.0, now=0.0)
    l.engaged(14.0, 10.0, now=1801.0)                      # zwolniony
    since, last_release, was_engaged = l._since, l._last_release, l.is_engaged
    assert l.engaged(10.0, 10.0, now=bad_now) is True
    assert l.is_engaged == was_engaged
    assert l._since == since
    assert l._last_release == last_release
