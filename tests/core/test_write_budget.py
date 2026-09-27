"""Budżet zapisów do pamięci nieulotnej: okno kroczące na zegarze ściennym, odporne na skoki zegara."""
import logging
from types import SimpleNamespace

import pytest

from custom_components.volcast.core.guard_state import WriteBudget
from custom_components.volcast.core.profile import load_builtin

DAY = 86400.0
NOW = 1_790_000_000.0            # wrzesień 2026
Y2030 = 1_900_000_000.0


def test_per_key_limit():
    b = WriteBudget(per_key=3, total=100)
    for i in range(3):
        assert b.exhausted(["power_w", "mode"], NOW + i) == set()
        b.note("power_w", NOW + i)
    assert b.exhausted(["power_w", "mode"], NOW + 10) == {"power_w"}
    assert b.hit is True


def test_total_limit_across_keys():
    b = WriteBudget(per_key=10, total=4)
    for i, k in enumerate(["a", "b", "c", "d"]):
        b.note(k, NOW + i)
    assert b.exhausted(["a", "e"], NOW + 5) == {"a", "e"}


def test_hit_false_until_exhausted():
    b = WriteBudget(per_key=2, total=10)
    b.note("a", NOW)
    assert b.exhausted(["a"], NOW) == set() and b.hit is False


def test_window_slides():
    b = WriteBudget(per_key=2, total=10, window_s=DAY)
    b.note("a", NOW)
    b.note("a", NOW + 100)
    assert b.exhausted(["a"], NOW + DAY - 1) == {"a"}
    assert b.exhausted(["a"], NOW + DAY + 1) == set()        # pierwszy wpis wypadł z okna
    assert b.exhausted(["a"], NOW + DAY + 101) == set()


def test_clock_backwards_keeps_entries():
    b = WriteBudget(per_key=2, total=10)
    b.note("a", NOW)
    b.note("a", NOW + 60)
    assert b.exhausted(["a"], NOW - 3600) == {"a"}           # cofnięty zegar nie kasuje wpisów
    assert b.exhausted(["a"], NOW - 5 * DAY) == {"a"}


def test_clock_forward_glitch_does_not_disable_budget(caplog):
    b = WriteBudget(per_key=50, total=1000)
    b.note("a", Y2030)                                      # zegar skoczył o lata do przodu
    with caplog.at_level(logging.WARNING):
        for i in range(49):
            assert b.exhausted(["a"], NOW + i) == set()
            b.note("a", NOW + i)
    assert b.exhausted(["a"], NOW + 60) == {"a"}             # wpis „z przyszłości” policzony dziś
    assert str(int(Y2030)) not in caplog.text and str(int(NOW)) not in caplog.text
    assert b.exhausted(["a"], NOW + DAY + 100) == set()      # i wypada po oknie, nie po latach
    assert all(ts <= NOW + DAY + 100 + 300 for _, ts in b.to_list())


def test_future_entries_clamped_on_load():
    raw = [["a", NOW + 5 * 365 * DAY]] * 3
    b = WriteBudget.from_list(raw, per_key=3, total=10, window_s=DAY, now_wall=NOW)
    assert b.exhausted(["a"], NOW + 1) == {"a"}
    assert b.exhausted(["a"], NOW + DAY + 1) == set()
    assert all(ts <= NOW + 300 for _, ts in b.to_list())


def test_to_list_never_later_than_last_now_plus_5_min():
    b = WriteBudget(per_key=10, total=10)
    b.note("a", Y2030)
    b.exhausted(["a"], NOW)
    assert all(ts <= NOW + 300 for _, ts in b.to_list())


def test_roundtrip_through_list():
    b = WriteBudget(per_key=5, total=20)
    for i, k in enumerate(["mode", "power_w", "tou.3.soc", "mode"]):
        b.note(k, NOW + i)
    c = WriteBudget.from_list(b.to_list(), per_key=5, total=20, window_s=DAY, now_wall=NOW + 10)
    assert c.to_list() == b.to_list()
    assert c.to_list() == [["mode", NOW], ["power_w", NOW + 1], ["tou.3.soc", NOW + 2], ["mode", NOW + 3]]


def test_old_entries_dropped_on_load():
    b = WriteBudget.from_list([["a", NOW - 2 * DAY], ["a", NOW - 10]], per_key=5, total=5,
                              window_s=DAY, now_wall=NOW)
    assert b.to_list() == [["a", NOW - 10]]


@pytest.mark.parametrize("raw", [
    None, "x", 5, {"a": 1},
    [["a"], ["a", "b"], [1, NOW], ["a", float("nan")], ["a", float("inf")], ["a", True], "zz", None,
     ["a", NOW, 3], ["", NOW]],
])
def test_garbage_list_ignored(raw):
    b = WriteBudget.from_list(raw, per_key=5, total=5, window_s=DAY, now_wall=NOW)
    assert b.to_list() == []


def test_garbage_entries_skipped_good_ones_kept():
    b = WriteBudget.from_list([["a", NOW], "junk", ["b", "x"], ["c", NOW - 1]], per_key=5, total=5,
                              window_s=DAY, now_wall=NOW)
    assert [k for k, _ in b.to_list()] == ["c", "a"]          # posortowane po czasie


def test_memory_capped():
    b = WriteBudget(per_key=1000, total=10)
    for i in range(100):
        b.note("a", NOW + i)
    assert len(b.to_list()) == 10 + 16
    assert b.exhausted(["a"], NOW + 200) == {"a"}


def test_tou_fields_counted_per_field():
    b = WriteBudget(per_key=1, total=10)
    b.note("tou.3.soc", NOW)
    assert b.exhausted(["tou.3.soc", "tou.3.power_w"], NOW) == {"tou.3.soc"}


def test_for_profile_none_without_budget():
    assert WriteBudget.for_profile(SimpleNamespace(nvm_budget=None)) is None


def test_goodwe_profile_budget_values():
    b = WriteBudget.for_profile(load_builtin("goodwe-et"))
    assert (b.per_key, b.total, b.window_s) == (144, 600, DAY)
    d = WriteBudget.for_profile(load_builtin("deye-sg"))
    assert (d.per_key, d.total) == (48, 400)


@pytest.mark.parametrize("kw", [{"per_key": 0, "total": 5}, {"per_key": 5, "total": 0},
                                {"per_key": 1, "total": 1, "window_s": 0}])
def test_bad_limits_rejected(kw):
    with pytest.raises(ValueError):
        WriteBudget(**kw)
