import pytest

from custom_components.volcast.core.control.baseline import (SNAPSHOT_KEYS, baseline_params,
                                                             needs_restore, snapshot_missing,
                                                             take_snapshot)
from custom_components.volcast.core.control.takeover import FOREIGN_PAUSE_S, is_foreign_change
from custom_components.volcast.core.params import Params
from custom_components.volcast.core.profile import load_builtin

GW = load_builtin("goodwe-et")


@pytest.mark.parametrize("ours,actor,new,last,expected", [
    (False, True, 3000.0, 2000.0, True),      # automatyzacja właściciela zmieniła moc
    (True, True, 3000.0, 2000.0, False),      # nasz własny zapis
    (False, False, 3000.0, 2000.0, False),    # odświeżenie integracji falownika — rozjazd, nie przejęcie
    (False, True, 2000.4, 2000.0, False),     # kwant rejestru
    (False, True, "auto", "sell_power", True),
    (False, True, "sell_power", "sell_power", False),
    (False, True, None, 2000.0, False),       # nieczytelny stan
    (False, True, 3000.0, None, False),       # tego klucza nie pisaliśmy
])
def test_is_foreign_change(ours, actor, new, last, expected):
    assert is_foreign_change(ours=ours, has_actor=actor, new_value=new, expected=last) is expected


def test_foreign_pause_is_half_an_hour():
    assert FOREIGN_PAUSE_S == 1800.0


def test_snapshot_only_known_keys():
    assert take_snapshot({"mode": "eco", "soc_min": 15.0, "power_w": 100.0}) == {"mode": "eco", "soc_min": 15.0}


def test_snapshot_keys_include_charge_ceiling():
    assert SNAPSHOT_KEYS == ("mode", "soc_min", "soc_max", "export_limit_w", "export_limit_enabled")


def test_baseline_uses_snapshot_export_limit():
    snap = {"soc_min": 15.0, "export_limit_w": 4000.0, "export_limit_enabled": 1.0, "mode": "eco"}
    assert baseline_params(GW, snap) == Params(mode="auto", soc_min=15.0, export_limit_w=4000.0,
                                               export_limit_enabled=True)


def test_baseline_restores_snapshot_soc_max():
    snap = take_snapshot({"mode": "eco", "soc_min": 15.0, "soc_max": 100.0, "power_w": 2500.0})
    assert baseline_params(GW, snap) == Params(mode="auto", soc_min=15.0, soc_max=100.0)


def test_baseline_without_snapshot_is_mode_only():
    assert baseline_params(GW, {}) == Params(mode="auto")


def test_baseline_without_profile_mode_writes_nothing():
    assert baseline_params(load_builtin("deye-sg"), {"soc_min": 15.0}) == Params()


@pytest.mark.parametrize("owned,consent,local,mode,expected", [
    (False, False, False, None, False),        # nigdy nie pisaliśmy — nic do przywracania
    (True, True, True, "entities", False),
    (True, False, True, "entities", True),     # zgoda cofnięta
    (True, None, True, "entities", False),     # zgoda nieznana (brak chmury) — nie ruszamy
    (True, True, False, "entities", True),     # lokalny przełącznik OFF
    (True, True, True, None, True),            # tryb encji wyłączony w opcjach
])
def test_needs_restore(owned, consent, local, mode, expected):
    assert needs_restore(owned=owned, consent=consent, local_switch=local, control_mode=mode) is expected


def test_snapshot_missing_lists_mapped_keys_without_reading_in_snapshot_order():
    mapped = ("mode", "power_w", "soc_min", "soc_max", "export_limit_w", "export_limit_enabled")
    snap = take_snapshot({"soc_max": 100.0, "export_limit_enabled": 1.0})
    # tryb bazowy pochodzi z profilu — jego brak w migawce niczego nie wstrzymuje
    assert snapshot_missing(snap, mapped) == ("soc_min", "export_limit_w")


def test_snapshot_missing_ignores_unmapped_keys():
    assert snapshot_missing({"soc_min": 10.0}, ("mode", "soc_min")) == ()
