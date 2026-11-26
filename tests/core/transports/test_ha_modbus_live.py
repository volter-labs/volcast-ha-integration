"""Transport `ha_modbus` po restarcie HA: czujnik `modbus` (`RestoreSensor`) przy dodaniu encji zapisuje wartość
sprzed restartu ze ŚWIEŻYM stemplem i odpytuje magistralę dopiero po połączeniu huba. Rejestr jest „żywy”
(czytelny) dopiero, gdy stan czujnika zapisano PO starcie transportu, a dla encji dodanej (albo podmienionej przy
przeładowaniu `modbus`) po starcie — dopiero przy KOLEJNYM zapisie stanu niż ten z dodania (odtworzenie)."""
from __future__ import annotations

import pytest

from custom_components.volcast.core.transports.ha_modbus import (
    HaModbusConfig, HaModbusTransport, MissingRegister, RegisterNotLive)

from .test_ha_modbus import HUB, UNIT, Inverter, ModbusRegisterSensor
from .helpers import FakeClock


def _transport(inv, clock, **kw):
    cfg = HaModbusConfig(hub=HUB, device_address=UNIT, registers=inv.entities)
    return HaModbusTransport(inv.hass, cfg, clock=clock, sleep=clock.sleep, utcnow=inv.utcnow, **kw)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def inv(clock):
    return Inverter(clock, {146: 0xFE, 147: 0, 148: 100})


@pytest.mark.asyncio
async def test_state_written_before_the_transport_started_is_not_live(inv, clock):
    t = _transport(inv, clock)                       # stany z dodania encji (odtworzone) — sprzed startu
    with pytest.raises(RegisterNotLive) as err:
        await t.read(146, 3)
    assert isinstance(err.value, MissingRegister) and err.value.addrs == (146, 147, 148)
    inv.refresh(146)                                 # pierwsze odpytanie huba po starcie
    assert await t.read(146, 1) == [0xFE]
    with pytest.raises(RegisterNotLive):
        await t.read(147, 2)


@pytest.mark.asyncio
async def test_entity_added_after_the_start_needs_a_second_state_write(inv, clock):
    """Encja dodana po starcie transportu (HA jeszcze startuje): odtworzony stan ze stemplem PO starcie to nie
    odczyt magistrali — dopiero następny zapis stanu."""
    eid = inv.entities[146]
    inv.hass.data["sensor"].entities.pop(eid)        # encja jeszcze niezaładowana
    t = _transport(inv, clock)
    inv.add_entity(eid)                              # dodanie encji: odtworzenie stanu
    inv.bank[146] = 0x11                             # (falownik zmieniony podczas przerwy — stan pokazuje starą)
    inv.hass.states.set(eid, str(0xFE), inv.utcnow())
    with pytest.raises(RegisterNotLive):
        await t.read(146, 1)
    inv.refresh(146)                                 # pierwsze prawdziwe odpytanie
    assert await t.read(146, 1) == [0x11]


@pytest.mark.asyncio
async def test_modbus_reload_replacing_the_entity_needs_a_fresh_state(inv, clock):
    t = _transport(inv, clock)
    for a in (146, 147, 148):
        inv.refresh(a)
    assert await t.read(146, 3) == [0xFE, 0, 100]
    eid = inv.entities[147]
    inv.hass.data["sensor"].entities[eid] = ModbusRegisterSensor()     # `modbus.reload`: nowy obiekt encji
    clock.advance(0.01)
    inv.hass.states.set(eid, "0", inv.utcnow())                         # odtworzony stan z nowym stemplem
    with pytest.raises(RegisterNotLive) as err:
        await t.read(146, 3)
    assert err.value.addrs == (147,)
    inv.refresh(147)
    assert await t.read(146, 3) == [0xFE, 0, 100]


@pytest.mark.asyncio
async def test_read_only_preview_may_read_restored_states(inv, clock):
    """Kreator (tylko odczyt, bez zapisu i bez migawki): `live_only=False` czyta od razu."""
    t = _transport(inv, clock, live_only=False)
    assert await t.read(146, 3) == [0xFE, 0, 100]


@pytest.mark.asyncio
async def test_a_missing_value_still_wins_over_not_live(inv, clock):
    t = _transport(inv, clock)
    inv.hass.states.set(inv.entities[147], "unavailable", inv.utcnow())
    with pytest.raises(MissingRegister) as err:
        await t.read(146, 3)
    assert not isinstance(err.value, RegisterNotLive) and err.value.addrs == (147,)
