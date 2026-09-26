from custom_components.volcast.core.control.latch import ReserveLatch


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


def test_write_budget_bounded_for_any_amplitude():
    """1440 tików, SoC skacze 10↔40 co minutę: ~2 przełączenia na cykl ≥ 2,5 h (≈ 20/dobę)."""
    l = ReserveLatch()
    flips, prev = 0, None
    for i in range(1440):
        soc = 10.0 if i % 2 == 0 else 40.0
        cur = l.engaged(soc, 10.0, now=i * 60.0)
        flips += prev is not None and cur != prev
        prev = cur
    assert flips <= 24


def test_copy_does_not_mutate_original():
    l = ReserveLatch()
    l.engaged(10.0, 10.0, now=0.0)
    c = l.copy()
    c.engaged(50.0, 10.0, now=99999.0)
    assert l.is_engaged is True and c.is_engaged is False
