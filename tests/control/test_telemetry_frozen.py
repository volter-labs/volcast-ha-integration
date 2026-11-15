"""Zamrożony odczyt w trybie encji: pobór i sieć bez zmiany przez 1800 s, przy czym źródło powtarza raporty (`last_reported`) = odczyt nie idzie do chmury."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from custom_components.volcast.control.telemetry import FROZEN_AFTER_S, FROZEN_MIN_REPORTS, TelemetrySender
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


def _setup(load="300", grid="120", *, manual=None, prices=False, loads=None):
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
    if loads is not None:
        s._loads_block = lambda: loads
    return h, s, cloud, clock


def _rebroadcast(h, clock):
    """Źródło powtarza te same wartości: stan bez zmiany, ale `last_reported` rusza (jak zapis w HA)."""
    for eid in (LOAD, GRID, PV, BAT):
        st = h.states.get(eid)
        if st is not None:
            h.states.set(eid, st.state, st.attributes, reported=clock.now)


def _tick(s, cloud, clock, seconds=60):
    clock.advance(seconds)
    _rebroadcast(s._hass, clock)
    asyncio.run(s.async_flush())
    return cloud.sent[-1] if cloud.sent else None


def _run_to(s, cloud, clock, until_s):
    """Tiki co 60 s od pierwszego odczytu (t = 0) do `until_s` włącznie; zwraca ostatni odczyt."""
    _rebroadcast(s._hass, clock)
    asyncio.run(s.async_flush())
    last = cloud.sent[-1] if cloud.sent else None
    for _ in range(until_s // 60):
        last = _tick(s, cloud, clock) or last
    return last


def _run_for(s, cloud, clock, seconds):
    """Tiki co 60 s przez `seconds`; zwraca liczbę wysłanych odczytów po ostatnim."""
    for _ in range(seconds // 60):
        _tick(s, cloud, clock)
    return len(cloud.sent)


def _frozen(s):
    return s.readings_report()["readings_frozen"]


def test_threshold_is_thirty_minutes_and_ten_reports():
    assert FROZEN_AFTER_S == 1800 and FROZEN_MIN_REPORTS == 10


def test_rebroadcast_for_1800s_stops_sending_values_but_prices_go():
    h, s, cloud, clock = _setup(prices=True)
    last = _run_to(s, cloud, clock, 1740)
    assert last["load_power_w"] == 300 and "frozen" not in last["extra"]["volcast"] and not _frozen(s)
    n = len(cloud.sent)
    _tick(s, cloud, clock)                                             # t = 1800
    assert _frozen(s) and len(cloud.sent) == n                          # ceny już poszły, wartości wstrzymane
    _run_for(s, cloud, clock, 6 * 3600)                                # ponowna wysyłka cen niesie znacznik
    last = cloud.sent[-1]
    assert last["extra"]["volcast"]["frozen"] is True and last["prices"] is not None
    assert "load_power_w" not in last and "grid_power_w" not in last and "battery_soc" not in last
    assert "driver" in last


def test_frozen_without_prices_posts_nothing_like_unavailable_inverter():
    """Zamrożony odczyt zachowuje się jak dziś niedostępne encje falownika: bez wartości i cen nie ma odczytu
    (także bez bloku `loads` — ograniczenie `build_reading`, nie tej reguły). Dla chmury zostają stare dane,
    jak przy HA offline — to zamierzone."""
    loads = [{"id": "ev"}]
    h, s, cloud, clock = _setup(loads=loads)
    _run_to(s, cloud, clock, 1740)
    n = len(cloud.sent)
    _tick(s, cloud, clock)
    assert _frozen(s) and len(cloud.sent) == n
    h2, s2, cloud2, clock2 = _setup(load="unavailable", grid="unavailable", loads=loads)
    asyncio.run(s2.async_flush())
    assert cloud2.sent == []


def test_frozen_reading_with_prices_still_carries_loads():
    loads = [{"id": "ev"}]
    h, s, cloud, clock = _setup(prices=True, loads=loads)
    _run_to(s, cloud, clock, 1800)
    _run_for(s, cloud, clock, 6 * 3600)
    last = cloud.sent[-1]
    assert last["loads"] == loads and last["extra"]["volcast"]["frozen"] is True


def test_live_reading_and_counters_are_withheld_while_frozen():
    manual = {"load_power_w": LOAD, "grid_power_w": GRID, "load_energy_total_kwh": "sensor.kwh"}
    h, s, cloud, clock = _setup(prices=True, manual=manual)
    h.states.set("sensor.kwh", "12.5", {"unit_of_measurement": "kWh"})
    asyncio.run(s.async_flush())
    assert cloud.sent[0]["extra"]["counters"] == {"load_energy_total_kwh": 12.5}
    assert s.build_live_reading()["load_power_w"] == 300
    _run_to(s, cloud, clock, 1800)
    h.states.set("sensor.kwh", "12.5", {"unit_of_measurement": "kWh"}, reported=clock.now)
    assert _frozen(s) and s.build_live_reading() is None
    _run_for(s, cloud, clock, 6 * 3600)
    assert "counters" not in cloud.sent[-1]["extra"]


def test_slow_poller_with_identical_values_is_never_frozen():
    h, s, cloud, clock = _setup()
    for i in range(5 * 60):                                            # 5 h, raport co 15 min, wartości te same
        clock.advance(60)
        if i % 15 == 0:
            _rebroadcast(h, clock)
        asyncio.run(s.async_flush())
        assert not _frozen(s)
    assert cloud.sent[-1]["load_power_w"] == 300


def test_state_without_last_reported_is_never_frozen():
    h, s, cloud, clock = _setup()
    _run_to(s, cloud, clock, 1740)
    for eid in (LOAD, GRID):
        h.states.get(eid).last_reported = None
    for _ in range(60):                                                # bez ponownego ustawiania stanów
        clock.advance(60)
        asyncio.run(s.async_flush())
    assert not _frozen(s)


def test_steady_coarse_night_load_frozen_only_with_reports_and_resumes_on_change():
    h, s, cloud, clock = _setup(load="300", grid="300")                # gruba rozdzielczość, stała noc
    _run_to(s, cloud, clock, 1800)
    assert _frozen(s)
    h.states.set(LOAD, "310", W, reported=clock.now)
    h.states.set(GRID, "300", W, reported=clock.now)
    last = _tick(s, cloud, clock)
    assert last["load_power_w"] == 310 and not _frozen(s)


def test_both_unchanged_without_enough_reports_is_not_frozen():
    h, s, cloud, clock = _setup()
    asyncio.run(s.async_flush())
    for _ in range(40):                                                # 40 min bez ani jednego nowego raportu
        clock.advance(60)
        asyncio.run(s.async_flush())
    assert not _frozen(s)


def test_change_of_either_value_resumes_immediately():
    for eid, val in ((GRID, "121"), (LOAD, "301")):
        h, s, cloud, clock = _setup()
        _run_to(s, cloud, clock, 2400)
        assert _frozen(s)
        h.states.set(eid, val, W, reported=clock.now)
        last = _tick(s, cloud, clock)
        assert last["load_power_w"] == (301 if eid == LOAD else 300) and "frozen" not in last["extra"]["volcast"]
        assert not _frozen(s)


def test_clock_and_report_count_restart_after_resume():
    h, s, cloud, clock = _setup()
    _run_to(s, cloud, clock, 2400)
    h.states.set(GRID, "130", W, reported=clock.now)
    _tick(s, cloud, clock)
    _run_for(s, cloud, clock, 1740)
    assert not _frozen(s)                                              # 1740 s od zmiany
    _tick(s, cloud, clock)
    assert _frozen(s)                                                  # 1800 s


def test_unavailable_is_not_unchanged_and_restarts_the_clock():
    h, s, cloud, clock = _setup()
    _run_to(s, cloud, clock, 900)
    h.states.set(LOAD, "unavailable", W, reported=clock.now)
    asyncio.run(s.async_flush())
    h.states.set(LOAD, "300", W, reported=clock.now)                   # wraca z tą samą wartością
    _tick(s, cloud, clock)                                             # zmiana (powrót z niedostępności)
    _run_for(s, cloud, clock, 1740)
    assert not _frozen(s)
    _tick(s, cloud, clock)
    assert _frozen(s)


def test_single_mapped_key_is_never_frozen():
    for manual in ({"load_power_w": LOAD}, {"grid_power_w": GRID}):
        h, s, cloud, clock = _setup(manual=manual)
        last = _run_to(s, cloud, clock, 3600)
        assert "frozen" not in last["extra"]["volcast"] and not _frozen(s)


def test_both_zero_is_never_frozen():
    h, s, cloud, clock = _setup(load="0", grid="0")
    last = _run_to(s, cloud, clock, 3600)
    assert "frozen" not in last["extra"]["volcast"] and last["load_power_w"] == 0


def test_a_change_of_another_mapped_value_counts_as_life():
    h, s, cloud, clock = _setup(manual={"load_power_w": LOAD, "grid_power_w": GRID, "pv_power_w": PV})
    h.states.set(PV, "0", W)
    asyncio.run(s.async_flush())
    for i in range(60):
        h.states.set(PV, str(i), W, reported=clock.now)
        last = _tick(s, cloud, clock)
        assert "frozen" not in last["extra"]["volcast"]
    assert last["load_power_w"] == 300


def test_state_is_visible_in_readings_report_with_since():
    h, s, cloud, clock = _setup()
    assert s.readings_report()["readings_frozen_since"] is None
    _run_to(s, cloud, clock, 1800)
    rep = s.readings_report()
    assert rep["readings_frozen"] is True and rep["readings_frozen_since"] == T0.isoformat()
    h.states.set(GRID, "1", W, reported=clock.now)
    rep = s.readings_report()
    assert rep["readings_frozen"] is False and rep["readings_frozen_since"] is None


def test_restart_starts_the_timer_from_the_first_reading():
    h, s, cloud, clock = _setup()
    _run_to(s, cloud, clock, 2400)
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


def test_frozen_detection_works_on_the_profile_mapped_path():
    profile = profile_from_dict(_EO_PROFILE)
    h = FakeHass()
    mapped = {"load_power_w": "sensor.ro_load", "grid_power_w": "sensor.ro_grid"}
    clock, cloud = Clock(), Cloud()
    s = TelemetrySender(h, SimpleNamespace(entry_id="e1", options={}), cloud, Exec(),
                        choice=ProfileChoice(profile, "ro_test", None), profile_map=mapped, manual_map={},
                        grid_negate=False, limits=None, utcnow=clock)

    def report():
        for eid, val in (("sensor.ro_load", "400"), ("sensor.ro_grid", "-1000")):
            h.states.set(eid, val, W, reported=clock.now)
    for _ in range(31):
        report()
        asyncio.run(s.async_flush())
        clock.advance(60)
    assert s.readings_report()["readings_frozen"] is True
    assert cloud.sent[0]["load_power_w"] == 400 and len(cloud.sent) == 30   # 31. tik (t = 1800) już zamrożony


def test_both_report_counts_advance_on_every_tick():
    # Źródło raportuje co 2 min, wysyłka co minutę: w oknie 1800 s obie encje mają po 16 raportów — zamrożony
    # dokładnie po 1800 s, a nie dopiero gdy licznik poboru dobije progu i sieć zacznie być liczona.
    h, s, cloud, clock = _setup(prices=True)
    _rebroadcast(h, clock)
    asyncio.run(s.async_flush())
    for t in range(60, 1800 + 1, 60):
        clock.advance(60)
        if t % 120 == 0:
            _rebroadcast(h, clock)
        asyncio.run(s.async_flush())
        assert _frozen(s) is (t >= 1800), t
