from datetime import datetime, timezone

from custom_components.volcast.core.ev.convert import amps_to_watts, slot_for, watts_to_amps
from custom_components.volcast.core.ev.model import LoadSlot

UTC = timezone.utc


def _slot(h_from, h_to, mode="forced"):
    return LoadSlot(device_id="d", start=datetime(2026, 10, 1, h_from, tzinfo=UTC),
                    end=datetime(2026, 10, 1, h_to, tzinfo=UTC), mode=mode,
                    power_w=None, min_power_w=None)


def test_three_phase_rounds_down_to_amps():
    assert watts_to_amps(11100, 3, 1, 6, 16) == 16


def test_below_max_floors_to_step():
    assert watts_to_amps(5500, 3, 1, 6, 32) == 7


def test_half_amp_step():
    assert watts_to_amps(5500, 3, 0.5, 6, 32) == 7.5


def test_below_min_is_none():
    assert watts_to_amps(3000, 3, 1, 6, 32) is None
    assert watts_to_amps(0, 1, 1, 6, 32) is None


def test_clamped_to_max():
    assert watts_to_amps(50000, 3, 1, 6, 16) == 16


def test_invalid_inputs_are_none():
    assert watts_to_amps(5000, 0, 1, 6, 16) is None
    assert watts_to_amps(5000, 1, 0, 6, 16) is None


def test_amps_to_watts():
    assert amps_to_watts(16, 3) == 11040
    assert amps_to_watts(6, 1) == 1380


def test_slot_for_boundaries():
    a, b = _slot(10, 11), _slot(11, 12)
    loads = [a, b]
    assert slot_for(datetime(2026, 10, 1, 10, tzinfo=UTC), loads) is a
    assert slot_for(datetime(2026, 10, 1, 10, 59, 59, tzinfo=UTC), loads) is a
    assert slot_for(datetime(2026, 10, 1, 11, tzinfo=UTC), loads) is b
    assert slot_for(datetime(2026, 10, 1, 12, tzinfo=UTC), loads) is None
    assert slot_for(datetime(2026, 10, 1, 9, tzinfo=UTC), loads) is None
