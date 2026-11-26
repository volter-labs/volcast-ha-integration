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


async def test_register_map_and_hub_writer_contract_on_real_registry_and_platforms(hass: HomeAssistant):
    """Mapa z rejestru encji (`registers_from_registry`), czujniki YAML bez `unique_id`
    (`unregistered_modbus_sensors`) i encje zapisu platformy `modbus` (`hub_writers`) — na prawdziwym
    `EntityRegistry.entities`, `EntityComponent.get_entity`/`entities`, `EntityPlatform.platform_name`
    i `hass.states.async_entity_ids`. Atrapy w `tests/` mają te same nazwy — zmiana w HA przeszłaby tam."""
    import logging
    from datetime import timedelta

    from homeassistant.components.sensor import SensorEntity
    from homeassistant.components.switch import SwitchEntity
    from homeassistant.helpers.entity_platform import EntityPlatform

    from custom_components.volcast.control.direct import hub_writers
    from custom_components.volcast.core.control.conflict import HubWriter
    from custom_components.volcast.core.transports.ha_modbus_map import (
        registers_from_registry, unregistered_modbus_sensors)

    log = logging.getLogger(__name__)
    for domain in ("sensor", "switch"):
        hass.data[domain] = EntityComponent(log, domain, hass)      # jak `async_setup` integracji domeny

    def platform(domain: str, name: str = "modbus") -> EntityPlatform:
        return EntityPlatform(hass=hass, logger=log, domain=domain, platform_name=name, platform=None,
                              scan_interval=timedelta(seconds=30), entity_namespace=None)

    class Raw(SensorEntity):
        _attr_should_poll = False                        # bez zegara odpytywania platformy w teście

        def __init__(self, name, uid=None):
            self._attr_name, self._attr_unique_id, self._attr_native_value = name, uid, 1

    class Writer(SwitchEntity):                          # kształt encji zapisu huba: `_hub.name`, `_device_address`
        _attr_should_poll = False

        def __init__(self, name, hub, unit):
            self._attr_name, self._hub, self._device_address = name, SimpleNamespace(name=hub), unit

        @property
        def is_on(self):
            return False

    await platform("sensor").async_add_entities([Raw("reg 146", "volcast_reg_146"), Raw("reg 0", "volcast_reg_0"),
                                                 Raw("yaml without unique id")])
    await platform("sensor", "template").async_add_entities([Raw("other", "volcast_reg_147"), Raw("plain")])
    await platform("switch").async_add_entities([Writer("export", "deye_hub", 1)])
    await hass.async_block_till_done()

    registers, dupes = registers_from_registry(hass)
    assert {a: e.split(".")[0] for a, e in registers.items()} == {146: "sensor", 0: "sensor"} and dupes == ()
    assert registers[146] == er.async_get(hass).async_get_entity_id("sensor", "modbus", "volcast_reg_146")
    assert unregistered_modbus_sensors(hass) == ("sensor.yaml_without_unique_id",)
    assert hub_writers(hass) == [HubWriter("deye_hub", 1)]
