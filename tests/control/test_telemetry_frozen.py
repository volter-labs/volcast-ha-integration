"""Zamrożony odczyt w trybie encji: pobór i sieć bez zmiany przez 600 s = odczyt nie idzie do chmury."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from custom_components.volcast.control.telemetry import FROZEN_AFTER_S, TelemetrySender
from custom_components.volcast.core.control.select import ProfileChoice
from custom_components.volcast.core.profile import entities_only, profile_from_dict, writes_allowed

from .ha_fakes import FakeHass, goodwe_hass
from .test_telemetry import Cloud, Exec, GW, _nordpool
from .test_telemetry_direct import GW as DIRECT_GW, _sender as direct_sender
from tests.control.test_direct_sensors import FakeConn

T0 = datetime(2026, 9, 23, 10, 0, 0, tzinfo=timezone.utc)
LOAD, GRID, PV, BAT = "sensor.load", "sensor.grid", "sensor.pv", "sensor.bat"
W = {"unit_of_measurement": "W"}


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


def _setup(load="300", grid="120", *, manual=None, prices=False):
    h = goodwe_hass()
    if prices:
        _nordpool(h)
    for eid, val in ((LOAD, load), (GRID, grid)):
        if val is not None:
            h.states.set(eid, val, W)
    clock, cloud = Clock(), Cloud()
    entry = SimpleNamespace(entry_id="e1", options={"entity_price_buy": "sensor.nordpool"} if prices else {})
    s = TelemetrySender(h, entry, cloud, Exec(), choice=GW, profile_map={},
                        manual_map=manual if manual is not None else {"load_power_w": LOAD, "grid_power_w": GRID},
                        grid_negate=False, limits=None, utcnow=clock)
    return h, s, cloud, clock


def _tick(s, cloud, clock, seconds=60):
    clock.advance(seconds)
    asyncio.run(s.async_flush())
    return cloud.sent[-1] if cloud.sent else None


def _run_to(s, cloud, clock, until_s):
    """Tiki co 60 s od pierwszego odczytu (t = 0) do `until_s` włącznie; zwraca ostatni odczyt."""
    asyncio.run(s.async_flush())
    last = cloud.sent[-1] if cloud.sent else None
    for _ in range(until_s // 60):
        last = _tick(s, cloud, clock) or last
    return last


def test_threshold_is_ten_minutes():
    assert FROZEN_AFTER_S == 600


def test_both_unchanged_for_600s_stops_sending_values_but_prices_go():
    h, s, cloud, clock = _setup(prices=True)
    last = _run_to(s, cloud, clock, 540)
    assert last["load_power_w"] == 300 and "frozen" not in last["extra"]["volcast"]
    n = len(cloud.sent)
    assert _tick(s, cloud, clock) is last and len(cloud.sent) == n     # t = 600: ceny już poszły, wartości wstrzymane
    last = _tick(s, cloud, clock, 6 * 3600)                            # ponowna wysyłka cen niesie znacznik
    assert last["extra"]["volcast"]["frozen"] is True
    assert "load_power_w" not in last and "grid_power_w" not in last and "battery_soc" not in last
    assert last["prices"] is not None and "driver" in last


def test_frozen_without_prices_sends_nothing():
    h, s, cloud, clock = _setup()
    _run_to(s, cloud, clock, 540)
    n = len(cloud.sent)
    clock.advance(60)
    assert asyncio.run(s.async_flush()) is False
    assert len(cloud.sent) == n


def test_just_under_threshold_is_not_frozen():
    h, s, cloud, clock = _setup()
    last = _run_to(s, cloud, clock, 540)
    assert "frozen" not in last["extra"]["volcast"]
    clock.advance(59)
    asyncio.run(s.async_flush())
    assert "frozen" not in cloud.sent[-1]["extra"]["volcast"] and cloud.sent[-1]["load_power_w"] == 300


def test_change_of_either_value_resumes_immediately():
    for eid, val in ((GRID, "121"), (LOAD, "301")):
        h, s, cloud, clock = _setup()
        _run_to(s, cloud, clock, 900)
        assert s.readings_report()["readings_frozen"] is True
        h.states.set(eid, val, W)
        last = _tick(s, cloud, clock)
        assert last["load_power_w"] == (301 if eid == LOAD else 300) and "frozen" not in last["extra"]["volcast"]
        assert s.readings_report()["readings_frozen"] is False


def test_unchanged_clock_restarts_after_resume():
    h, s, cloud, clock = _setup()
    _run_to(s, cloud, clock, 900)
    h.states.set(GRID, "130", W)
    _tick(s, cloud, clock)
    _run_to_continue(s, cloud, clock, 540)
    assert s.readings_report()["readings_frozen"] is False             # 540 s od zmiany
    _tick(s, cloud, clock)
    assert s.readings_report()["readings_frozen"] is True              # 600 s


def _run_to_continue(s, cloud, clock, seconds):
    last = None
    for _ in range(seconds // 60):
        last = _tick(s, cloud, clock)
    return last


def test_unavailable_is_not_unchanged_and_restarts_the_clock():
    h, s, cloud, clock = _setup()
    _run_to(s, cloud, clock, 420)
    h.states.set(LOAD, "unavailable", W)
    _tick(s, cloud, clock)
    h.states.set(LOAD, "300", W)                                       # wraca z tą samą wartością
    _tick(s, cloud, clock)
    _run_to_continue(s, cloud, clock, 540)
    assert s.readings_report()["readings_frozen"] is False             # 540 s od zmiany
    _tick(s, cloud, clock)
    assert s.readings_report()["readings_frozen"] is True              # 600 s


def test_single_mapped_key_is_never_frozen():
    for manual in ({"load_power_w": LOAD}, {"grid_power_w": GRID}):
        h, s, cloud, clock = _setup(manual=manual)
        last = _run_to(s, cloud, clock, 3600)
        assert "frozen" not in last["extra"]["volcast"]
        assert s.readings_report()["readings_frozen"] is False


def test_both_zero_is_never_frozen():
    h, s, cloud, clock = _setup(load="0", grid="0")
    last = _run_to(s, cloud, clock, 3600)
    assert "frozen" not in last["extra"]["volcast"] and last["load_power_w"] == 0


def test_a_change_of_another_mapped_value_counts_as_life():
    h, s, cloud, clock = _setup(manual={"load_power_w": LOAD, "grid_power_w": GRID, "pv_power_w": PV})
    h.states.set(PV, "0", W)
    asyncio.run(s.async_flush())
    for i in range(30):
        h.states.set(PV, str(i), W)
        last = _tick(s, cloud, clock)
        assert "frozen" not in last["extra"]["volcast"]
    assert last["load_power_w"] == 300


def test_state_is_visible_in_readings_report_with_since():
    h, s, cloud, clock = _setup()
    assert s.readings_report()["readings_frozen_since"] is None
    _run_to(s, cloud, clock, 600)
    rep = s.readings_report()
    assert rep["readings_frozen"] is True and rep["readings_frozen_since"] == T0.isoformat()
    h.states.set(GRID, "1", W)
    rep = s.readings_report()
    assert rep["readings_frozen"] is False and rep["readings_frozen_since"] is None


def test_restart_starts_the_timer_from_the_first_reading():
    h, s, cloud, clock = _setup()
    _run_to(s, cloud, clock, 900)
    fresh = TelemetrySender(h, SimpleNamespace(entry_id="e1", options={}), Cloud(), Exec(), choice=GW,
                            profile_map={}, manual_map={"load_power_w": LOAD, "grid_power_w": GRID},
                            grid_negate=False, limits=None, utcnow=clock)
    assert fresh.readings_report()["readings_frozen"] is False


def test_direct_mode_is_never_frozen():
    conn = FakeConn(DIRECT_GW)
    clock = Clock()
    s, cloud = direct_sender(conn)
    s._utcnow = clock
    for _ in range(40):
        asyncio.run(s.async_flush())
        clock.advance(60)
    assert "frozen" not in cloud.sent[-1]["extra"]["volcast"]
    assert "grid_power_w" in cloud.sent[-1] and s.readings_report()["readings_frozen"] is False


_EO_PROFILE = {
    "schema_version": 1, "id": "readonly-test", "label": "Read only (test)", "status": "draft",
    "sources": [{"what": "test", "ref": "test"}], "access": "entities_only", "min_engine": 3,
    "ha": {"integrations": [{"domain": "ro_test", "ems": False, "status": "draft",
                             "model_regex": ["(?i)\\bro test\\b"], "entities": {
        "soc": {"domain": "sensor", "unique_id_regex": "_soc$"},
        "pv_power_w": {"domain": "sensor", "unique_id_regex": "_pv$"},
        "load_power_w": {"domain": "sensor", "unique_id_regex": "_load$"},
        "grid_power_w": {"domain": "sensor", "unique_id_regex": "_grid$"},
        "battery_power_w": {"domain": "sensor", "unique_id_regex": "_bat$"}}}]},
    "capabilities": {"force_charge_from_grid": False, "sell_from_battery": False, "force_discharge": False,
                     "standby": False, "set_power_w": False, "limit_export": False, "set_soc_floor": False,
                     "set_soc_ceiling": False, "time_windows": 0},
}


def test_entities_only_profile_sends_readings_without_any_control_mode_or_consent():
    """Brak bramki trybu sterowania, zgody ani encji trybu między wyborem profilu a wysyłką odczytu."""
    profile = profile_from_dict(_EO_PROFILE)
    assert entities_only(profile) and writes_allowed(profile) is False
    h = FakeHass()
    mapped = {"soc": "sensor.ro_soc", "pv_power_w": "sensor.ro_pv", "load_power_w": "sensor.ro_load",
              "grid_power_w": "sensor.ro_grid", "battery_power_w": "sensor.ro_bat"}
    for key, val, unit in (("soc", "55", "%"), ("pv_power_w", "1500", "W"), ("load_power_w", "400", "W"),
                           ("grid_power_w", "-1000", "W"), ("battery_power_w", "100", "W")):
        h.states.set(mapped[key], val, {"unit_of_measurement": unit})
    cloud = Cloud()
    entry = SimpleNamespace(entry_id="e1", options={})                       # żadnej opcji trybu sterowania
    s = TelemetrySender(h, entry, cloud, Exec(), choice=ProfileChoice(profile, "ro_test", None),
                        profile_map=mapped, manual_map={}, grid_negate=False, limits=None, utcnow=lambda: T0)
    assert asyncio.run(s.async_flush()) is True
    sent = cloud.sent[0]
    assert (sent["battery_soc"], sent["pv_power_w"], sent["load_power_w"], sent["grid_power_w"],
            sent["battery_power_w"]) == (55, 1500, 400, -1000, 100)
