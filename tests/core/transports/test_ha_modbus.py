"""Transport `ha_modbus`: zapis przez usługę `modbus.write_register` istniejącego huba użytkownika,
odczyt z obrazu rejestrów składanego z jego SUROWYCH czujników (atrapy hass, bez sieci)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import voluptuous as vol

from custom_components.volcast.core.transports.base import LinkDown, RegisterTransport, RequestTimeout
from custom_components.volcast.core.transports.ha_modbus import (
    BACKGROUND_TASK_NAME, CHECK_FAILED, HA_MODBUS_KIND, NOT_LOADED, NOT_MODBUS_PLATFORM, NOT_REGISTERED,
    NOT_SENSOR_DOMAIN, REGISTRY_UNAVAILABLE, SLAVE_SENSOR, HaModbusConfig, HaModbusTransport, MissingRegister,
    RegisterValueError, UnsupportedSensor, WriteUnconfirmed, bus_sensor_problem, validate_ha_modbus_config)
from custom_components.volcast.core.transports.modbus_frames import FC_WRITE_MULTIPLE, FC_WRITE_SINGLE

from .helpers import FakeClock

HUB = "deye_hub"
UNIT = 1

# Kształt schematu usługi jak w HA 2022.4 … dev (2026-11): `slave` albo `unit` (wykluczające się).
SCHEMA_SLAVE = vol.Schema({
    vol.Optional("hub", default="modbus_hub"): str,
    vol.Exclusive("slave", "unit"): int,
    vol.Exclusive("unit", "unit"): int,
    vol.Required("address"): int,
    vol.Required("value"): vol.Any(int, [int]),
})
# HA ≤ 2022.3: tylko `unit` (wymagany).
SCHEMA_UNIT_ONLY = vol.Schema({
    vol.Optional("hub", default="modbus_hub"): str,
    vol.Required("unit"): int,
    vol.Required("address"): int,
    vol.Required("value"): vol.Any(int, [int]),
})
# Ewentualna przyszła nazwa (w HA 2026.x `device_address` to już klucz konfiguracji YAML czujnika).
SCHEMA_DEVICE_ADDRESS = vol.Schema({
    vol.Optional("hub", default="modbus_hub"): str,
    vol.Optional("device_address"): int,
    vol.Required("address"): int,
    vol.Required("value"): vol.Any(int, [int]),
})


class ServiceNotFound(Exception):
    pass


class FakeServices:
    def __init__(self, hass) -> None:
        self._hass = hass
        self.calls: list[tuple[str, str, dict, bool]] = []
        self.handlers: dict[tuple[str, str], object] = {}
        self.schemas: dict[tuple[str, str], object] = {}

    def has_service(self, domain: str, service: str) -> bool:
        return (domain, service) in self.handlers

    def async_services_for_domain(self, domain: str) -> dict:
        return {s: SimpleNamespace(schema=self.schemas.get((d, s)))
                for (d, s) in self.handlers if d == domain}

    async def async_call(self, domain, service, service_data=None, blocking=False, context=None, **_kw):
        data = dict(service_data or {})
        self.calls.append((domain, service, data, blocking))
        handler = self.handlers.get((domain, service))
        if handler is None:
            raise ServiceNotFound(f"{domain}.{service}")
        schema = self.schemas.get((domain, service))
        if schema is not None:
            data = schema(data)                     # vol.Invalid — przed wykonaniem, jak w HA
        # HA: zdarzenie `call_service` po walidacji, z ORYGINALNYM słownikiem danych, przed wykonaniem;
        # słuchacz asynchroniczny bywa uruchomiony później (`late_events`)
        event = SimpleNamespace(data={"domain": domain, "service": service, "service_data": service_data})
        if not self._hass.late_events:
            await self._hass.bus.fire(event)
        await handler(data)
        if self._hass.late_events:
            await self._hass.bus.fire(event)

    def writes(self) -> list[dict]:
        return [c[2] for c in self.calls if c[:2] == ("modbus", "write_register")]


class FakeBus:
    def __init__(self) -> None:
        self.listeners: list[tuple[str, object]] = []

    def async_listen(self, event_type, listener, event_filter=None):
        # HA: słuchacz bez `@callback` dostałby zadanie na każde zdarzenie — wymagamy oznaczenia
        assert getattr(listener, "_hass_callback", False) and getattr(event_filter, "_hass_callback", False)
        item = (event_type, listener, event_filter)
        self.listeners.append(item)
        return lambda: self.listeners.remove(item)

    async def fire(self, event, event_type="call_service") -> None:
        for et, cb, flt in list(self.listeners):
            if et == event_type and (flt is None or flt(event.data)):
                cb(event)


class ModbusRegisterSensor:                         # kształt czujnika bazowego HA: czyta przez `_hub`
    _hub = object()


class SlaveSensor:                                  # `slave_count`/`virtual_count`: encja koordynatora
    coordinator = object()


class FakeRegistry:
    def __init__(self) -> None:
        self.entries: dict[str, SimpleNamespace] = {}

    def async_get(self, entity_id):
        return self.entries.get(entity_id)


class FakeComponent:
    def __init__(self) -> None:
        self.entities: dict[str, object] = {}

    def get_entity(self, entity_id):
        return self.entities.get(entity_id)


class FakeStates:
    def __init__(self) -> None:
        self._s: dict[str, SimpleNamespace] = {}

    def get(self, entity_id):
        return self._s.get(entity_id)

    def set(self, entity_id, state, stamp: datetime, *, reported: datetime | None = None) -> None:
        self._s[entity_id] = SimpleNamespace(entity_id=entity_id, state=state, last_updated=stamp,
                                             last_reported=reported or stamp)


class Inverter:
    """Atrapa: bank rejestrów + hub HA (`modbus.write_register`) + czujniki surowe odświeżane na żądanie."""

    def __init__(self, clock: FakeClock, registers: dict[int, int], *, schema=SCHEMA_SLAVE) -> None:
        self.clock = clock
        self.bank = dict(registers)
        self.hass = SimpleNamespace(states=FakeStates(), services=None, bus=FakeBus(), late_events=False,
                                    data={"entity_registry": FakeRegistry(), "sensor": FakeComponent()})
        self.hass.services = FakeServices(self.hass)
        self.entities = {a: f"sensor.volcast_reg_{a}" for a in registers}
        for eid in self.entities.values():
            self.add_entity(eid)
        self.drop_writes = False                    # HA połyka błąd magistrali — usługa wraca bez błędu
        self.refresh_on_request = True
        self.hass.services.handlers[("modbus", "write_register")] = self._write
        self.hass.services.schemas[("modbus", "write_register")] = schema
        self.hass.services.handlers[("homeassistant", "update_entity")] = self._update
        for a in registers:
            self.refresh(a)

    def poll_all(self) -> None:
        """Odpytanie wszystkich czujników przez hub (po starcie transportu — stany przestają być odtworzone)."""
        for a, eid in self.entities.items():
            st = self.hass.states.get(eid)
            if st is not None and st.state == str(self.bank.get(a)):
                self.refresh(a)

    def add_entity(self, eid: str, *, platform: str = "modbus", entity=None) -> None:
        self.hass.data["entity_registry"].entries[eid] = SimpleNamespace(platform=platform)
        self.hass.data["sensor"].entities[eid] = entity if entity is not None else ModbusRegisterSensor()

    def utcnow(self) -> datetime:
        return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=self.clock.now)

    def refresh(self, addr: int) -> None:
        self.clock.advance(0.01)                    # odczyt na magistrali trwa — stan stemplowany później
        self.hass.states.set(self.entities[addr], str(self.bank[addr]), self.utcnow())

    async def _write(self, data: dict) -> None:
        if self.drop_writes:
            return
        value = data["value"]
        words = value if isinstance(value, list) else [value]
        for i, v in enumerate(words):
            self.bank[data["address"] + i] = v

    async def _update(self, data: dict) -> None:
        if not self.refresh_on_request:
            return
        ids = data["entity_id"]
        for a, eid in self.entities.items():
            if eid in ids:
                self.refresh(a)


def _transport(inv: Inverter, clock: FakeClock, *, registers=None, **kw) -> HaModbusTransport:
    cfg = HaModbusConfig(hub=HUB, device_address=UNIT,
                         registers=registers if registers is not None else inv.entities, **kw)
    t = HaModbusTransport(inv.hass, cfg, clock=clock, sleep=clock.sleep, utcnow=inv.utcnow)
    inv.poll_all()                                  # pierwsze odpytanie huba po starcie (stany „żywe”)
    return t


@pytest.fixture
def deye_profile():
    from custom_components.volcast.core.profile import load_builtin
    return load_builtin("deye-sg")


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def inv(clock):
    return Inverter(clock, {146: 0xFE, 148: 100, 149: 200, 150: 300, 166: 55})


# ── zapis ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_three_register_write_is_one_service_call_with_a_list(inv, clock):
    t = _transport(inv, clock)
    sent = []
    with pytest.raises(WriteUnconfirmed):
        await t.write(148, [1, 2, 3], function=FC_WRITE_MULTIPLE, on_send=lambda: sent.append(1))
    assert inv.hass.services.writes() == [{"hub": HUB, "slave": UNIT, "address": 148, "value": [1, 2, 3]}]
    assert [c[3] for c in inv.hass.services.calls if c[1] == "write_register"] == [True]   # blocking
    assert sent == [1] and t.stats.requests == 1
    assert [inv.bank[a] for a in (148, 149, 150)] == [1, 2, 3]


@pytest.mark.asyncio
async def test_fc16_single_register_still_sends_a_list(inv, clock):
    t = _transport(inv, clock)
    with pytest.raises(WriteUnconfirmed):
        await t.write(146, [0xFF], function=FC_WRITE_MULTIPLE)
    assert inv.hass.services.writes()[0]["value"] == [0xFF]


@pytest.mark.asyncio
async def test_fc6_single_register_sends_a_scalar(inv, clock):
    t = _transport(inv, clock)
    with pytest.raises(WriteUnconfirmed):
        await t.write(166, [60], function=FC_WRITE_SINGLE)
    assert inv.hass.services.writes()[0]["value"] == 60


@pytest.mark.asyncio
@pytest.mark.parametrize("values,function", [
    ([1, 2], FC_WRITE_SINGLE),          # FC 6 pisze jeden rejestr
    ([], FC_WRITE_MULTIPLE),
    ([70000], FC_WRITE_MULTIPLE),       # słowo spoza u16
    ([-1], FC_WRITE_MULTIPLE),
    ([1], 3),                           # nie funkcja zapisu
])
async def test_bad_write_requests_never_reach_the_service(inv, clock, values, function):
    t = _transport(inv, clock)
    with pytest.raises(ValueError):
        await t.write(148, values, function=function)
    assert inv.hass.services.calls == []


@pytest.mark.asyncio
async def test_device_address_param_falls_back_to_legacy_unit(clock):
    inv = Inverter(clock, {146: 0xFE}, schema=SCHEMA_UNIT_ONLY)
    t = _transport(inv, clock)
    with pytest.raises(WriteUnconfirmed):
        await t.write(146, [0xFF], function=FC_WRITE_MULTIPLE)
    assert inv.hass.services.writes() == [{"hub": HUB, "unit": UNIT, "address": 146, "value": [0xFF]}]
    assert inv.bank[146] == 0xFF


@pytest.mark.asyncio
async def test_device_address_param_follows_a_schema_that_declares_device_address(clock):
    inv = Inverter(clock, {146: 0xFE}, schema=SCHEMA_DEVICE_ADDRESS)
    t = _transport(inv, clock)
    with pytest.raises(WriteUnconfirmed):
        await t.write(146, [0xFF], function=FC_WRITE_MULTIPLE)
    assert inv.hass.services.writes()[0]["device_address"] == UNIT and inv.bank[146] == 0xFF


@pytest.mark.asyncio
async def test_device_address_param_defaults_to_slave_without_schema(clock):
    inv = Inverter(clock, {146: 0xFE}, schema=None)
    t = _transport(inv, clock)
    with pytest.raises(WriteUnconfirmed):
        await t.write(146, [0xFF], function=FC_WRITE_MULTIPLE)
    assert inv.hass.services.writes()[0]["slave"] == UNIT


@pytest.mark.asyncio
async def test_explicit_unit_overrides_configured_device_address(inv, clock):
    t = _transport(inv, clock)
    with pytest.raises(WriteUnconfirmed):
        await t.write(146, [0xFF], function=FC_WRITE_MULTIPLE, unit=7)
    assert inv.hass.services.writes()[0]["slave"] == 7


@pytest.mark.asyncio
async def test_missing_modbus_service_is_link_down_and_not_counted(inv, clock):
    del inv.hass.services.handlers[("modbus", "write_register")]
    t = _transport(inv, clock)
    sent = []
    with pytest.raises(LinkDown):
        await t.write(146, [0xFF], function=FC_WRITE_MULTIPLE, on_send=lambda: sent.append(1))
    assert sent == [] and inv.hass.services.calls == []


class ServiceValidationError(Exception):
    pass


class _FakeServiceValidationError(Exception):      # nazwa jak atrapa w tests/conftest.py
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [vol.Invalid("schema"), vol.MultipleInvalid([vol.Invalid("x")]),
                                 ServiceValidationError("unknown hub"), ServiceNotFound("race"),
                                 _FakeServiceValidationError("stub")])
async def test_rejected_service_data_is_link_down_and_leaves_no_pending_write(inv, clock, exc):
    t = _transport(inv, clock)

    async def _reject(_data):
        raise exc                                   # odrzucone przed wykonaniem — nic nie poszło
    inv.hass.services.handlers[("modbus", "write_register")] = _reject
    with pytest.raises(LinkDown):
        await t.write(146, [0xFF], function=FC_WRITE_MULTIPLE)
    inv.refresh_on_request = False
    assert await t.read(146, 1) == [0xFE]          # brak zapisu w toku → bez czekania na świeżość


@pytest.mark.asyncio
async def test_failed_service_call_is_unconfirmed_and_read_back_waits_for_fresh_state(inv, clock):
    t = _transport(inv, clock)

    async def _boom(data):
        inv.bank[146] = data["value"][0]            # zapis doszedł, a usługa zgłosiła błąd
        raise RuntimeError("hub error")
    inv.hass.services.handlers[("modbus", "write_register")] = _boom
    with pytest.raises(WriteUnconfirmed):
        await t.write(146, [0xFF], function=FC_WRITE_MULTIPLE)
    assert await t.read(146, 1) == [0xFF]


# ── odczyt z czujników ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_assembles_register_image_from_sensors(inv, clock):
    t = _transport(inv, clock)
    assert await t.read(148, 3) == [100, 200, 300]
    assert t.stats.last_ok_mono == clock.now and inv.hass.services.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["unavailable", "unknown", "abc", "nan", "inf", ""])
async def test_unavailable_or_non_numeric_sensor_is_a_missing_register_not_zero(inv, clock, state):
    t = _transport(inv, clock)
    inv.hass.states.set(inv.entities[149], state, inv.utcnow())
    with pytest.raises(MissingRegister) as err:
        await t.read(148, 3)
    assert err.value.addrs == (149,)


@pytest.mark.asyncio
async def test_unmapped_or_absent_entity_is_a_missing_register(inv, clock):
    inv.add_entity("sensor.gone")                   # zarejestrowany czujnik bez stanu
    t = _transport(inv, clock, registers={148: inv.entities[148], 150: "sensor.gone"})
    with pytest.raises(MissingRegister) as err:
        await t.read(148, 3)
    assert err.value.addrs == (149, 150)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["65536", "-32769", "12.5", "1e9"])
async def test_value_outside_u16_i16_is_a_configuration_error(inv, clock, state):
    t = _transport(inv, clock)
    inv.hass.states.set(inv.entities[166], state, inv.utcnow())
    with pytest.raises(RegisterValueError) as err:
        await t.read(166, 1)
    assert err.value.addrs == (166,)


@pytest.mark.asyncio
@pytest.mark.parametrize("state,word", [("-1", 0xFFFF), ("-32768", 0x8000), ("65535", 0xFFFF),
                                        ("42.0", 42), ("0", 0)])
async def test_i16_and_integral_float_states_map_to_words(inv, clock, state, word):
    t = _transport(inv, clock)
    inv.hass.states.set(inv.entities[166], state, inv.utcnow())
    assert await t.read(166, 1) == [word]


@pytest.mark.asyncio
async def test_read_from_another_device_or_input_registers_is_missing(inv, clock):
    t = _transport(inv, clock)
    with pytest.raises(MissingRegister):
        await t.read(146, 1, unit=UNIT + 1)
    with pytest.raises(MissingRegister):
        await t.read(146, 1, fc=4)
    assert await t.read(146, 1, unit=UNIT) == [0xFE]


# ── świeżość odczytu zwrotnego ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_back_after_write_waits_for_a_refresh_and_requests_one(inv, clock):
    t = _transport(inv, clock)
    with pytest.raises(WriteUnconfirmed):
        await t.write(148, [1, 2, 3], function=FC_WRITE_MULTIPLE)
    assert await t.read(148, 3) == [1, 2, 3]
    upd = [c[2] for c in inv.hass.services.calls if c[:2] == ("homeassistant", "update_entity")]
    assert upd == [{"entity_id": [inv.entities[148], inv.entities[149], inv.entities[150]]}]


@pytest.mark.asyncio
async def test_sensor_not_refreshed_after_write_is_an_unknown_read_back(inv, clock):
    t = _transport(inv, clock, readback_timeout_s=30.0, readback_poll_s=1.0)
    inv.refresh_on_request = False
    with pytest.raises(WriteUnconfirmed):
        await t.write(146, [0xFF], function=FC_WRITE_MULTIPLE)
    with pytest.raises(RequestTimeout):
        await t.read(146, 1)                       # stary stan (0xFE) nie jest odczytem zwrotnym
    assert t.stats.timeouts == 1 and t.stats.consecutive_timeouts == 1
    assert sum(clock.sleeps) <= 30.0 + 1.0
    # deadline liczony od zapisu: ponowny odczyt po terminie nie czeka drugi raz
    before = len(clock.sleeps)
    with pytest.raises(RequestTimeout):
        await t.read(146, 1)
    assert len(clock.sleeps) == before


@pytest.mark.asyncio
async def test_state_stamped_exactly_at_write_time_is_not_fresh(inv, clock):
    t = _transport(inv, clock, readback_timeout_s=5.0, readback_poll_s=1.0)
    inv.refresh_on_request = False
    with pytest.raises(WriteUnconfirmed):
        await t.write(146, [0xFF], function=FC_WRITE_MULTIPLE)
    inv.hass.states.set(inv.entities[146], "255", inv.utcnow())       # ten sam stempel co zapis
    with pytest.raises(RequestTimeout):
        await t.read(146, 1)


@pytest.mark.asyncio
async def test_natural_refresh_after_write_is_accepted(inv, clock):
    t = _transport(inv, clock, readback_poll_s=1.0)
    inv.refresh_on_request = False
    with pytest.raises(WriteUnconfirmed):
        await t.write(146, [0xFF], function=FC_WRITE_MULTIPLE)

    async def _sleep(dt):
        await FakeClock.sleep(clock, dt)
        if len(clock.sleeps) == 3:
            inv.refresh(146)                        # odpytanie huba po kilku sekundach
    t._sleep = _sleep
    assert await t.read(146, 1) == [0xFF]
    assert t.stats.consecutive_timeouts == 0


@pytest.mark.asyncio
async def test_unchanged_value_counts_as_fresh_via_last_reported(inv, clock):
    """HA nie przesuwa `last_updated`, gdy wartość się nie zmienia — świeżość z `last_reported`."""
    t = _transport(inv, clock)
    inv.refresh_on_request = False
    old = inv.hass.states.get(inv.entities[166]).last_updated
    with pytest.raises(WriteUnconfirmed):
        await t.write(166, [55], function=FC_WRITE_MULTIPLE)              # ta sama wartość
    clock.advance(1.0)
    inv.hass.states.set(inv.entities[166], "55", old, reported=inv.utcnow())
    assert await t.read(166, 1) == [55]


@pytest.mark.asyncio
async def test_silently_dropped_write_reads_back_the_old_value_after_refresh(inv, clock):
    t = _transport(inv, clock)
    inv.drop_writes = True
    with pytest.raises(WriteUnconfirmed):
        await t.write(146, [0xFF], function=FC_WRITE_MULTIPLE)
    assert await t.read(146, 1) == [0xFE]          # świeży odczyt, rozstrzyga pisarz (≠ zamówiona → ERROR)


@pytest.mark.asyncio
async def test_sensor_going_unavailable_while_waiting_is_missing(inv, clock):
    t = _transport(inv, clock, readback_poll_s=1.0)
    inv.refresh_on_request = False
    with pytest.raises(WriteUnconfirmed):
        await t.write(146, [0xFF], function=FC_WRITE_MULTIPLE)
    clock.advance(1.0)
    inv.hass.states.set(inv.entities[146], "unavailable", inv.utcnow())
    with pytest.raises(MissingRegister):
        await t.read(146, 1)


@pytest.mark.asyncio
async def test_untouched_registers_do_not_wait(inv, clock):
    t = _transport(inv, clock)
    inv.refresh_on_request = False
    with pytest.raises(WriteUnconfirmed):
        await t.write(146, [0xFF], function=FC_WRITE_MULTIPLE)
    assert await t.read(148, 3) == [100, 200, 300] and clock.sleeps == []


# ── cykl życia, konfiguracja, zgodność z protokołem ────────────────────────


@pytest.mark.asyncio
async def test_closed_transport_refuses_io(inv, clock):
    t = _transport(inv, clock)
    await t.reset_channel()
    await t.close()
    with pytest.raises(LinkDown):
        await t.read(146, 1)
    with pytest.raises(LinkDown):
        await t.write(146, [1], function=FC_WRITE_MULTIPLE)


def test_protocol_shape(inv, clock):
    t: RegisterTransport = _transport(inv, clock)
    assert t.kind == HA_MODBUS_KIND == "ha_modbus"
    assert all(callable(getattr(t, m)) for m in ("read", "write", "reset_channel", "close"))
    assert t.stats.requests == 0 and t.cfg.hub == HUB


@pytest.mark.parametrize("kw", [
    {"hub": ""}, {"hub": 5}, {"device_address": 0}, {"device_address": 248}, {"device_address": True},
    {"registers": {-1: "sensor.a"}}, {"registers": {65536: "sensor.a"}}, {"registers": {1: "nodot"}},
    {"registers": {1: ""}}, {"readback_timeout_s": 0}, {"readback_poll_s": -1}, {"conflict_hold_s": 0},
    {"write_timeout_s": float("nan")},
])
def test_config_validation(kw):
    base = {"hub": HUB, "device_address": UNIT, "registers": {146: "sensor.volcast_reg_146"}}
    with pytest.raises(ValueError):
        validate_ha_modbus_config(HaModbusConfig(**{**base, **kw}))


# ── z pisarzem rejestrów (profil Deye) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_register_writer_ok_when_sensor_refreshes(clock, deye_profile):
    from custom_components.volcast.core.modbus.client import RegisterClient
    from custom_components.volcast.core.modbus.writer import RegisterWriter
    from custom_components.volcast.core.registers import RegisterWrite
    from custom_components.volcast.core.write_sequence import OK
    inv = Inverter(clock, {146: 0xFE, 166: 55})
    w = RegisterWriter(RegisterClient(_transport(inv, clock), deye_profile), deye_profile)
    assert await w.async_write(RegisterWrite("tou.1.soc", 166, 60)) == OK
    assert inv.bank[166] == 60 and len(inv.hass.services.writes()) == 1


@pytest.mark.asyncio
async def test_register_writer_error_not_denied_when_write_silently_dropped(clock, deye_profile):
    from custom_components.volcast.core.modbus.client import RegisterClient
    from custom_components.volcast.core.modbus.writer import RegisterWriter
    from custom_components.volcast.core.registers import RegisterWrite
    from custom_components.volcast.core.write_sequence import ERROR
    inv = Inverter(clock, {146: 0xFE, 166: 55})
    inv.drop_writes = True
    w = RegisterWriter(RegisterClient(_transport(inv, clock), deye_profile), deye_profile)
    assert await w.async_write(RegisterWrite("tou.1.soc", 166, 60)) == ERROR


@pytest.mark.asyncio
async def test_register_writer_error_when_read_back_never_refreshes(clock, deye_profile):
    from custom_components.volcast.core.modbus.client import RegisterClient
    from custom_components.volcast.core.modbus.writer import RegisterWriter
    from custom_components.volcast.core.registers import RegisterWrite
    from custom_components.volcast.core.write_sequence import ERROR
    inv = Inverter(clock, {146: 0xFE, 166: 55})
    inv.refresh_on_request = False
    w = RegisterWriter(RegisterClient(_transport(inv, clock, readback_timeout_s=20.0), deye_profile),
                       deye_profile)
    assert await w.async_write(RegisterWrite("tou.1.soc", 166, 60)) == ERROR
    assert inv.bank[166] == 60                      # zapis doszedł, ale bez świeżego odczytu nie ma OK


# ── wiek obrazu (zegar monotoniczny) ───────────────────────────────────────


@pytest.mark.asyncio
async def test_image_at_mono_is_the_oldest_sensor_stamp_on_the_monotonic_clock(inv, clock):
    t = _transport(inv, clock)
    clock.advance(10.0)
    inv.refresh(149)                                # tylko jeden z trzech odświeżony
    oldest = inv.hass.states.get(inv.entities[148]).last_reported
    expect = clock.now - (inv.utcnow() - oldest).total_seconds()
    assert t.image_at_mono([148, 149, 150]) == pytest.approx(expect)
    assert t.image_at_mono([149]) == pytest.approx(clock.now)
    assert await t.read(148, 3) == [100, 200, 300]
    assert t.last_read_at_mono == pytest.approx(expect)


@pytest.mark.asyncio
async def test_half_refreshed_image_after_multi_register_write_is_older_than_the_write(inv, clock):
    t = _transport(inv, clock, readback_timeout_s=1.0, readback_poll_s=1.0)
    inv.refresh_on_request = False
    with pytest.raises(WriteUnconfirmed):
        await t.write(148, [1, 2, 3], function=FC_WRITE_MULTIPLE)
    write_end = clock.now
    clock.advance(1.0)
    inv.refresh(148)
    inv.refresh(149)                                # 150 jeszcze sprzed zapisu
    assert t.image_at_mono([148, 149, 150]) < write_end
    inv.refresh(150)
    assert t.image_at_mono([148, 149, 150]) > write_end


def test_image_at_mono_unknown_without_sensor(inv, clock):
    t = _transport(inv, clock)
    assert t.image_at_mono([146, 147]) is None and t.image_at_mono([]) is None


# ── obcy zapis na naszym hubie (drugi sterownik) ───────────────────────────


async def _foreign(inv, data, service="write_register"):
    await inv.hass.bus.fire(SimpleNamespace(data={"domain": "modbus", "service": service,
                                                  "service_data": data}))


@pytest.mark.asyncio
@pytest.mark.parametrize("service", ["write_register", "write_coil"])
async def test_foreign_write_to_our_device_raises_conflict_until_hold_expires(inv, clock, service):
    t = _transport(inv, clock, conflict_hold_s=100.0)
    assert t.conflict is False
    await _foreign(inv, {"hub": HUB, "slave": UNIT, "address": 146, "value": 0xFF}, service)
    assert t.conflict is True and t.foreign_writes == 1 and t.stats.stray == 1
    assert t.last_foreign_write_addr == 146
    clock.advance(99.0)
    assert t.conflict is True
    clock.advance(1.0)
    assert t.conflict is False


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [
    {"hub": "other_hub", "slave": UNIT, "address": 146, "value": 1},
    {"address": 146, "value": 1, "slave": UNIT},               # hub domyślny `modbus_hub`
    {"hub": HUB, "slave": UNIT + 1, "address": 146, "value": 1},
    {"hub": HUB, "unit": UNIT + 1, "address": 146, "value": 1},
    {"hub": HUB, "slave": 200, "address": 146, "value": 1},
])
async def test_writes_to_other_hubs_or_devices_are_not_conflicts(inv, clock, data):
    t = _transport(inv, clock)
    await _foreign(inv, data)
    await inv.hass.bus.fire(SimpleNamespace(data={"domain": "light", "service": "turn_on",
                                                  "service_data": {"hub": HUB}}))
    assert t.conflict is False and t.foreign_writes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [
    {"hub": HUB, "address": 146, "value": 1},                  # adres urządzenia domyślnie 1
    {"hub": HUB, "unit": "x", "address": 146, "value": 1},     # nieustalony → fail-closed
    {"hub": HUB, "device_address": UNIT, "address": 146, "value": 1},
    {"hub": HUB, "slave": 0, "address": 146, "value": 1},      # rozgłoszenie dociera do falownika
])
async def test_default_or_unparsable_device_address_counts_as_ours(inv, clock, data):
    t = _transport(inv, clock)
    await _foreign(inv, data)
    assert t.conflict is True


@pytest.mark.asyncio
@pytest.mark.parametrize("late", [False, True])
async def test_own_writes_are_never_conflicts(inv, clock, late):
    t = _transport(inv, clock)
    inv.hass.late_events = late

    async def _slow(data):                          # zdarzenie po dłuższym zapisie — tożsamość danych
        clock.advance(30.0)
        await Inverter._write(inv, data)
    inv.hass.services.handlers[("modbus", "write_register")] = _slow
    with pytest.raises(WriteUnconfirmed):
        await t.write(146, [0xFF], function=FC_WRITE_MULTIPLE)
    assert t.conflict is False and t.foreign_writes == 0


@pytest.mark.asyncio
async def test_identical_foreign_payload_right_after_own_write_is_still_foreign(inv, clock):
    t = _transport(inv, clock)
    with pytest.raises(WriteUnconfirmed):
        await t.write(146, [0xFF], function=FC_WRITE_MULTIPLE)
    assert t.conflict is False
    await _foreign(inv, {"hub": HUB, "slave": UNIT, "address": 146, "value": [0xFF]})
    assert t.conflict is True                       # własny = wyłącznie ten sam obiekt danych


@pytest.mark.asyncio
async def test_close_stops_listening(inv, clock):
    t = _transport(inv, clock)
    await t.close()
    assert inv.hass.bus.listeners == []
    await _foreign(inv, {"hub": HUB, "slave": UNIT, "address": 146, "value": 1})
    assert t.conflict is False


# ── warunek mapy: tylko czujniki bazowe platformy `modbus` ─────────────────


def _updates(inv) -> list:
    return [c[2] for c in inv.hass.services.calls if c[:2] == ("homeassistant", "update_entity")]


@pytest.mark.asyncio
@pytest.mark.parametrize("platform,entity,reason", [
    ("modbus", SlaveSensor(), SLAVE_SENSOR),                       # slave_count / virtual_count
    ("template", ModbusRegisterSensor(), NOT_MODBUS_PLATFORM),    # encja innej integracji
    ("modbus", None, NOT_LOADED),                                 # brak w komponencie `sensor`
])
async def test_non_bus_entities_are_unsupported_and_never_refreshed(inv, clock, platform, entity, reason):
    t = _transport(inv, clock)
    eid = inv.entities[149]
    inv.add_entity(eid, platform=platform, entity=entity)
    if entity is None:
        del inv.hass.data["sensor"].entities[eid]
    with pytest.raises(WriteUnconfirmed):
        await t.write(148, [1, 2, 3], function=FC_WRITE_MULTIPLE)
    with pytest.raises(UnsupportedSensor) as err:
        await t.read(148, 3)
    assert isinstance(err.value, MissingRegister)                 # wołający nadal fail-closed
    assert err.value.details == ((149, eid, reason),) and err.value.addrs == (149,)
    assert eid in str(err.value) and reason in str(err.value)
    assert _updates(inv) == [] and t.image_at_mono([149]) is None


def test_base_sensor_needs_no_private_attributes(inv):
    """Tylko publiczne cechy: czujnik bazowy bez `_hub` i bez `coordinator` jest przyjmowany."""
    inv.add_entity("sensor.plain", entity=object())
    assert bus_sensor_problem(inv.hass, "sensor.plain") is None


@pytest.mark.asyncio
async def test_unregistered_or_non_sensor_entity_is_unsupported(clock):
    inv = Inverter(clock, {146: 1, 147: 2})
    del inv.hass.data["entity_registry"].entries[inv.entities[146]]   # czujnik YAML bez unique_id
    t = _transport(inv, clock, registers={146: inv.entities[146], 147: "number.volcast_reg_147"})
    inv.add_entity("number.volcast_reg_147")
    inv.hass.states.set("number.volcast_reg_147", "2", inv.utcnow())
    with pytest.raises(UnsupportedSensor) as err:
        await t.read(146, 2)
    assert err.value.reasons == (NOT_REGISTERED, NOT_SENSOR_DOMAIN)


@pytest.mark.asyncio
async def test_no_entity_registry_is_a_distinct_cause(inv, clock):
    inv.hass.data = {}
    t = _transport(inv, clock)
    with pytest.raises(UnsupportedSensor) as err:
        await t.read(146, 1)
    assert err.value.reasons == (REGISTRY_UNAVAILABLE,)


@pytest.mark.asyncio
async def test_broken_check_reports_check_failed_not_silent_missing(inv, clock):
    class _BrokenComponent:
        def get_entity(self, _eid):
            raise AttributeError("api changed")
    inv.hass.data["sensor"] = _BrokenComponent()
    t = _transport(inv, clock)
    with pytest.raises(UnsupportedSensor) as err:
        await t.read(146, 1)
    assert err.value.reasons == (f"{CHECK_FAILED}: AttributeError",)


@pytest.mark.asyncio
async def test_injected_sensor_check_replaces_the_default(inv, clock):
    inv.hass.data = {}
    cfg = HaModbusConfig(hub=HUB, device_address=UNIT, registers=inv.entities)
    seen = []

    def _check(eid):
        seen.append(eid)
        if eid == inv.entities[166]:
            raise RuntimeError("boom")              # wyłożone sprawdzenie nie przepuszcza encji
        return "custom" if eid == inv.entities[149] else None
    t = HaModbusTransport(inv.hass, cfg, clock=clock, sleep=clock.sleep, utcnow=inv.utcnow, sensor_check=_check)
    inv.poll_all()
    assert await t.read(146, 1) == [0xFE]
    with pytest.raises(UnsupportedSensor) as err:
        await t.read(149, 1)
    assert err.value.reasons == ("custom",)
    with pytest.raises(UnsupportedSensor) as err:
        await t.read(166, 1)
    assert err.value.reasons == (f"{CHECK_FAILED}: RuntimeError",)
    assert {inv.entities[146], inv.entities[149]} <= set(seen)


# ── wywołanie HA nigdy nie jest anulowane ─────────────────────────────────


class _Blocking:
    def __init__(self, inv) -> None:
        self.inv = inv
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.cancelled = False
        self.finished = False

    async def __call__(self, data) -> None:
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        await Inverter._write(self.inv, data)
        self.finished = True


@pytest.mark.asyncio
async def test_write_timeout_does_not_cancel_the_ha_call_and_marks_pending_on_completion(inv, clock):
    t = _transport(inv, clock, write_timeout_s=0.05, readback_timeout_s=5.0, readback_poll_s=1.0)
    ha = _Blocking(inv)
    inv.hass.services.handlers[("modbus", "write_register")] = ha
    inv.refresh_on_request = False
    with pytest.raises(WriteUnconfirmed):
        await t.write(146, [0xFF], function=FC_WRITE_MULTIPLE)
    assert ha.started.is_set() and not ha.cancelled and not ha.finished
    with pytest.raises(RequestTimeout):
        await t.read(146, 1)                       # w toku: stary stan nie jest odczytem zwrotnym
    inv.refresh(146)                                # odpytanie PRZED końcem wywołania HA
    ha.release.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert ha.finished and not ha.cancelled
    with pytest.raises(RequestTimeout):
        await t.read(146, 1)                       # stempel sprzed końca zapisu — nadal nieświeży
    inv.refresh(146)
    assert await t.read(146, 1) == [0xFF]


@pytest.mark.asyncio
async def test_cancelled_caller_does_not_cancel_the_ha_call(inv, clock):
    t = _transport(inv, clock, readback_timeout_s=5.0, readback_poll_s=1.0)
    ha = _Blocking(inv)
    inv.hass.services.handlers[("modbus", "write_register")] = ha
    inv.refresh_on_request = False
    task = asyncio.ensure_future(t.write(146, [0xFF], function=FC_WRITE_MULTIPLE))
    await ha.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not ha.cancelled
    with pytest.raises(RequestTimeout):
        await t.read(146, 1)                       # zapis w toku od anulowania
    ha.release.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert ha.finished and not ha.cancelled
    inv.refresh(146)
    assert await t.read(146, 1) == [0xFF]


@pytest.mark.asyncio
async def test_late_rejection_after_timeout_leaves_no_pending_write(inv, clock):
    t = _transport(inv, clock, write_timeout_s=0.05, readback_timeout_s=5.0, readback_poll_s=1.0)
    inv.refresh_on_request = False
    release = asyncio.Event()

    async def _late_reject(_data):
        await release.wait()
        raise KeyError("hub")                       # HA 2026.5: nieznany hub
    inv.hass.services.handlers[("modbus", "write_register")] = _late_reject
    with pytest.raises(WriteUnconfirmed):
        await t.write(146, [0xFF], function=FC_WRITE_MULTIPLE)
    clock.advance(1.0)
    inv.refresh(146)                                # odpytanie po limicie, przed odrzuceniem
    release.set()
    for _ in range(5):
        await asyncio.sleep(0)
    # odrzucenie przed wykonaniem nie odnawia zapisu w toku — odpytanie po limicie wystarcza
    assert await t.read(146, 1) == [0xFE] and clock.sleeps == []


@pytest.mark.asyncio
async def test_unknown_hub_key_error_is_link_down(inv, clock):
    t = _transport(inv, clock)

    async def _unknown_hub(_data):
        raise KeyError("hub")
    inv.hass.services.handlers[("modbus", "write_register")] = _unknown_hub
    with pytest.raises(LinkDown):
        await t.write(146, [0xFF], function=FC_WRITE_MULTIPLE)


@pytest.mark.asyncio
async def test_device_address_preferred_over_deprecated_slave_alias(clock):
    schema = vol.Schema({
        vol.Optional("hub", default="modbus_hub"): str,
        vol.Exclusive("device_address", "unit"): int,
        vol.Exclusive("slave", "unit"): int,
        vol.Required("address"): int,
        vol.Required("value"): vol.Any(int, [int]),
    })
    inv = Inverter(clock, {146: 0xFE}, schema=schema)
    t = _transport(inv, clock)
    with pytest.raises(WriteUnconfirmed):
        await t.write(146, [0xFF], function=FC_WRITE_MULTIPLE)
    assert "device_address" in inv.hass.services.writes()[0] and "slave" not in inv.hass.services.writes()[0]


@pytest.mark.asyncio
async def test_write_runs_as_a_home_assistant_background_task_when_available(inv, clock):
    made = []

    def _bg(coro, name):
        made.append(name)
        return asyncio.ensure_future(coro)
    inv.hass.async_create_background_task = _bg
    t = _transport(inv, clock)
    with pytest.raises(WriteUnconfirmed):
        await t.write(146, [0xFF], function=FC_WRITE_MULTIPLE)
    assert made == [BACKGROUND_TASK_NAME] and inv.bank[146] == 0xFF
