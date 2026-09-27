import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from custom_components.volcast.control_entities import (VolcastControlStatusSensor, VolcastControlSwitch,
                                                        VolcastPlanSensor, plan_slots_for_card)

NOW = datetime(2026, 9, 23, 10, 30, tzinfo=timezone.utc)


def raw_plan(n=48):
    t0 = datetime(2026, 9, 23, 0, tzinfo=timezone.utc)
    slots = [{"from": (t0 + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "to": (t0 + timedelta(hours=i + 1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "mode": "self_consume", "price_pln_kwh": 0.61, "plan_mode": "SELF_CONSUME",
              "grid_import_kwh": 0.12, "grid_export_kwh": 0.0, "display_kind": "SELF_CONSUME",
              "soc_target": 20} for i in range(n)]
    return {"schedule_id": "p", "slots": list(reversed(slots)),
            "fallback": {"mode": "self_consume", "soc_reserve": 10}, "control_enabled": False}


def test_plan_slots_for_card_fields_order_and_size():
    out = plan_slots_for_card(raw_plan(), NOW)
    assert len(out) == 48 and out[0]["from"] < out[1]["from"]
    assert out[10] | {} == out[10] and out[10]["now"] is True and out[10]["plan_mode"] == "SELF_CONSUME"
    assert set(out[0]) == {"from", "to", "action", "charge_source", "discharge_purpose", "power_w", "soc_target",
                           "price", "export_allowed", "export_limit_w", "plan_mode", "import_kwh",
                           "export_kwh", "display_kind", "now"}
    assert out[0]["from"] == "2026-09-23T00:00:00Z"
    # HA zapisuje atrybuty zwartym JSON-em
    assert len(json.dumps(out, separators=(",", ":"))) < 16_000


def test_invalid_plan_gives_empty():
    assert plan_slots_for_card({"slots": [{"mode": "x"}]}, NOW) == []
    assert plan_slots_for_card(None, NOW) == []


def _rt(**ex):
    executor = SimpleNamespace(raw_plan=raw_plan(), schedule=None, consent=False, local_switch=False,
                               paused=False, last_decision=None, tou_preview=None, **ex)
    return SimpleNamespace(executor=executor, mapped={"soc": "sensor.soc", "mode": "select.m"},
                           choice=None, rated_power_w=None)


def test_plan_sensor_unique_id_and_attributes():
    entry = SimpleNamespace(entry_id="e1", options={"telemetry_map": {"load_power_w": "sensor.load"},
                                                    "battery_capacity_kwh": 10.2})
    s = VolcastPlanSensor(entry, _rt())
    assert s._attr_unique_id == "e1_control_plan"
    a = s.extra_state_attributes
    assert a["entities"] == {"soc": "sensor.soc", "pv": None, "grid": None, "battery": None, "load": "sensor.load"}
    assert a["account_consent"] is False and a["battery_capacity_kwh"] == 10.2 and len(a["slots"]) == 48
    assert {"slots", "entities", "tou_preview"} <= s._unrecorded_attributes
    assert s.native_value == "no_plan"


def test_switch_toggles_executor():
    calls = []

    async def set_local(v):
        calls.append(("set", v))

    async def tick():
        calls.append(("tick",))
    rt = _rt(async_set_local_switch=set_local, async_tick=tick)
    sw = VolcastControlSwitch(SimpleNamespace(entry_id="e1", options={}), rt)
    asyncio.run(sw.async_turn_on())
    asyncio.run(sw.async_turn_off())
    assert calls == [("set", True), ("tick",), ("set", False), ("tick",)]
    assert sw._attr_unique_id == "e1_control_switch"


def test_switch_never_touches_account_consent(monkeypatch):
    # Zgoda przychodzi wyłącznie z chmury/aplikacji — przełącznik to tylko lokalne wł./wył.
    from tests.control.test_executor import make, ready
    h, ex = make(monkeypatch=monkeypatch)

    async def go():
        await ready(ex, consent=False, local=False)
        sw = VolcastControlSwitch(SimpleNamespace(entry_id="e1", options={}), SimpleNamespace(executor=ex))
        await sw.async_turn_on()
        return sw
    sw = asyncio.run(go())
    assert ex.consent is False and ex.local_switch is True and sw.is_on is True
    assert h.services.calls == []                               # bez zgody nic nie idzie


def test_status_sensor_reports_decision_and_foreign_changes():
    from custom_components.volcast.core.control.cycle import CycleDecision
    rt = _rt(foreign_changes=[{"key": "mode", "entity_id": "select.m", "at": "x"}])
    s = VolcastControlStatusSensor(SimpleNamespace(entry_id="e1", options={}), rt)
    assert s.native_value == "starting" and s._attr_unique_id == "e1_control_status"
    rt.executor.last_decision = CycleDecision("idle", "no_plan")
    assert s.native_value == "idle" and s.extra_state_attributes["reason"] == "no_plan"
    assert s.extra_state_attributes["foreign_changes"][0]["key"] == "mode"
