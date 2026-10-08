"""Widoki odczytu jednego rejestru (bloki znane jako dozwolone + sam rejestr) i blok rozdzielający."""
from dataclasses import replace

from custom_components.volcast.core.modbus.views import (
    key_views, needs_disambiguation, pick_view, separator_blocks)


class _T:
    def __init__(self, kind):
        self.kind = kind


def test_goodwe_profile_declares_box_blocks(goodwe_profile):
    # Box czyta te bloki co minutę od tygodni — dowód, że są dozwolone na GW8KN-ET.
    assert goodwe_profile.modbus.verify_blocks == ((47509, 4), (45353, 4))


def test_key_views_blocks_first_then_register(goodwe_profile):
    assert key_views(goodwe_profile, 47512) == ((47509, 4), (47512, 1))
    assert key_views(goodwe_profile, 47509) == ((47509, 4), (47509, 1))
    assert key_views(goodwe_profile, 45356) == ((45353, 4), (45356, 1))
    assert key_views(goodwe_profile, 47760) == ((47760, 1),)         # brak znanego bloku
    assert key_views(goodwe_profile, 47512, skip={(47509, 4)}) == ((47512, 1),)


def test_pick_view_differs_from_previous_length():
    views = ((47509, 4), (47512, 1))
    assert pick_view(views, None) == (47509, 4)
    assert pick_view(views, 4) == (47512, 1)
    assert pick_view(views, 1) == (47509, 4)
    assert pick_view(views, 33) == (47509, 4)
    assert pick_view(((47760, 1),), 1) is None                        # tylko ta sama długość


def test_separator_differs_from_every_view_and_avoids_the_register(goodwe_profile):
    # EMS i DOD mają tę samą długość (4) — dla ich kluczy rozdziela tylko blok identyfikacji (33).
    assert separator_blocks(goodwe_profile, 47512) == ((35000, 33),)
    assert separator_blocks(goodwe_profile, 45356) == ((35000, 33),)
    # soc_max czyta się tylko pojedynczo — każdy znany blok innej długości, w kolejności prób
    assert separator_blocks(goodwe_profile, 47760) == ((47509, 4), (45353, 4), (35000, 33))
    bare = replace(goodwe_profile, modbus=replace(goodwe_profile.modbus, verify_blocks=(), identify_reads=((47760, 1),)))
    assert separator_blocks(bare, 47760) == ()


def test_only_uncorrelated_transports_need_disambiguation():
    assert needs_disambiguation(_T("goodwe_udp")) is True
    for kind in ("modbus_tcp", "modbus_rtu", "solarman_v5", None):
        assert needs_disambiguation(_T(kind)) is False
