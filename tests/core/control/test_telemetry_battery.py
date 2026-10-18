"""Moc baterii w telemetrii cyklu: wartość i wiek, dodatnia = rozładowanie; brak odczytu nic nie blokuje."""
import asyncio
from types import SimpleNamespace

from custom_components.volcast.control.device_io import DirectIO
from custom_components.volcast.core.control.cycle import Telemetry
from custom_components.volcast.core.modbus.reading import build_reading
from custom_components.volcast.core.profile import load_builtin
from custom_components.volcast.core.registers import RegisterImage
from tests.control import test_executor as te
from tests.control.ha_fakes import NOW, goodwe_hass
from tests.sim.fixtures import goodwe_words

E_BAT = {**te.E_LIVE, "battery_power_w": "sensor.goodwe_battery_power"}


def test_telemetry_battery_defaults_to_none():
    t = Telemetry(soc=50.0, soc_age_s=1.0, battery_temp_c=None)
    assert t.battery_w is None and t.battery_age_s is None


def test_direct_io_carries_battery_power_with_reading_age():
    gw = load_builtin("goodwe-et")
    reading = build_reading(gw, RegisterImage(goodwe_words()), at_mono=0.0, at_utc=NOW)
    conn = SimpleNamespace(client=None, reading=reading, age_s=lambda: 12.0)
    rd = DirectIO(conn, gw, trial=False, salt=bytes(16)).read(NOW)
    assert rd.live("battery_power_w") == (-2734.0, 12.0)          # nagranie: ładowanie
    words = goodwe_words()
    words.pop(35182)
    partial = build_reading(gw, RegisterImage(words), at_mono=0.0, at_utc=NOW)
    rd = DirectIO(SimpleNamespace(client=None, reading=partial, age_s=lambda: 3.0), gw, trial=False,
                  salt=bytes(16)).read(NOW)
    assert rd.live("battery_power_w") == (None, None)
    assert rd.live("pv_power_w")[1] == 3.0


def _hass(battery=None, battery_at=None):
    h = te._live_hass()
    if battery is not None:
        h.states.set(E_BAT["battery_power_w"], battery, {"unit_of_measurement": "W"},
                     reported=battery_at or NOW)
    return h


def test_entity_mode_battery_reaches_telemetry_with_own_age(monkeypatch):
    _, tele = te._tick_live(_hass("1500"), monkeypatch, mapped=E_BAT)
    assert tele.battery_w == 1500.0 and tele.battery_age_s == 30.0
    assert tele.pv_power_w == 513.0 and tele.pv_age_s == 30.0


def test_entity_mode_missing_battery_changes_nothing_else(monkeypatch):
    _, with_bat = te._tick_live(_hass("1500"), monkeypatch, mapped=E_BAT)
    _, without = te._tick_live(te._live_hass(), monkeypatch)
    assert without.battery_w is None and without.battery_age_s is None
    for f in ("soc", "soc_age_s", "pv_power_w", "pv_age_s", "load_power_w", "load_age_s"):
        assert getattr(without, f) == getattr(with_bat, f)
