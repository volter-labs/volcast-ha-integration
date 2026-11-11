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
    assert SNAPSHOT_KEYS[:5] == ("mode", "soc_min", "soc_max", "export_limit_w", "export_limit_enabled")


def test_snapshot_keys_add_set_points_and_switch_only_where_present():
    # klucze nowych konstrukcji trafiają do migawki tylko wtedy, gdy odczyt/mapa je ma
    assert set(SNAPSHOT_KEYS[5:]) == {"charge_power_w", "discharge_power_w", "enable"}
    assert take_snapshot({"mode": "eco", "soc_min": 15.0}) == {"mode": "eco", "soc_min": 15.0}
    snap = take_snapshot({"mode": "x", "charge_power_w": 5000.0, "discharge_power_w": 6000.0, "enable": 0.0})
    assert snap == {"mode": "x", "charge_power_w": 5000.0, "discharge_power_w": 6000.0, "enable": 0.0}
    assert snapshot_missing({}, ("mode", "enable", "charge_power_w")) == ("charge_power_w", "enable")


def _dir():
    from tests.core.modbus.engine_profile import directional_profile
    return directional_profile()


def test_baseline_restores_set_points_and_switch_from_snapshot():
    snap = {"charge_power_w": 5000.0, "discharge_power_w": 6000.0, "enable": 1.0}
    assert baseline_params(_dir(), snap) == Params(mode="auto", export_limit_enabled=False, enable=True,
                                                   charge_power_w=5000.0, discharge_power_w=6000.0)


def test_baseline_enable_from_profile_wins_over_snapshot():
    from tests.core.modbus.engine_profile import directional_profile

    def mutate(raw):
        raw["baseline"]["enable"] = False
    p = baseline_params(directional_profile(mutate), {"enable": 1.0})
    assert p.enable is False and p.charge_power_w is None


def test_baseline_no_limit_set_points_restored_exactly():
    from custom_components.volcast.core.control.target import RegisterTarget
    from custom_components.volcast.core.registers import encode_writes
    from tests.core.modbus.engine_profile import PLANT, directional_profile

    def mutate(raw):
        raw["write"]["export_limit_w"] = {"addr": 40038, "type": "u32", "encode": "watts", "unit_id": PLANT}
    prof = directional_profile(mutate)
    snap = {"export_limit_w": 4294967295.0, "charge_power_w": 4294967295.0,
            "discharge_power_w": 42949672950.0, "enable": 0.0}
    fitted, _, unfit = RegisterTarget(None).fit(baseline_params(prof, snap), prof)
    assert unfit == ()
    words = {w.key: w.value for w in encode_writes(fitted, prof, set(snap))}
    assert words == {"export_limit_w": 0xFFFFFFFF, "charge_power_w": 0xFFFFFFFF,
                     "discharge_power_w": 0xFFFFFFFF, "enable": 0}


def test_goodwe_baseline_unchanged_by_new_keys():
    assert baseline_params(GW, {"soc_min": 15.0, "enable": 1.0, "charge_power_w": 1.0}) == Params(
        mode="auto", soc_min=15.0)


def _sched(*spans):
    from datetime import datetime, timezone
    from custom_components.volcast.core.slot import parse_schedule
    iso = lambda h: datetime(2026, 9, 23, h, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")  # noqa: E731
    return parse_schedule({"schedule_id": "s", "fallback": {"mode": "self_consume", "soc_reserve": 10},
                           "slots": [{"from": iso(a), "to": iso(b), "mode": "self_consume"} for a, b in spans]})


def test_plan_uncovered_since():
    from datetime import datetime, timezone
    from custom_components.volcast.core.control.baseline import plan_uncovered_since
    t = lambda h, m=0: datetime(2026, 9, 23, h, m, tzinfo=timezone.utc)  # noqa: E731
    start = t(8)
    assert plan_uncovered_since(_sched((10, 11)), t(10, 30), start) is None
    assert plan_uncovered_since(_sched((10, 11), (12, 13)), t(11, 30), start) == t(11)
    assert plan_uncovered_since(_sched((12, 13)), t(9), start) == start      # plan dopiero w przyszłości
    assert plan_uncovered_since(None, t(9), start) == start


def test_release_without_plan_only_with_the_profile_field():
    from datetime import datetime, timezone
    from custom_components.volcast.core.control.baseline import release_without_plan
    from tests.core.modbus.engine_profile import directional_profile
    t = lambda h, m=0: datetime(2026, 9, 23, h, m, tzinfo=timezone.utc)  # noqa: E731

    def mutate(raw):
        raw["write_policy"]["release_without_plan_s"] = 600
    p = directional_profile(mutate)
    assert release_without_plan(p, _sched((10, 11)), t(11, 10), t(8)) is False
    assert release_without_plan(p, _sched((10, 11)), t(11, 11), t(8)) is True
    assert release_without_plan(p, _sched((10, 11)), t(10, 59), t(8)) is False
    assert release_without_plan(GW, _sched((10, 11)), t(20), t(8)) is False


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
