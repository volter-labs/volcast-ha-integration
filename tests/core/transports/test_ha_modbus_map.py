"""Mapa czujników → rejestry dla transportu `ha_modbus`: konwencja `unique_id`, jawna mapa z kreatora,
walidacja względem profilu, raport dla UI (bez entity_id w części do telemetrii)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.volcast.core.profile import load_profile
from custom_components.volcast.core.transports.ha_modbus import NOT_MODBUS_PLATFORM, NOT_REGISTERED
from custom_components.volcast.core.transports.ha_modbus_map import (
    UNIQUE_ID_PREFIX, build_register_map, registers_from_explicit,
    registers_from_registry, required_addresses, unregistered_modbus_sensors)

PROFILE = Path(__file__).parents[3] / "custom_components/volcast/profiles/deye-sg.json"


@pytest.fixture(scope="module")
def profile():
    return load_profile(PROFILE)


@pytest.fixture
def required(profile):
    return required_addresses(profile)


class Reg:
    def __init__(self) -> None:
        self.entities: dict[str, SimpleNamespace] = {}

    def add(self, eid, unique_id, platform="modbus", domain="sensor"):
        self.entities[eid] = SimpleNamespace(entity_id=eid, unique_id=unique_id, platform=platform, domain=domain)

    def async_get(self, eid):
        return self.entities.get(eid)


class Component:
    def __init__(self) -> None:
        self.ents: dict[str, object] = {}

    def get_entity(self, eid):
        return self.ents.get(eid)


class States:
    def __init__(self, ids=()) -> None:
        self.ids = list(ids)

    def async_entity_ids(self, domain=None):
        return [i for i in self.ids if domain is None or i.startswith(domain + ".")]


def make_hass(reg=None, comp=None, ids=()):
    return SimpleNamespace(data={"entity_registry": reg if reg is not None else Reg(),
                                 "sensor": comp or Component()}, states=States(ids))


def full_registry(required):
    reg = Reg()
    for a in sorted(required):
        reg.add(f"sensor.r{a}", f"{UNIQUE_ID_PREFIX}{a}")
    return reg


def test_required_addresses_cover_read_write_identify(required):
    # słowa bloków, które czytnik woła: poll (scalony przez dziury) + identyfikacja
    for a in (0, 3, 4, 5, 6, 7, 20, 21, 534, 535, 587, 588, 589, 590, 591, 625, 653, 672, 673, *range(146, 178)):
        assert a in required, a
    assert 1 not in required and 8 not in required and 22 not in required


def test_registry_convention_builds_full_map(profile, required):
    hass = make_hass(full_registry(required))
    reg, dups = registers_from_registry(hass)
    assert reg[146] == "sensor.r146" and set(reg) == set(required) and not dups
    rep = build_register_map(hass, profile, sensor_check=lambda e: None)
    assert rep.ok and rep.missing == () and rep.unsupported == ()
    assert rep.registers == reg


def test_missing_addresses_listed_sorted(profile, required):
    r = full_registry(required)
    for a in (3, 150, 151):
        del r.entities[f"sensor.r{a}"]
    rep = build_register_map(make_hass(r), profile, sensor_check=lambda e: None)
    assert rep.missing == (3, 150, 151) and not rep.ok


def test_ignores_non_matching_unique_ids_and_other_platforms(profile):
    r = Reg()
    r.add("sensor.a", "volcast_reg_146")
    r.add("sensor.b", "volcast_reg_abc")
    r.add("sensor.c", "other_146")
    r.add("sensor.d", "volcast_reg_147", platform="template")
    r.add("switch.e", "volcast_reg_148", domain="switch")
    reg, _ = registers_from_registry(make_hass(r))
    assert reg == {146: "sensor.a"}


def test_duplicate_unique_address_is_ambiguous_and_excluded(profile):
    r = Reg()
    r.add("sensor.a", "volcast_reg_146")
    r.add("sensor.b", "volcast_reg_146")
    reg, dups = registers_from_registry(make_hass(r))
    assert 146 not in reg and dups == (146,)
    rep = build_register_map(make_hass(r), profile, sensor_check=lambda e: None)
    assert rep.duplicates == (146,) and 146 in rep.missing


def test_registry_unavailable_gives_no_map(profile):
    hass = SimpleNamespace(data={}, states=States())
    reg, dups = registers_from_registry(hass)
    assert reg == {} and dups == ()
    rep = build_register_map(hass, profile, sensor_check=lambda e: None)
    assert rep.registry_available is False and not rep.ok


def test_explicit_map_from_wizard(profile, required):
    explicit = {str(a): f"sensor.w{a}" for a in required}     # klucze z JSON-a to napisy
    explicit["bogus"] = "sensor.x"
    explicit["5000"] = 123
    rep = build_register_map(make_hass(), profile, explicit=explicit, sensor_check=lambda e: None)
    assert rep.ok and rep.registers[146] == "sensor.w146" and 5000 not in rep.registers
    assert rep.invalid_entries == 2
    assert registers_from_explicit({"7": "sensor.a", 8: "sensor.b"}) == ({7: "sensor.a", 8: "sensor.b"}, 0)


def test_explicit_map_skips_registry_lookup(profile, required):
    hass = SimpleNamespace(data={}, states=States())             # bez rejestru encji
    rep = build_register_map(hass, profile, explicit={a: f"sensor.w{a}" for a in required},
                             sensor_check=lambda e: None)
    assert rep.ok


def test_unsupported_entities_reported_with_cause(profile, required):
    hass = make_hass(full_registry(required))
    causes = {"sensor.r146": NOT_MODBUS_PLATFORM, "sensor.r0": NOT_REGISTERED}
    rep = build_register_map(hass, profile, sensor_check=lambda e: causes.get(e))
    assert [(u.addr, u.entity_id, u.cause) for u in rep.unsupported] == [
        (0, "sensor.r0", NOT_REGISTERED), (146, "sensor.r146", NOT_MODBUS_PLATFORM)]
    assert not rep.ok and rep.missing == ()


def test_sensor_check_exception_is_a_cause_not_a_crash(profile, required):
    def boom(e):
        raise RuntimeError("x")
    rep = build_register_map(make_hass(full_registry(required)), profile, sensor_check=boom)
    assert rep.unsupported and all(u.cause.startswith("check_failed") for u in rep.unsupported)


def test_report_ranges_exclude_unsupported(profile, required):
    rep = build_register_map(make_hass(full_registry(required)), profile,
                             sensor_check=lambda e: NOT_MODBUS_PLATFORM if e == "sensor.r150" else None)
    blk = next(b for b in rep.blocks if b.start == 146)
    assert blk.missing == (150,) and (146, 32) not in rep.mapped_ranges and (587, 5) in rep.mapped_ranges


def test_unregistered_modbus_sensors_found_by_platform(profile):
    comp = Component()
    comp.ents["sensor.yaml1"] = SimpleNamespace(platform=SimpleNamespace(domain="sensor", platform_name="modbus"))
    comp.ents["sensor.yaml2"] = SimpleNamespace(platform=SimpleNamespace(platform_name="modbus"))
    comp.ents["sensor.other"] = SimpleNamespace(platform=SimpleNamespace(platform_name="template"))
    reg = Reg()
    reg.add("sensor.reg", "volcast_reg_0")
    comp.ents["sensor.reg"] = SimpleNamespace(platform=SimpleNamespace(platform_name="modbus"))
    hass = make_hass(reg, comp, ids=["sensor.yaml1", "sensor.yaml2", "sensor.other", "sensor.reg", "sensor.gone"])
    assert unregistered_modbus_sensors(hass) == ("sensor.yaml1", "sensor.yaml2")
    rep = build_register_map(hass, profile, sensor_check=lambda e: None)
    assert rep.unregistered_sensors == ("sensor.yaml1", "sensor.yaml2")


def test_unregistered_detection_is_best_effort(profile):
    hass = SimpleNamespace(data={"entity_registry": Reg(), "sensor": object()}, states=object())
    assert unregistered_modbus_sensors(hass) == ()


def test_telemetry_has_causes_and_addresses_only(profile, required):
    r = full_registry(required)
    del r.entities["sensor.r3"]
    rep = build_register_map(make_hass(r, ids=["sensor.yaml1"]), profile,
                             sensor_check=lambda e: NOT_MODBUS_PLATFORM if e == "sensor.r146" else None)
    t = rep.telemetry()
    assert t["missing"] == [3] and t["unsupported"] == [{"addr": 146, "cause": NOT_MODBUS_PLATFORM}]
    assert "unregistered_count" in t and t["ok"] is False
    assert "sensor." not in repr(t)


def test_required_equals_words_of_poll_blocks(profile, required):
    from custom_components.volcast.core.modbus.blocks import read_plan
    for b in read_plan(profile):
        assert set(range(b[0], b[0] + b[1])) <= required
    assert {147, 160, 165, 589} <= required


def test_ok_only_when_every_poll_block_is_readable(profile, required):
    r = full_registry(required)
    rep = build_register_map(make_hass(r), profile, sensor_check=lambda e: None)
    assert rep.ok and all(b.readable for b in rep.blocks)
    del r.entities["sensor.r589"]                      # w bloku (587,5), poza polami profilu
    rep = build_register_map(make_hass(r), profile, sensor_check=lambda e: None)
    assert not rep.ok and rep.missing == (589,)
    assert (587, 5) not in rep.mapped_ranges and (146, 32) in rep.mapped_ranges


@pytest.mark.asyncio
async def test_ok_report_gives_a_transport_the_client_can_poll(profile, required):
    from datetime import datetime, timezone
    from custom_components.volcast.core.transports.ha_modbus import HaModbusConfig, HaModbusTransport
    rep = build_register_map(make_hass(full_registry(required)), profile, sensor_check=lambda e: None)
    assert rep.ok
    now = datetime.now(timezone.utc)
    states = {e: SimpleNamespace(state="1", last_updated=now, last_reported=now) for e in rep.registers.values()}
    hass = SimpleNamespace(data={}, states=SimpleNamespace(get=states.get), services=None, bus=None)
    t = HaModbusTransport(hass, HaModbusConfig("h", 1, rep.registers), sensor_check=lambda e: None)
    for start, n in rep.mapped_ranges:
        assert len(await t.read(start, n)) == n


def test_explicit_one_entity_on_several_addresses_is_invalid(profile, required):
    explicit = {a: f"sensor.w{a}" for a in required}
    explicit[148] = explicit[149] = "sensor.same"
    rep = build_register_map(make_hass(), profile, explicit=explicit, sensor_check=lambda e: None)
    assert not rep.ok and rep.duplicates == (148, 149) and {148, 149} <= set(rep.missing)
    assert 148 not in rep.registers and rep.telemetry()["duplicates"] == [148, 149]


def test_input_registers_and_unit_override_are_unsupported(profile, required):
    import custom_components.volcast.core.transports.ha_modbus_map as m
    plan = [(0, 2, 4, None), (10, 2, 3, 5)]
    orig = m._plan
    m._plan = lambda p: plan
    try:
        reg = {a: f"sensor.r{a}" for a in (0, 1, 10, 11)}
        rep = m.build_register_map(make_hass(), profile, explicit=reg, sensor_check=lambda e: None)
    finally:
        m._plan = orig
    assert not rep.ok and [b.cause for b in rep.blocks] == [m.UNSUPPORTED_INPUT_REGISTERS, m.UNSUPPORTED_UNIT_OVERRIDE]


def test_strict_explicit_keys_and_leading_zero_unique_id():
    from custom_components.volcast.core.transports.ha_modbus_map import _addr_from_unique_id
    reg, bad = registers_from_explicit({146.7: "sensor.a", " 5": "sensor.b", True: "sensor.c", "7": "sensor.d"})
    assert reg == {7: "sensor.d"} and bad == 3
    assert _addr_from_unique_id("volcast_reg_0146") is None and _addr_from_unique_id("volcast_reg_0") == 0
