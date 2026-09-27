"""Sensory trybu bezpośredniego: encje z mapy `read` profilu, tylko przy połączeniu."""
from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from custom_components.volcast import sensor as sensor_mod
from custom_components.volcast.const import DOMAIN
from custom_components.volcast.control.direct_sensors import SENSOR_SPECS, DirectSensor, direct_sensors
from custom_components.volcast.core.modbus.reading import build_reading
from custom_components.volcast.core.profile import load_builtin
from custom_components.volcast.core.registers import RegisterImage
from custom_components.volcast.core.transports.base import TransportStats
from tests.sim.fixtures import deye_words, goodwe_words

GW = load_builtin("goodwe-et")
DEYE = load_builtin("deye-sg")
COMPONENT = Path(__file__).parents[2] / "custom_components" / "volcast"


class FakeConn:
    """Połączenie bezpośrednie w minimalnym kształcie potrzebnym sensorom i telemetrii."""

    def __init__(self, profile, words=None, *, age=0.0, poll_s=10.0, trial=False):
        self.profile = profile
        words = words if words is not None else (goodwe_words() if profile.id == "goodwe-et" else deye_words())
        self.reading = build_reading(profile, RegisterImage(words), at_mono=0.0,
                                     at_utc=datetime(2026, 9, 23, 10, tzinfo=timezone.utc))
        self._age = age
        self.poll_s = poll_s
        self.trial = trial
        self.stats = TransportStats(requests=40, timeouts=2, stray=1)
        self.target = {"transport": "goodwe_udp", "host": "192.168.1.50", "port": 8899, "unit_id": 247,
                       "device_fp": "0123456789abcdef"}
        self.identity = "confirmed"
        self.conflict = False
        self.listeners = []

    def age_s(self):
        return self._age if self.reading is not None else math.inf

    def refused(self):
        return None

    def add_listener(self, cb):
        self.listeners.append(cb)
        return lambda: self.listeners.remove(cb)


def _by_key(entities):
    return {e.key: e for e in entities}


def test_sensors_created_only_with_connection():
    entry = SimpleNamespace(entry_id="e1", options={})
    added = []

    def run(control):
        hass = SimpleNamespace(data={DOMAIN: {"e1": {"control": control}}})
        added.clear()
        asyncio.run(sensor_mod.async_setup_entry(hass, entry, lambda ents: added.extend(ents)))
        return [e for e in added if isinstance(e, DirectSensor)]

    rt = SimpleNamespace(executor=SimpleNamespace(), direct=None)
    assert run(rt) == []
    rt.direct = FakeConn(GW)
    keys = {e.key for e in run(rt)}
    assert {"soc", "battery_temp_c", "pv_power_w", "grid_power_w", "pv_energy_total_kwh", "mode",
            "power_w", "soc_min", "link_quality"} <= keys
    assert "mode_value" not in keys and "export_limit_enabled" not in keys


def test_unique_ids_have_control_prefix():
    for e in direct_sensors(SimpleNamespace(entry_id="e1"), FakeConn(GW)):
        assert e.unique_id == f"e1_control_direct_{e.key}"


def test_energy_sensors_total_increasing():
    for key in ("pv_energy_total_kwh", "grid_import_total_kwh", "grid_export_total_kwh"):
        spec = SENSOR_SPECS[key]
        assert spec.unit == "kWh" and spec.device_class == "energy" and spec.state_class == "total_increasing"
    assert SENSOR_SPECS["pv_power_w"].state_class == "measurement"
    assert SENSOR_SPECS["power_w"].diagnostic and SENSOR_SPECS["link_quality"].diagnostic
    assert not SENSOR_SPECS["soc"].diagnostic


def test_sensor_values_and_unavailable_when_reading_stale():
    conn = FakeConn(GW)
    s = _by_key(direct_sensors(SimpleNamespace(entry_id="e1"), conn))
    assert s["soc"].available and s["soc"].native_value == 83
    assert s["grid_power_w"].native_value == conn.reading.values["grid_power_w"]
    assert s["link_quality"].native_value == 5.0                  # 2 z 40 ramek bez odpowiedzi
    conn._age = 3 * conn.poll_s + 0.1
    assert not s["soc"].available and s["soc"].native_value is None
    assert s["link_quality"].available                            # jakość łącza liczy się dalej
    conn.reading = None
    assert not s["soc"].available


def test_mode_sensor_enum_options_from_profile():
    conn = FakeConn(GW)
    mode = _by_key(direct_sensors(SimpleNamespace(entry_id="e1"), conn))["mode"]
    assert mode.options == list(GW.modes) and mode.native_value == "charge_battery"
    conn.reading = build_reading(GW, RegisterImage({**goodwe_words(), 47511: 99}), at_mono=0.0,
                                 at_utc=conn.reading.at_utc)
    assert mode.native_value is None                              # wartość spoza profilu


def test_no_serial_sensor():
    assert "serial" not in SENSOR_SPECS
    for profile in (GW, DEYE):
        keys = {e.key for e in direct_sensors(SimpleNamespace(entry_id="e1"), FakeConn(profile))}
        assert not any("serial" in k for k in keys)
        assert "mode" not in keys or "mode" in profile.raw["write"]


def test_listener_updates_state_and_unsubscribes():
    conn = FakeConn(GW)
    s = _by_key(direct_sensors(SimpleNamespace(entry_id="e1"), conn))["soc"]
    writes, removed = [], []
    s.async_write_ha_state = lambda: writes.append(1)
    s.async_on_remove = removed.append
    asyncio.run(s.async_added_to_hass())
    conn.listeners[0]()
    assert writes == [1]
    removed[0]()
    assert conn.listeners == []


def test_sensor_translations_present():
    for path in (COMPONENT / "strings.json", COMPONENT / "translations" / "en.json"):
        sensors = json.loads(path.read_text(encoding="utf-8"))["entity"]["sensor"]
        for key, spec in SENSOR_SPECS.items():
            assert sensors[f"control_direct_{key}"]["name"], key
            if key == "mode":
                assert set(sensors["control_direct_mode"]["state"]) >= set(GW.modes)
