"""Nastawa eksportu slotu sprzedaży liczona z odczytów — parytet z implementacją referencyjną."""
from __future__ import annotations

import pytest

from custom_components.volcast.core.engines.sell_xset import SELL_XSET_HYSTERESIS_W, sell_xset
from tests.core.golden import load_golden

GOLDEN = load_golden("sell")
VECTORS = GOLDEN["vectors"]
RATED = 8000.0
NAN = float("nan")
INF = float("inf")


def _num(v):
    """Wartość z wektora: null → None, napisy "nan"/"inf"/"-inf" → float niefinitywny."""
    return float(v) if isinstance(v, str) else v


def test_vectors_cover_reference_table():
    assert len(VECTORS) == 36
    assert GOLDEN["hysteresis_w"] == SELL_XSET_HYSTERESIS_W


@pytest.mark.parametrize("vec", VECTORS, ids=[v["id"] for v in VECTORS])
def test_matches_reference_sell_xset(vec):
    got = sell_xset(
        battery_w=_num(vec["battery_w"]),
        pv_w=_num(vec["pv_w"]),
        load_w=_num(vec["load_w"]),
        readings_ok=vec["readings_ok"],
        last_known_load_w=_num(vec["last_load_w"]),
        export_limit_w=_num(vec["export_limit_w"]),
        rated_power_w=_num(vec["rated_w"]),
        prev_xset_w=_num(vec["prev_xset_w"]),
    )
    assert got == pytest.approx(vec["xset_w"], abs=1e-3)


def _x(battery=1000.0, pv=500.0, load=200.0, ok=True, last=None, limit=4300.0,
       rated=RATED, prev=None):
    return sell_xset(
        battery_w=battery, pv_w=pv, load_w=load, readings_ok=ok, last_known_load_w=last,
        export_limit_w=limit, rated_power_w=rated, prev_xset_w=prev,
    )


def test_keyword_only():
    with pytest.raises(TypeError):
        sell_xset(1000.0, 500.0, 200.0, True, None, 4300.0, RATED, None)  # type: ignore[misc]


def test_fresh_readings_battery_plus_pv_minus_load():
    assert _x(3000, 0, 500) == 2500
    assert _x(513, 978, 319, limit=1520) == 1172


def test_load_above_battery_and_pv_gives_zero():
    assert _x(300, 100, 900) == 0


def test_ceiling_is_min_of_export_limit_and_rated():
    assert _x(3000, 4000, 200, limit=4300) == 4300
    assert _x(5000, 5000, 0, limit=None) == RATED
    assert _x(5000, 5000, 0, limit=9000) == RATED
    assert _x(1000, 500, 200, limit=0) == 0


def test_export_limit_none_is_no_limit():
    assert _x(limit=None) == 1300


@pytest.mark.parametrize("limit", [NAN, INF, -INF, -50.0])
def test_non_finite_or_negative_export_limit_fails_closed(limit):
    assert _x(limit=limit) == 0


@pytest.mark.parametrize("rated", [None, NAN, INF, 0.0, -5.0])
def test_unknown_or_non_positive_rated_gives_zero(rated):
    assert _x(3000, 1000, 200, limit=None, rated=rated) == 0


def test_fallback_uses_last_known_load_when_readings_not_ok():
    assert _x(1000, None, None, ok=False, last=400) == 600
    # readings_ok=False wygrywa nawet przy obecnych liczbach PV/domu
    assert _x(1000, 5000, 0, ok=False, last=400) == 600


def test_fallback_without_known_load_gives_zero():
    assert _x(1000, None, None, ok=False, last=None) == 0


@pytest.mark.parametrize("pv,load", [(None, 200.0), (500.0, None), (NAN, 200.0),
                                     (500.0, NAN), (INF, 200.0), (500.0, -INF)])
def test_missing_or_non_finite_pv_load_uses_fallback(pv, load):
    assert _x(1000, pv, load, ok=True, last=400) == 600
    assert _x(1000, pv, load, ok=True, last=None) == 0


def test_fallback_is_capped_and_never_negative():
    assert _x(3000, None, None, ok=False, last=100, limit=1520) == 1520
    assert _x(300, None, None, ok=False, last=900) == 0


def test_negative_inputs_count_as_zero():
    assert _x(-1000, 500, 200) == 300
    assert _x(1000, -500, 200) == 800
    assert _x(1000, 500, -200) == 1500
    assert _x(1000, None, None, ok=False, last=-400) == 1000


def test_non_finite_battery_counts_as_zero_and_continues():
    assert _x(NAN, 500, 200) == 300
    assert _x(INF, 500, 200) == 300
    assert _x(None, 500, 200) == 300


def test_non_finite_last_known_load_is_unknown():
    assert _x(1000, None, None, ok=False, last=NAN) == 0
    assert _x(1000, None, None, ok=False, last=INF) == 0


def test_hysteresis_constant():
    assert SELL_XSET_HYSTERESIS_W == 150.0


def test_hysteresis_keeps_previous_within_band():
    assert _x(513, 1056, 319, limit=1520, prev=1172) == 1172
    assert _x(513, 829, 319, limit=1520, prev=1172) == 1172  # świeża niższa o 149
    assert _x(300, 100, 300, prev=0) == 0  # prev = 0 mieści się w [0, pułap]


def test_hysteresis_releases_at_or_above_band():
    assert _x(513, 1206, 319, limit=1520, prev=1172) == 1400
    assert _x(513, 1128, 319, limit=1520, prev=1172) == 1322  # różnica dokładnie 150
    assert _x(513, 828, 319, limit=1520, prev=1172) == 1022


def test_hysteresis_does_not_keep_previous_above_ceiling():
    assert _x(513, 978, 319, limit=1100, prev=1172) == 1100


def test_hysteresis_keeps_previous_equal_to_ceiling():
    assert _x(513, 1226, 319, limit=1520, prev=1520) == 1520
    assert _x(513, 1200, 319, limit=1520, prev=1520) == 1520  # świeża 1394, różnica 126


def test_hysteresis_does_not_keep_negative_or_non_finite_previous():
    assert _x(513, 978, 319, limit=1520, prev=-50) == 1172
    assert _x(prev=NAN) == 1300
    assert _x(prev=INF) == 1300


def test_hysteresis_does_not_keep_nonzero_previous_when_fresh_is_zero():
    assert _x(300, 100, 900, prev=100) == 0
    assert _x(1000, None, None, ok=False, last=None, prev=100) == 0
    assert _x(300, None, None, ok=False, last=900, prev=100) == 0


def test_hysteresis_applies_in_fallback_branch():
    assert _x(1000, None, None, ok=False, last=400, prev=650) == 650


def test_result_is_not_rounded():
    assert _x(1000.4, 0.3, 0.2, limit=None) == pytest.approx(1000.5)
