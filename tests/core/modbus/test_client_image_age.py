"""Wiek odczytu stanu na transporcie, który zna wiek swojego obrazu rejestrów (`ha_modbus`): chwila odczytu =
NAJSTARSZY stempel czujników wszystkich przeczytanych bloków (zegar monotoniczny), nie start cyklu."""
from __future__ import annotations

import pytest

from custom_components.volcast.core.modbus.blocks import read_plan
from custom_components.volcast.core.modbus.client import RegisterClient
from custom_components.volcast.core.profile import load_builtin
from custom_components.volcast.core.transports.ha_modbus import HaModbusConfig, HaModbusTransport
from tests.core.transports.helpers import FakeClock
from tests.core.transports.test_ha_modbus import Inverter
from tests.sim.fixtures import deye_words

DEYE = load_builtin("deye-sg")


def _words() -> dict[int, int]:
    words = deye_words()
    for start, n, *_ in read_plan(DEYE):
        for a in range(start, start + n):
            words.setdefault(a, 0)                       # słowa bloków bez pola profilu (np. 589)
    return words


def _client(clock: FakeClock):
    inv = Inverter(clock, _words())
    t = HaModbusTransport(inv.hass, HaModbusConfig(hub="deye_hub", device_address=1, registers=inv.entities),
                          clock=clock, sleep=clock.sleep, utcnow=inv.utcnow)
    inv.poll_all()                                       # pierwsze odpytanie po starcie transportu
    return inv, RegisterClient(t, DEYE, clock=clock)


@pytest.mark.asyncio
async def test_reading_age_is_the_oldest_sensor_stamp_not_the_poll_start():
    clock = FakeClock()
    inv, client = _client(clock)
    oldest = clock.now                                   # ostatni stempel przed odświeżeniem części czujników
    clock.advance(20.0)
    for a in range(146, 178):
        inv.refresh(a)                                   # blok TOU świeży, reszta sprzed 20 s
    start = clock.now
    r = await client.read_state()
    assert r.at_mono <= oldest and r.at_mono < start - 19.0


@pytest.mark.asyncio
async def test_fully_refreshed_image_is_as_young_as_its_oldest_sensor():
    clock = FakeClock()
    inv, client = _client(clock)
    clock.advance(30.0)
    for a in sorted(inv.entities):
        inv.refresh(a)
    polled = [a for start, n, *_ in read_plan(DEYE) for a in range(start, start + n)]
    first = min(inv.hass.states.get(inv.entities[a]).last_updated for a in polled)
    r = await client.read_state()
    age = (inv.utcnow() - first).total_seconds()
    assert r.at_mono == pytest.approx(clock.now - age, abs=1e-6)


@pytest.mark.asyncio
async def test_block_without_a_sensor_stamp_is_not_read():
    clock = FakeClock()
    inv, client = _client(clock)
    st = inv.hass.states.get(inv.entities[625])
    st.last_updated = st.last_reported = None            # wiek nieznany — blok bez odczytu, nie „świeży”
    r = await client.read_state()
    assert r.values.get("grid_power_w") is None and r.values.get("soc") is not None
    assert {"addr": 625, "count": 1, "ok": False} in client.last_frames
