"""Transport `ha_modbus` na prawdziwym rdzeniu HA: kontrakt sprawdzenia czujników z PRAWDZIWYMI klasami
encji integracji `modbus` oraz szyna usług/zdarzeń (słuchacz `call_service`, rozpoznanie własnego zapisu).

Główny zestaw (`tests/`) stoi na atrapach o tych samych nazwach — zmiana w HA przeszłaby tam niezauważona.
"""
from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest
import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import EntityComponent

from custom_components.volcast.core.transports.ha_modbus import (
    SLAVE_SENSOR, HaModbusConfig, HaModbusTransport, WriteUnconfirmed, bus_sensor_problem)
from custom_components.volcast.core.transports.modbus_frames import FC_WRITE_MULTIPLE

# Biblioteki magistrali integracji `modbus` (zależności z jej manifestu, nie pakietu testowego HA).
_BUS_LIBS = {
    "pymodbus": ("pymodbus", "pymodbus.client", "pymodbus.exceptions", "pymodbus.framer", "pymodbus.pdu"),
    "modbus_connection": ("modbus_connection", "modbus_connection.tmodbus"),       # HA ≥ 2026.9
}


class _Stub(types.ModuleType):
    """Biblioteki magistrali nie są zależnościami pakietu testowego HA; klasy encji ich nie używają."""

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        cls = type(name, (Exception,), {})
        setattr(self, name, cls)
        return cls


@pytest.fixture
def modbus_classes(monkeypatch):
    """Prawdziwe `SENSOR_SCHEMA`, `struct_validator`, `ModbusRegisterSensor`, `SlaveSensor` z HA; moduły
    `modbus` usuwane z `sys.modules` po teście, atrapy bibliotek magistrali — przez `monkeypatch`."""
    before = set(sys.modules)
    for lib, names in _BUS_LIBS.items():
        try:
            __import__(lib)
        except ImportError:
            for name in names:
                monkeypatch.setitem(sys.modules, name, _Stub(name))
    try:
        try:
            from homeassistant.components.modbus.schemas import SENSOR_SCHEMA          # HA ≥ 2026.9
        except ImportError:
            from homeassistant.components.modbus import SENSOR_SCHEMA
        from homeassistant.components.modbus.sensor import ModbusRegisterSensor, SlaveSensor
        from homeassistant.components.modbus.validators import struct_validator
    except Exception as err:  # noqa: BLE001
        pytest.skip(f"Home Assistant modbus classes not importable: {type(err).__name__}: {err}")
    yield SimpleNamespace(SENSOR_SCHEMA=SENSOR_SCHEMA, struct_validator=struct_validator,
                          ModbusRegisterSensor=ModbusRegisterSensor, SlaveSensor=SlaveSensor)
    for name in set(sys.modules) - before:
        if name.startswith("homeassistant.components.modbus"):
            sys.modules.pop(name, None)


async def test_sensor_check_contract_with_real_modbus_entities(hass: HomeAssistant, modbus_classes):
    """Czujnik bazowy przyjęty (także z `slave_count`), `SlaveSensor` odrzucony — na prawdziwych klasach
    i prawdziwym rejestrze encji (publiczne `RegistryEntry.platform`, `EntityComponent.get_entity`)."""
    m = modbus_classes
    entry = m.struct_validator(m.SENSOR_SCHEMA({"name": "reg 146", "address": 146, "unique_id": "volcast_reg_146",
                                                "data_type": "uint16", "slave": 1}))
    hub = SimpleNamespace(name="deye_hub")
    built = {
        "volcast_reg_146": m.ModbusRegisterSensor(hass, hub, entry, 0),
        "volcast_reg_147": m.ModbusRegisterSensor(hass, hub, {**entry, "slave_count": 1}, 1),
        "volcast_reg_146_1": m.SlaveSensor(SimpleNamespace(), 0, entry),
    }
    registry = er.async_get(hass)
    entities = {registry.async_get_or_create("sensor", "modbus", uid).entity_id: ent for uid, ent in built.items()}
    other = registry.async_get_or_create("sensor", "template", "volcast_reg_148").entity_id
    assert hasattr(EntityComponent, "get_entity")
    view = SimpleNamespace(data={"entity_registry": registry,
                                 "sensor": SimpleNamespace(get_entity=entities.get)})
    got = {uid: bus_sensor_problem(view, eid) for uid, eid in zip(built, entities)}
    assert got == {"volcast_reg_146": None, "volcast_reg_147": None, "volcast_reg_146_1": SLAVE_SENSOR}
    assert bus_sensor_problem(view, other) == "not_modbus_platform"
    assert bus_sensor_problem(view, "sensor.not_registered") == "not_in_entity_registry"


async def test_real_bus_listener_and_own_write_identity(hass: HomeAssistant):
    """Prawdziwy `async_listen` przyjmuje słuchacza i filtr (wymaga `@callback`), zdarzenie `call_service`
    niesie ten sam obiekt danych (własny zapis nie jest obcy), a obcy zapis na naszym hubie — jest."""
    calls = []

    async def write_register(call):
        calls.append(dict(call.data))
    hass.services.async_register("modbus", "write_register", write_register, schema=vol.Schema({
        vol.Optional("hub", default="modbus_hub"): str,
        vol.Exclusive("slave", "unit"): int,
        vol.Exclusive("unit", "unit"): int,
        vol.Required("address"): int,
        vol.Required("value"): vol.Any(int, [int]),
    }, extra=vol.PREVENT_EXTRA))
    t = HaModbusTransport(hass, HaModbusConfig(hub="deye_hub", device_address=1,
                                               registers={146: "sensor.volcast_reg_146"}))
    with pytest.raises(WriteUnconfirmed):
        await t.write(146, [0xFF], function=FC_WRITE_MULTIPLE)
    await hass.async_block_till_done()
    assert calls == [{"hub": "deye_hub", "slave": 1, "address": 146, "value": [0xFF]}]
    assert t.conflict is False and t.foreign_writes == 0
    await hass.services.async_call("modbus", "write_register",
                                   {"hub": "deye_hub", "slave": 1, "address": 146, "value": 0xFE}, blocking=True)
    await hass.async_block_till_done()
    assert t.conflict is True and t.foreign_writes == 1
    await t.close()
