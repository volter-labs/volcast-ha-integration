"""Encje integracji `modbus` HA widziane przez transport `ha_modbus`: hub i adres urządzenia czujnika (czy czyta
z urządzenia, do którego piszemy), rejestry zapisu encji zapisu (kolizja tylko na naszych rejestrach), rejestry
zapisu profilu. Atrybuty prywatne encji `modbus` czyta WYŁĄCZNIE jeden pomocnik; kontrakt z prawdziwymi klasami HA
— `tests_ha/test_ha_modbus_transport_real_ha.py`."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.volcast.core.modbus.blocks import write_addresses
from custom_components.volcast.core.profile import load_builtin
from custom_components.volcast.core.transports.ha_modbus import (
    SENSOR_DEVICE_UNKNOWN, SENSOR_NOT_RAW, SENSOR_OTHER_DEVICE, SENSOR_OTHER_REGISTER, modbus_entity_device,
    modbus_scan_s,
    modbus_sensor_problem, modbus_writer_registers)
from custom_components.volcast.core.transports.ha_modbus_map import build_register_map

HUB, UNIT = "deye_hub", 1
DEYE = load_builtin("deye-sg")


def sensor(addr, *, hub=HUB, unit=UNIT, input_type="holding", count=1, scan=30, **over):
    s = SimpleNamespace(_hub=SimpleNamespace(name=hub), _device_address=unit, _address=addr, _input_type=input_type,
                        _count=count, _scan_interval=scan, _data_type="uint16", _swap=None, _scale=1, _offset=0,
                        _precision=0, _min_value=None, _max_value=None, _nan_value=None, _zero_suppress=None)
    for k, v in over.items():
        setattr(s, k, v)
    return s


def test_sensor_on_the_target_hub_device_and_register_is_bound():
    assert modbus_sensor_problem(sensor(146), 146, HUB, UNIT) is None
    assert modbus_entity_device(sensor(146)) == (HUB, UNIT)


@pytest.mark.parametrize("s, cause", [
    (sensor(146, hub="other"), SENSOR_OTHER_DEVICE),
    (sensor(146, unit=2), SENSOR_OTHER_DEVICE),
    (sensor(147), SENSOR_OTHER_REGISTER),                     # unique_id 146, czyta 147
    (sensor(146, input_type="input"), SENSOR_OTHER_REGISTER),  # rejestry input, nie holding
    (sensor(146, count=2), SENSOR_OTHER_REGISTER),             # dwa słowa (int32) na jednym czujniku
    (SimpleNamespace(_address=146), SENSOR_DEVICE_UNKNOWN),    # bez huba i adresu urządzenia
    (sensor(146, _hub=None), SENSOR_DEVICE_UNKNOWN),
    (sensor(146, unit=None), SENSOR_DEVICE_UNKNOWN),
    (None, SENSOR_DEVICE_UNKNOWN),
])
def test_sensor_not_proven_on_the_target_device_is_refused(s, cause):
    assert modbus_sensor_problem(s, 146, HUB, UNIT) == cause


@pytest.mark.parametrize("over", [
    {"_data_type": "uint32"}, {"_data_type": "float32"}, {"_data_type": "custom"}, {"_scale": 10},
    {"_scale": 0.1}, {"_offset": 5}, {"_swap": "byte"}, {"_swap": "word"}, {"_precision": 1},
    {"_min_value": 0}, {"_max_value": 100}, {"_nan_value": 65535}, {"_zero_suppress": 1}])
def test_sensor_that_does_not_carry_the_raw_word_is_refused(over):
    assert modbus_sensor_problem(sensor(146, **over), 146, HUB, UNIT) == SENSOR_NOT_RAW


@pytest.mark.parametrize("over", [{"_data_type": "int16"}, {"_swap": "none"}, {"_scale": 1.0, "_offset": 0.0},
                                  {"_precision": None}])
def test_raw_sensor_variants_are_accepted(over):
    assert modbus_sensor_problem(sensor(146, **over), 146, HUB, UNIT) is None


@pytest.mark.parametrize("missing", ["_data_type", "_scale", "_offset", "_swap", "_precision"])
def test_raw_settings_that_cannot_be_read_are_unknown(missing):
    s = sensor(146)
    delattr(s, missing)
    assert modbus_sensor_problem(s, 146, HUB, UNIT) == SENSOR_DEVICE_UNKNOWN


def test_older_ha_slave_attribute_is_the_device_address():
    s = sensor(146)
    del s._device_address
    s._slave = UNIT
    assert modbus_sensor_problem(s, 146, HUB, UNIT) is None


def test_scan_interval_of_a_sensor():
    assert modbus_scan_s(sensor(146, scan=45)) == 45.0
    assert modbus_scan_s(SimpleNamespace()) is None and modbus_scan_s(sensor(146, scan=0)) is None


@pytest.mark.parametrize("entity, domain, regs", [
    (SimpleNamespace(_write_type="write_register", _address=166), "switch", frozenset({166})),
    (SimpleNamespace(_write_type="write_registers", _address=166), "light", frozenset({166})),
    (SimpleNamespace(_write_type="write_coil", _address=166), "fan", frozenset()),       # cewka, nie rejestr
    (SimpleNamespace(_write_type="write_coil", _address=1, _brightness_address=166, _color_temp_address=None),
     "light", frozenset({166})),                                                          # jasność = rejestr
    (SimpleNamespace(_write_type="write_coils", _address=166), "switch", frozenset()),
    (SimpleNamespace(_write_type="write_register", _address=10, _write_address=166), "cover", frozenset({166})),
    (SimpleNamespace(_write_type="write_register", _address=166), "climate", None),      # wiele rejestrów
    (SimpleNamespace(_address=166), "switch", None),
    (SimpleNamespace(_write_type="write_register"), "switch", None),
    (SimpleNamespace(_write_type="something_new", _address=166), "switch", None),
])
def test_writer_registers(entity, domain, regs):
    assert modbus_writer_registers(entity, domain) == regs


def test_profile_write_addresses_cover_the_tou_block():
    assert write_addresses(DEYE) == frozenset({146, *range(148, 160), *range(166, 178)})


class Registry:
    def __init__(self, entries):
        self.entities = {e.entity_id: e for e in entries}

    def async_get(self, eid):
        return self.entities.get(eid)


def _hass(entities: dict[int, object]):
    reg = Registry([SimpleNamespace(entity_id=f"sensor.volcast_reg_{a}", platform="modbus",
                                    unique_id=f"volcast_reg_{a}") for a in entities])
    comp = SimpleNamespace(get_entity={f"sensor.volcast_reg_{a}": e for a, e in entities.items()}.get)
    return SimpleNamespace(data={"entity_registry": reg, "sensor": comp},
                           states=SimpleNamespace(async_entity_ids=lambda d: []))


def _required():
    from custom_components.volcast.core.transports.ha_modbus_map import required_addresses
    return sorted(required_addresses(DEYE))


def test_map_bound_to_the_target_device_is_ok_and_reports_the_slowest_scan():
    ents = {a: sensor(a, scan=60 if a == 588 else 30) for a in _required()}
    report = build_register_map(_hass(ents), DEYE, device=(HUB, UNIT), sensor_check=lambda eid: None)
    assert report.ok and report.max_scan_s == 60.0


def test_map_with_a_sensor_on_another_device_is_not_ok():
    ents = {a: sensor(a) for a in _required()}
    ents[0] = sensor(0, unit=2)                       # identyfikacja z innego falownika
    report = build_register_map(_hass(ents), DEYE, device=(HUB, UNIT), sensor_check=lambda eid: None)
    assert not report.ok
    assert [(u.addr, u.cause) for u in report.unsupported] == [(0, SENSOR_OTHER_DEVICE)]
    assert {"addr": 0, "cause": SENSOR_OTHER_DEVICE} in report.telemetry()["unsupported"]


def test_map_without_a_device_binding_keeps_its_old_meaning():
    ents = {a: sensor(a, unit=2) for a in _required()}
    assert build_register_map(_hass(ents), DEYE, sensor_check=lambda eid: None).ok
