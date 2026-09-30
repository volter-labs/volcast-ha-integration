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
