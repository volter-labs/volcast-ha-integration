"""Atrapy HA dla testów sterowania: stany z atrybutami, usługi zmieniające stan jak
integracja falownika, konteksty zapisu."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from homeassistant.core import Context

NOW = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)   # = conftest.FAKE_UTCNOW


class FakeContext(Context):
    """Kontekst zapisu — ta sama atrapa co `homeassistant.core.Context` z conftest."""


class FakeState:
    def __init__(self, entity_id: str, state: str, attributes: dict | None = None,
                 reported: datetime | None = None, context: Context | None = None):
        self.entity_id = entity_id
        self.state = state
        self.attributes = dict(attributes or {})
        self.last_updated = reported or NOW
        self.last_reported = reported or NOW
        self.context = context or Context()


class FakeStates:
    def __init__(self):
        self._s: dict[str, FakeState] = {}

    def get(self, entity_id):
        return self._s.get(entity_id)

    def set(self, entity_id, state, attributes=None, **kw):
        prev = self._s.get(entity_id)
        attrs = dict(prev.attributes) if prev and attributes is None else (attributes or {})
        self._s[entity_id] = FakeState(entity_id, str(state), attrs, **kw)

    def async_all(self):
        return list(self._s.values())


class FakeServices:
    """Zapis przez usługę zmienia stan encji (jak integracja falownika)."""

    def __init__(self, states: FakeStates):
        self.states = states
        self.calls: list[tuple[str, str, dict, Any]] = []
        self.blocking: list[bool] = []                # flaga `blocking` każdego wywołania
        self.fail: dict[str, BaseException] = {}      # entity_id → wyjątek do rzucenia
        self.delay_s: float = 0.0                     # opóźnienie usługi (test limitu czasu)

    def has_service(self, domain, service):
        return True

    async def async_call(self, domain, service, data, blocking=False, context=None):
        self.calls.append((domain, service, dict(data), context))
        self.blocking.append(blocking)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        eid = data["entity_id"]
        if eid in self.fail:
            raise self.fail[eid]
        if service == "select_option":
            self.states.set(eid, data["option"], context=context)
        elif service == "set_value":
            self.states.set(eid, data.get("value", data.get("time")), context=context)
        elif service in ("turn_on", "turn_off"):
            self.states.set(eid, "on" if service == "turn_on" else "off", context=context)


class FakeHass:
    def __init__(self):
        self.states = FakeStates()
        self.services = FakeServices(self.states)
        self.bus = SimpleNamespace(async_fire=lambda *a, **k: None)
        self.data: dict = {}
        self.config = SimpleNamespace(time_zone="Europe/Warsaw", location_name="Home", country="PL",
                                      units=SimpleNamespace(temperature_unit="°C"))
        self.tasks: list = []
        self.is_running = True

    def async_create_task(self, coro, *_a, **_k):
        t = asyncio.get_running_loop().create_task(coro)
        self.tasks.append(t)
        return t

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


GOODWE_ENTITIES = {
    "mode": "select.goodwe_ems_mode",
    "power_w": "number.goodwe_ems_power_limit",
    "soc_min": "number.goodwe_depth_of_discharge_on_grid",
    "soc_max": "number.goodwe_soc_upper_limit",
    "export_limit_w": "number.goodwe_grid_export_limit",
    "export_limit_enabled": "switch.goodwe_grid_export_limit",
    "soc": "sensor.goodwe_battery_state_of_charge",
    "battery_temp_c": "sensor.goodwe_battery_temperature",
}
GOODWE_UNITS = {"power_w": "W", "soc_min": "%", "soc_max": "%", "export_limit_w": "W", "soc": "%",
                "battery_temp_c": "°F"}


def goodwe_hass(**over) -> FakeHass:
    h = FakeHass()
    s = h.states
    s.set(GOODWE_ENTITIES["mode"], over.get("mode", "auto"),
          {"options": ["auto", "charge_pv", "battery_standby", "sell_power", "charge_battery",
                       "discharge_battery"]})
    s.set(GOODWE_ENTITIES["power_w"], over.get("power", "0"),
          {"min": 0, "max": 10000, "step": 1, "unit_of_measurement": "W"})
    s.set(GOODWE_ENTITIES["soc_min"], over.get("dod", "85"),
          {"min": 0, "max": 99, "step": 1, "unit_of_measurement": "%"})
    s.set(GOODWE_ENTITIES["soc_max"], over.get("soc_max", "100"),
          {"min": 10, "max": 100, "step": 1, "unit_of_measurement": "%"})
    s.set(GOODWE_ENTITIES["export_limit_w"], over.get("export", "4000"),
          {"min": 0, "max": 10000, "step": 1, "unit_of_measurement": "W"})
    s.set(GOODWE_ENTITIES["export_limit_enabled"], over.get("export_on", "on"), {})
    s.set(GOODWE_ENTITIES["soc"], over.get("soc", "60"), {"unit_of_measurement": "%"})
    s.set(GOODWE_ENTITIES["battery_temp_c"], over.get("temp", "82.4"), {"unit_of_measurement": "°F"})
    return h
