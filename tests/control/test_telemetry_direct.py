"""Telemetria w trybie bezpośrednim: wartości z rejestrów, blok `driver` bez seriala i adresu."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from custom_components.volcast.cloud.client import TelemetryResult
from custom_components.volcast.control.direct import DirectConnection
from custom_components.volcast.control.telemetry import TelemetrySender, direct_driver_block
from custom_components.volcast.core.control.caps import direct_capabilities
from custom_components.volcast.core.control.limits import executor_limits
from custom_components.volcast.core.control.select import ProfileChoice
from custom_components.volcast.core.profile import load_builtin
from tests.control.test_direct_connection import SALT, _factory, _target
from tests.control.test_direct_sensors import FakeConn
from tests.core.transports.helpers import FakeClock

GW = load_builtin("goodwe-et")
DEYE = load_builtin("deye-sg")
NOW = datetime(2026, 9, 23, 10, 0, 30, tzinfo=timezone.utc)
ALL_TRUE = {k: True for k in GW.modbus.probe_keys}


class Cloud:
    def __init__(self):
        self.sent = []

    async def async_post_telemetry(self, reading):
        self.sent.append(reading)
        return TelemetryResult(200, None)


class Executor:
    local_switch = True
    nvm_budget_hit = False

    def exec_summary(self):
        return {"decision": None, "profile": "goodwe-et"}


def _hass():
    return SimpleNamespace(states=SimpleNamespace(get=lambda eid: None),
                           config=SimpleNamespace(time_zone="Europe/Warsaw", country="PL"))


def _sender(conn, *, manual=None, limits=None, caps=None, options=None, executor=None, profile=GW):
    entry = SimpleNamespace(entry_id="e1", options=options if options is not None else {"control_mode": "direct"})
    cloud = Cloud()
    sender = TelemetrySender(_hass(), entry, cloud, executor or Executor(), choice=ProfileChoice(profile, None, None),
                             profile_map={}, manual_map=manual or {}, grid_negate=False, limits=limits,
                             utcnow=lambda: NOW, direct=conn, direct_capabilities=caps)
    return sender, cloud


def _flush(sender, cloud):
    assert asyncio.run(sender.async_flush()) is True
    return cloud.sent[-1]


def test_telemetry_values_from_registers_fake_conn():
    conn = FakeConn(GW)
    sender, cloud = _sender(conn, caps=direct_capabilities(GW, ALL_TRUE, ("soc_max",)))
    r = _flush(sender, cloud)
    assert r["battery_soc"] == 83 and r["ems_mode"] == "charge_battery"
    assert r["grid_power_w"] == conn.reading.values["grid_power_w"]
    assert r["driver"]["access"] == "direct" and r["driver"]["id"] == "goodwe-et"
    d = r["extra"]["volcast"]["direct"]
    assert d == {"transport": "goodwe_udp", "status": "ok", "stray": 1, "timeouts": 2, "nvm_budget_hit": False}


@pytest.mark.asyncio
async def test_telemetry_values_from_registers(make_hass, goodwe_udp_sim):
    entry = SimpleNamespace(domain="volcast", entry_id="self", options={"control_mode": "direct"}, data={},
                            disabled_by=None)
    hass = make_hass(entries=[entry])
    conn = DirectConnection(hass, entry, GW, _target(goodwe_udp_sim), trial=False, salt=SALT,
                            transport_factory=_factory(), allow_loopback=True, clock=FakeClock(),
                            unreadable={"soc_max"})
    try:
        await conn.async_start()
        await conn.async_poll()
        sender, cloud = _sender(conn)
        assert await sender.async_flush() is True
        r = cloud.sent[-1]
        assert r["battery_soc"] == 83 and r["pv_power_w"] == conn.reading.values["pv_power_w"]
        assert "grid_import_total_kwh" in r and "pv_energy_total_kwh" in r
    finally:
        await conn.async_stop()


def test_stale_or_missing_reading_sends_no_values_and_link_down():
    conn = FakeConn(GW, age=31.0)
    sender, cloud = _sender(conn)
    assert asyncio.run(sender.async_flush()) is False                # nieświeży odczyt: nic do wysłania
    assert cloud.sent == [] and sender._direct_status() == "link_down"
    conn.reading = None
    assert asyncio.run(sender.async_flush()) is False and sender._direct_status() == "link_down"


def test_status_conflict_and_trial():
    conn = FakeConn(GW)
    conn.conflict = True
    sender, cloud = _sender(conn)
    assert _flush(sender, cloud)["extra"]["volcast"]["direct"]["status"] == "conflict"
    conn = FakeConn(GW, trial=True)
    sender, cloud = _sender(conn, options={"direct_trial": True}, caps=None)
    r = _flush(sender, cloud)
    assert r["extra"]["volcast"]["direct"]["status"] == "trial" and "capabilities" not in r["driver"]


def _hass_with(states):
    def get(eid):
        if eid not in states:
            return None
        value, unit = states[eid]
        return SimpleNamespace(state=value, attributes={"unit_of_measurement": unit})
    return SimpleNamespace(states=SimpleNamespace(get=get),
                           config=SimpleNamespace(time_zone="Europe/Warsaw", country="PL"))


def test_readings_source_prefers_entity_over_register():
    from custom_components.volcast.core.control.readings import MEASURED_KEYS
    conn = FakeConn(GW)
    entry = SimpleNamespace(entry_id="e1", options={"control_mode": "direct"})
    cloud = Cloud()
    sender = TelemetrySender(_hass_with({"sensor.grid": ("1.5", "kW")}), entry, cloud, Executor(),
                             choice=ProfileChoice(GW, None, None), profile_map={},
                             manual_map={"grid_power_w": "sensor.grid", "load_power_w": "sensor.nope"},
                             grid_negate=False, limits=executor_limits(rated_power_w=10000.0, source="registers"),
                             utcnow=lambda: NOW, direct=conn)
    regs = conn.reading.values
    assert "grid_power_w" in regs and "load_power_w" in regs     # rejestr też ma te odczyty
    for _ in range(2):                                           # pełna mapa w każdym odczycie
        r = _flush(sender, cloud)
        src = r["driver"]["meta"]["readings_source"]
        assert src["grid_power_w"] == "entity" and r["grid_power_w"] == 1500.0
        assert src["load_power_w"] == "none" and "load_power_w" not in r   # ręczna nieczytelna wygrywa
        for key in MEASURED_KEYS:
            if key not in ("grid_power_w", "load_power_w"):
                assert src[key] == ("register" if key in regs else "none"), key
        assert "battery_capacity_kwh" not in src                  # nikt jej nie podał
        assert r["driver"]["access"] == "direct"


def test_stale_direct_reading_reports_none_sources():
    conn = FakeConn(GW, age=31.0)
    sender, _ = _sender(conn)
    sender._current()
    assert set(sender.readings_report()["readings_source"].values()) == {"none"}


_COUNTERS = {"load_energy_total_kwh": 4315.5, "load_energy_day_kwh": 39.8,
             "battery_charge_total_kwh": 2495.1, "battery_discharge_total_kwh": 1987.9}


def _gw_conn(**words):
    from tests.sim.fixtures import goodwe_words
    w = goodwe_words()
    w.update({int(a): v for a, v in words.items()})
    return FakeConn(GW, w)


def test_direct_limits_include_max_charge_w():
    sender, cloud = _sender(FakeConn(GW), limits=executor_limits(rated_power_w=8000.0, source="registers"))
    # 9 A × 304,9 V i 22 A × 304,9 V z rejestrów BMS (ramki golden), w dół do 100 W
    assert _flush(sender, cloud)["driver"]["limits"] == {
        "rated_power_w": 8000, "max_charge_w": 2700, "max_discharge_w": 6700, "source": "registers"}
    # bez mocy znamionowej (sonda jej nie dała) — same limity BMS
    sender, cloud = _sender(FakeConn(GW), limits=None)
    assert _flush(sender, cloud)["driver"]["limits"] == {
        "max_charge_w": 2700, "max_discharge_w": 6700, "source": "registers"}


def test_direct_limits_keep_peak_when_bms_tapers():
    conn = FakeConn(GW)
    sender, cloud = _sender(conn, limits=executor_limits(rated_power_w=8000.0, source="registers"))
    assert _flush(sender, cloud)["driver"]["limits"]["max_charge_w"] == 2700
    conn.reading = _gw_conn(**{"37004": 1}).reading              # 1 A przy pełnej baterii
    assert _flush(sender, cloud)["driver"]["limits"]["max_charge_w"] == 2700


@pytest.mark.parametrize("words", [
    {"37004": 0, "37005": 0},                    # BMS zgłasza 0 A
    {"37004": 65535, "37005": 65535},            # śmieci
    {"35180": 0},                                # brak napięcia baterii
])
def test_direct_limits_omit_implausible_bms_values(words):
    sender, cloud = _sender(_gw_conn(**words), limits=executor_limits(rated_power_w=8000.0, source="registers"))
    assert _flush(sender, cloud)["driver"]["limits"] == {"rated_power_w": 8000, "source": "registers"}
    sender, cloud = _sender(_gw_conn(**words), limits=None)
    assert "limits" not in _flush(sender, cloud)["driver"]


def test_direct_counters_in_extra_and_sources():
    sender, cloud = _sender(FakeConn(GW))
    r = _flush(sender, cloud)
    assert r["extra"]["counters"] == pytest.approx(_COUNTERS)
    assert set(r["extra"]) == {"volcast", "counters"}
    assert not set(_COUNTERS) & set(r)                   # nigdy jako pola odczytu
    src = r["driver"]["meta"]["readings_source"]
    for key in (*_COUNTERS, "bms_charge_current_max_a", "bms_discharge_current_max_a"):
        assert src[key] == "register", key
    assert "battery_capacity_kwh" not in src             # profil nie opisuje, nikt nie podał
    assert "counters" not in (sender.build_live_reading() or {})


def test_direct_counters_only_valid_keys():
    # 0xFFFFFFFF ×0,1 kWh = śmieci; licznik dobowy 0 kWh tuż po północy jest prawdziwą wartością
    conn = _gw_conn(**{"35203": 0xFFFF, "35204": 0xFFFF, "35205": 0})
    sender, cloud = _sender(conn)
    counters = _flush(sender, cloud)["extra"]["counters"]
    assert "load_energy_total_kwh" not in counters and counters["load_energy_day_kwh"] == 0.0
    assert counters["battery_charge_total_kwh"] == pytest.approx(2495.1)


def test_no_counters_block_without_any_valid_counter():
    from custom_components.volcast.control.telemetry import counters_block
    assert counters_block({}, {}) is None
    assert counters_block({"load_energy_total_kwh": -1.0, "battery_charge_total_kwh": float("nan"),
                           "battery_discharge_total_kwh": True, "load_energy_day_kwh": "7"}, {}) is None
    # encja wskazana ręcznie wygrywa, także nieczytelna (jak pozostałe pola)
    assert counters_block({"load_energy_total_kwh": 10.0, "battery_charge_total_kwh": 5.0},
                          {"load_energy_total_kwh": None}) == {"battery_charge_total_kwh": 5.0}
    assert counters_block({}, {"load_energy_total_kwh": 12.3456}) == {"load_energy_total_kwh": 12.346}
    assert counters_block({"pv_energy_total_kwh": 100.0}, {}) is None    # kolumny odczytu zostają kolumnami


def test_direct_driver_limits_source_registers():
    limits = executor_limits(rated_power_w=10000.0, source="registers")
    b = direct_driver_block(profile=GW, access="direct", capabilities={"sell_from_battery": True},
                            local_switch=True, limits=limits)
    assert b == {"id": "goodwe-et", "model": "mode_setpoint", "local_switch_enabled": True, "access": "direct",
                 "capabilities": {"sell_from_battery": True},
                 "limits": {"rated_power_w": 10000, "source": "registers"}}


def test_manual_rated_power_wins_with_source_user():
    from custom_components.volcast.control.runtime import direct_limits
    target = {"rated_power_w": 10000}
    assert direct_limits({"rated_power_w": 8000}, target) == {"rated_power_w": 8000, "source": "user"}
    assert direct_limits({}, target) == {"rated_power_w": 10000, "source": "registers"}
    assert direct_limits({}, {}) is None


def test_direct_capabilities_from_probe_minus_unreadable():
    caps = direct_capabilities(GW, ALL_TRUE, ("soc_max",))
    assert caps["sell_from_battery"] and caps["set_power_w"] and caps["limit_export"]
    assert caps["set_soc_ceiling"] is False                          # bez odczytu zwrotnego
    no_power = direct_capabilities(GW, {**ALL_TRUE, "power_w": False}, ())
    assert not any(no_power.values())                                # grupa tryb+moc niekompletna
    assert not any(direct_capabilities(GW, ALL_TRUE, ("mode",)).values())
    tou = direct_capabilities(DEYE, {"tou": True}, ())
    assert tou["force_charge_from_grid"] and not tou["sell_from_battery"]
    assert not any(direct_capabilities(DEYE, {"tou": False}, ()).values())


def test_telemetry_has_no_host_or_serial():
    conn = FakeConn(GW)
    sender, cloud = _sender(conn, caps=direct_capabilities(GW, ALL_TRUE, ()),
                            limits=executor_limits(rated_power_w=8000.0, source="registers"))
    text = json.dumps(_flush(sender, cloud))
    serial_words = "".join(chr(w >> 8) + chr(w & 0xFF) for w in (conn.reading.image.words(35003, 8)))
    for secret in ("192.168.1.50", "8899", "0123456789abcdef", SALT.hex(), serial_words.strip("\x00 ")):
        assert secret not in text
    assert "serial" not in text and "host" not in text and "device_fp" not in text


def test_entity_mode_telemetry_unchanged():
    from tests.control.ha_fakes import GOODWE_ENTITIES as E, goodwe_hass
    hass = goodwe_hass()
    hass.config = SimpleNamespace(time_zone="Europe/Warsaw", country="PL")
    entry = SimpleNamespace(entry_id="e1", options={"control_mode": "entities"})
    cloud = Cloud()
    sender = TelemetrySender(hass, entry, cloud, Executor(), choice=ProfileChoice(GW, "goodwe", "GW8KN-ET"),
                             profile_map=E, manual_map={}, grid_negate=False, limits=None, utcnow=lambda: NOW)
    assert asyncio.run(sender.async_flush()) is True
    r = cloud.sent[-1]
    assert "access" not in r["driver"] and "direct" not in r["extra"]["volcast"]
    assert r["driver"]["capabilities"] and "battery_soc" in r
    # tryb encji bez encji liczników i bez limitów: bez bloku liczników (chmura liczy ze średniej mocy)
    assert "counters" not in r["extra"] and "limits" not in r["driver"]
