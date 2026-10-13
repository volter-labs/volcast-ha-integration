"""Odtworzenie zapisu z recordera (2026-10-11): ładowarka sama przestawia impulsową encję start/stop.

Wyciąg (`fixtures/charger_status_2026_10_11.json`) ma tylko `entity_id`, `ts`, `state` trzech encji
ładowarki — bez kontekstów. Aktora (użytkownik w HA) test dokłada sam dla dwóch zmian, które zrobił
użytkownik: Stop o 12:33:12 i Start o 13:08:12. Wszystkie pozostałe zmiany są bez aktora (urządzenie).
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from homeassistant.core import Context

from custom_components.volcast.control.ha_writer import EntityServiceWriter
from custom_components.volcast.control.loads_executor import LoadsExecutor, charger_roles
from custom_components.volcast.control.loads_store import LoadsStore
from custom_components.volcast.core.ev.model import LoadSlot, LoadsMeta

from .ha_fakes import FakeHass, FakeState

FIXTURE = Path(__file__).parent / "fixtures" / "charger_status_2026_10_11.json"
STATUS = "sensor.ev_charger_status"
OPERATION = "select.ev_charger_charging_operation"
CURRENT = "number.ev_charger_charge_current"
ATTRS = {
    STATUS: {"options": ["available", "plugged_in", "charging", "waiting_for_authorization", "charged",
                         "waiting", "fault"]},
    OPERATION: {"options": ["Start charging", "Stop charging", "No operation"]},
    CURRENT: {"min": 6, "max": 16, "step": 1, "unit_of_measurement": "A"},
}
# zmiany zrobione przez użytkownika w HA (prefiks czasu UTC, encja)
USER_CHANGES = {("12:33:12", OPERATION), ("13:08:12", OPERATION)}


def _ts(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _at(hhmmss: str) -> datetime:
    return _ts(f"2026-10-11T{hhmmss}+00:00")


class Clock:
    def __init__(self, start: datetime):
        self.t0 = start
        self.utc = start

    @property
    def mono(self) -> float:
        return 1000.0 + (self.utc - self.t0).total_seconds()


def _slot(mode, start, end):
    return LoadSlot(device_id="dev1", start=_at(start), end=_at(end), mode=mode,
                    power_w=11040.0 if mode == "forced" else None, min_power_w=None,
                    session_id="s1", battery_guard="bypass")


def _plan(gen, slot, remaining):
    meta = LoadsMeta(session_id="s1", load_key="dev1", control_enabled=True, phases=3, control_unit="A",
                     min=6.0, max=16.0, step=1.0, max_power_w=11040.0, remaining_kwh=remaining,
                     plan_generated_at=_at(gen))
    return SimpleNamespace(loads=(slot,), loads_meta=meta)


def _setup(rows):
    h = FakeHass()
    h.data["entity_registry"] = SimpleNamespace(entities={
        e: SimpleNamespace(entity_id=e, device_id="dev1") for e in (STATUS, OPERATION, CURRENT)})
    entry = SimpleNamespace(entry_id="e1", options={"ev_chargers": [{
        "device_id": "dev1", "label": "Wallbox",
        "roles": {"status": STATUS, "setpoint": CURRENT, "start_stop": OPERATION}}]})
    clock = Clock(_ts(rows[0]["ts"]))
    ex = LoadsExecutor(h, entry, writer=EntityServiceWriter(h),
                       roles_provider=lambda key: charger_roles(entry.options, key),
                       store=LoadsStore(h, "e1"), consent_getter=lambda: True,
                       local_switch_getter=lambda: True, plan_only_getter=lambda: False,
                       clock=lambda: clock.mono, utcnow=lambda: clock.utc, readback_s=0)
    return h, ex, clock


def _replay(h, ex, clock, rows, until: str) -> None:
    """Odtwarza wiersze do `until` (włącznie): stan encji i zdarzenie zmiany z kontekstem."""
    while rows and rows[0]["ts"][11:19] <= until:
        r = rows.pop(0)
        eid, ts = r["entity_id"], _ts(r["ts"])
        clock.utc = ts
        old = h.states.get(eid)
        ctx = Context(user_id="u1") if (r["ts"][11:19], eid) in USER_CHANGES else Context()
        new = FakeState(eid, r["state"], ATTRS[eid], context=ctx)
        h.states._s[eid] = new
        ex.on_state_event(SimpleNamespace(
            data={"entity_id": eid, "old_state": old, "new_state": new}, context=ctx))


def _calls(h):
    return [(d, s, dict(data)) for d, s, data, _ctx in h.services.calls]


def test_recorder_day_impulse_select_from_the_device_is_not_a_manual_change():
    async def body():
        rows = json.loads(FIXTURE.read_text())
        assert {tuple(sorted(r)) for r in rows} == {("entity_id", "state", "ts")}   # wyciąg zanonimizowany
        h, ex, clock = _setup(rows)
        for r in rows[:3]:                       # stan po starcie HA (ładuje, 16 A)
            h.states.set(r["entity_id"], r["state"], ATTRS[r["entity_id"]])
        del rows[:3]
        await ex.async_start()
        clock.utc = _at("10:02:00")
        await ex.async_on_plan(_plan("10:01:50", _slot("forced", "10:00:00", "12:45:00"), 8.0))
        assert _calls(h) == [("number", "set_value", {"entity_id": CURRENT, "value": 16.0})]
        h.services.calls.clear()

        # 12:33 — Stop z HA (użytkownik) w slocie forced: ręczny stop; „charged” 0,35 s później nic nie zmienia
        _replay(h, ex, clock, rows, "12:33:13")
        stop_at = "2026-10-11T12:33:12.866105+00:00"
        assert ex.state.manual_event == "stop"
        assert _ts(ex.state.manual_at) == _ts(stop_at) and _ts(ex.state.held_since) == _ts(stop_at)
        await ex.async_ack_report(ex.report_for("dev1"))
        clock.utc = _at("12:34:00")              # chmura uwzględnia stop: slot off
        await ex.async_on_plan(_plan("12:34:00", _slot("off", "12:30:00", "13:30:00"), 8.0))
        assert ex.state.held_since is None

        # 13:08 — Start z HA (użytkownik) w slocie off: ręczny start; 15 s później URZĄDZENIE ustawia
        # impulsową encję na „Stop charging” w chwili, gdy ładowanie rusza — to NIE jest ręczny stop
        _replay(h, ex, clock, rows, "13:08:27")
        start_at = "2026-10-11T13:08:12.721497+00:00"
        assert h.states.get(OPERATION).state == "No operation" and h.states.get(STATUS).state == "charging"
        assert ex.state.manual_event == "start"
        assert _ts(ex.state.manual_at) == _ts(start_at) and _ts(ex.state.held_since) == _ts(start_at)
        await ex.async_ack_report(ex.report_for("dev1"))
        clock.utc = _at("13:09:00")              # chmura uwzględnia start: slot forced, cel prawie osiągnięty
        await ex.async_on_plan(_plan("13:09:00", _slot("forced", "13:00:00", "14:30:00"), 0.3))
        assert ex.state.held_since is None

        # 14:02 — koniec ładowania bez aktora w slocie forced: wstrzymanie bez zdarzenia (do celu < 0,5 kWh)
        _replay(h, ex, clock, rows, "14:02:38")
        end_at = "2026-10-11T14:02:37.997219+00:00"
        assert ex.state.manual_event is None and _ts(ex.state.held_since) == _ts(end_at)
        clock.utc = _at("14:05:00")
        await ex.async_tick()

        # 14:11 — odłączenie: urządzenie ustawia „Start charging”, status charged → available: nie ręczny start
        _replay(h, ex, clock, rows, "23:59:59")
        assert ex.state.manual_event is None and ex.state.manual_at is None
        assert _ts(ex.state.held_since) == _ts(end_at)
        assert ex.report_for("dev1")["held_since"] == _ts(end_at).isoformat()
        assert _calls(h) == []                   # przez cały dzień ani jednego Startu/Stopu
        await ex.async_stop()
    asyncio.run(body())
