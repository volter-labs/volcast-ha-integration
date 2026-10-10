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
    # Notatki decyzji zmieniają się co cykl (nastawa sprzedaży) — bez wiersza rekordera na tik.
    assert s._unrecorded_attributes == frozenset({"notes"})


# ── lustro zgody (ADR-051): przełącznik ↔ zgoda konta przez `set-controller` ──

import pytest  # noqa: E402

from custom_components.volcast.cloud.client import SetControllerResult  # noqa: E402

_STATUS = {"ok": 200, "network": 0, "server_error": 503, "rate_limited": 429, "unauthorized": 401,
           "invalid_body": 400, "no_readings": 409, "revoked_by_other": 409, "other_controller_active": 409,
           "tier_required": 403, "mirror_disabled": 403, "forbidden": 403}


class _Cloud:
    """Atrapa `VolcastCloud.async_set_controller`: kolejka wyników (ostatni się powtarza)."""

    def __init__(self, *outcomes, on_call=None):
        self.outcomes = list(outcomes) or ["ok"]
        self.calls: list[tuple[str, str]] = []
        self.on_call = on_call

    async def async_set_controller(self, kind, origin="ha_switch"):
        self.calls.append((kind, origin))
        if self.on_call is not None:
            await self.on_call(kind)
        o = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        return SetControllerResult(_STATUS.get(o, 418), o)


def _mirror_switch(monkeypatch, cloud, *, consent=True, local=True, mirror=True):
    from tests.control.test_executor import make, ready
    h, ex = make(monkeypatch=monkeypatch)
    sw = VolcastControlSwitch(SimpleNamespace(entry_id="e1", options={}), SimpleNamespace(executor=ex, cloud=cloud))
    sw.hass = h
    sleeps: list[float] = []

    async def no_sleep(s):
        sleeps.append(s)
    sw._sleep = no_sleep

    async def setup():
        await ready(ex, consent=consent, local=local)
        ex.set_consent_mirror(mirror)
    return h, ex, sw, sleeps, setup


async def _drain(sw):
    if sw._off_task is not None:
        await sw._off_task


def test_switch_off_calls_cloud_none_and_restores_baseline(monkeypatch):
    from tests.control.ha_fakes import GOODWE_ENTITIES as E
    seen = {}
    cloud = _Cloud("ok")
    h, ex, sw, _, setup = _mirror_switch(monkeypatch, cloud)

    async def go():
        await setup()
        await ex.async_tick()
        assert h.states.get(E["mode"]).state == "sell_power"
        await sw.async_turn_off()
        # lokalnie OD RAZU (przed chmurą): przełącznik off i powrót do trybu bazowego
        seen["local"], seen["mode"], seen["calls"] = ex.local_switch, h.states.get(E["mode"]).state, list(cloud.calls)
        await _drain(sw)
    asyncio.run(go())
    assert seen == {"local": False, "mode": "auto", "calls": []}
    assert cloud.calls == [("none", "ha_switch")] and ex.mirror_off_pending is False and sw.is_on is False


def test_switch_off_retries_with_backoff_until_ok(monkeypatch):
    cloud = _Cloud("network", "server_error", "unauthorized", "ok")
    _, ex, sw, sleeps, setup = _mirror_switch(monkeypatch, cloud)

    async def go():
        await setup()
        await sw.async_turn_off()
        await _drain(sw)
    asyncio.run(go())
    assert cloud.calls == [("none", "ha_switch")] * 4 and sleeps == [5, 15, 45]
    assert ex.mirror_off_pending is False


def test_switch_off_retry_continues_past_backoff_table(monkeypatch):
    cloud = _Cloud(*(["network"] * 6 + ["ok"]))
    _, _, sw, sleeps, setup = _mirror_switch(monkeypatch, cloud)

    async def go():
        await setup()
        await sw.async_turn_off()
        await _drain(sw)
    asyncio.run(go())
    assert sleeps == [5, 15, 45, 300, 300, 300] and len(cloud.calls) == 7


def test_switch_off_retry_stops_on_permanent_refusal(monkeypatch):
    cloud = _Cloud("mirror_disabled")
    _, ex, sw, sleeps, setup = _mirror_switch(monkeypatch, cloud)

    async def go():
        await setup()
        await sw.async_turn_off()
        await _drain(sw)
    asyncio.run(go())
    assert cloud.calls == [("none", "ha_switch")] and sleeps == [] and ex.local_switch is False


def test_switch_off_retry_stops_when_cloud_reports_consent_off(monkeypatch):
    holder = {}

    async def on_call(_kind):
        await holder["ex"].async_set_consent(False)       # w międzyczasie plan: zgoda już off
    cloud = _Cloud("network", on_call=on_call)
    _, ex, sw, _, setup = _mirror_switch(monkeypatch, cloud)
    holder["ex"] = ex

    async def go():
        await setup()
        await sw.async_turn_off()
        await _drain(sw)
    asyncio.run(go())
    assert len(cloud.calls) == 1 and ex.mirror_off_pending is False


@pytest.mark.parametrize("outcome,key", [
    ("no_readings", "consent_no_readings"),
    ("revoked_by_other", "consent_revoked_by_other"),
    ("other_controller_active", "consent_other_controller_active"),
    ("tier_required", "consent_tier_required"),
    ("mirror_disabled", "consent_mirror_error"),
    ("forbidden", "consent_mirror_error"),
    ("rate_limited", "consent_mirror_error"),
    ("invalid_body", "consent_mirror_error"),
    ("server_error", "consent_mirror_error"),
    ("network", "consent_mirror_error"),
])
def test_switch_on_409_stays_off_and_raises_issue(monkeypatch, outcome, key):
    from homeassistant.helpers import issue_registry as ir
    ir.async_create_issue.reset_mock()
    cloud = _Cloud(outcome)
    h, ex, sw, _, setup = _mirror_switch(monkeypatch, cloud, consent=False, local=False)

    async def go():
        await setup()
        await sw.async_turn_on()
    asyncio.run(go())
    assert cloud.calls == [("ha", "ha_switch")]
    assert ex.local_switch is False and sw.is_on is False and h.services.calls == []
    created = ir.async_create_issue.call_args
    assert created.args[2] == f"{key}_e1" and created.kwargs["translation_key"] == key


def test_switch_on_revoked_by_other_stays_off(monkeypatch):
    from homeassistant.helpers import issue_registry as ir
    ir.async_create_issue.reset_mock()
    _, ex, sw, _, setup = _mirror_switch(monkeypatch, _Cloud("revoked_by_other"), consent=False, local=False)

    async def go():
        await setup()
        await sw.async_turn_on()
    asyncio.run(go())
    assert ex.local_switch is False
    assert ir.async_create_issue.call_args.kwargs["translation_key"] == "consent_revoked_by_other"


def test_switch_on_ok_turns_on_and_clears_issues(monkeypatch):
    from homeassistant.helpers import issue_registry as ir
    from tests.control.ha_fakes import GOODWE_ENTITIES as E
    cloud = _Cloud("ok")
    h, ex, sw, _, setup = _mirror_switch(monkeypatch, cloud, consent=True, local=False)

    async def go():
        await setup()
        ir.async_delete_issue.reset_mock()
        await sw.async_turn_on()
    asyncio.run(go())
    # najpierw chmura, potem lokalnie
    assert cloud.calls == [("ha", "ha_switch")] and ex.local_switch is True
    assert h.states.get(E["mode"]).state == "sell_power"
    assert "consent_no_readings_e1" in [c.args[2] for c in ir.async_delete_issue.call_args_list]


def test_switch_on_server_first_switch_stays_off_while_waiting(monkeypatch):
    holder = {}

    async def on_call(_kind):
        holder["during"] = holder["ex"].local_switch
    cloud = _Cloud("ok", on_call=on_call)
    _, ex, sw, _, setup = _mirror_switch(monkeypatch, cloud, consent=True, local=False)
    holder["ex"] = ex

    async def go():
        await setup()
        await sw.async_turn_on()
    asyncio.run(go())
    assert holder["during"] is False and ex.local_switch is True


def test_switch_off_during_pending_on_wins(monkeypatch):
    # Wyłączenie w trakcie włączania: późne 200 z chmury nie zapala przełącznika.
    holder = {}

    async def on_call(kind):
        if kind == "ha":
            await holder["sw"].async_turn_off()
    cloud = _Cloud("ok", on_call=on_call)
    _, ex, sw, _, setup = _mirror_switch(monkeypatch, cloud, consent=True, local=False)
    holder["sw"] = sw

    async def go():
        await setup()
        await sw.async_turn_on()
        await _drain(sw)
    asyncio.run(go())
    assert ex.local_switch is False and cloud.calls[0] == ("ha", "ha_switch") and ("none", "ha_switch") in cloud.calls


def test_switch_on_cancels_pending_off_retry(monkeypatch):
    cloud = _Cloud("network")
    _, ex, sw, _, setup = _mirror_switch(monkeypatch, cloud)

    async def go():
        await setup()
        gate = asyncio.Event()

        async def blocked_sleep(_s):
            await gate.wait()
        sw._sleep = blocked_sleep
        await sw.async_turn_off()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        off_task = sw._off_task
        cloud.outcomes = ["ok"]
        await sw.async_turn_on()
        await asyncio.sleep(0)
        return off_task
    task = asyncio.run(go())
    assert task.cancelled() or task.done()
    assert cloud.calls[-1] == ("ha", "ha_switch") and ex.local_switch is True


def test_switch_on_already_on_does_not_call_cloud(monkeypatch):
    cloud = _Cloud("ok")
    _, ex, sw, _, setup = _mirror_switch(monkeypatch, cloud)

    async def go():
        await setup()
        await sw.async_turn_on()
    asyncio.run(go())
    assert cloud.calls == [] and ex.local_switch is True


def test_mirror_off_keeps_legacy_behaviour(monkeypatch):
    cloud = _Cloud("ok")
    _, ex, sw, _, setup = _mirror_switch(monkeypatch, cloud, consent=True, local=False, mirror=False)

    async def go():
        await setup()
        await sw.async_turn_on()
        on = ex.local_switch
        await sw.async_turn_off()
        await _drain(sw)
        return on
    assert asyncio.run(go()) is True
    assert cloud.calls == [] and ex.local_switch is False and ex.consent is True and sw._off_task is None


def test_restore_state_does_not_call_set_controller(monkeypatch):
    # Start/przeładowanie: stan przełącznika z magazynu i pierwszy plan nie wołają `set-controller`.
    from custom_components.volcast.control.store import ControlState, ControlStore
    from tests.control.test_executor import make, plan
    from tests.control.ha_fakes import goodwe_hass
    from custom_components.volcast.core.slot import parse_schedule
    for consent, local, cloud_consent in ((True, True, True), (False, False, True), (True, False, True),
                                          (True, True, False)):
        h = goodwe_hass()
        store = ControlStore(h, "e1")
        raw = plan()
        asyncio.run(store.async_save(ControlState(consent=consent, local_switch=local, plan_raw=raw)))
        _, ex = make(h, store=store, monkeypatch=monkeypatch)
        cloud = _Cloud("ok")
        sw = VolcastControlSwitch(SimpleNamespace(entry_id="e1", options={}),
                                  SimpleNamespace(executor=ex, cloud=cloud))
        sw.hass = h
        sw.async_on_remove = lambda _unsub: None          # atrapa encji HA nie ma tych metod
        sw.async_write_ha_state = lambda: None

        async def go():
            await ex.async_start()
            await sw.async_added_to_hass()
            ex.set_consent_mirror(True)
            await ex.async_on_plan(raw, parse_schedule(raw))
            await ex.async_set_consent(cloud_consent)
            await ex.async_tick()
        asyncio.run(go())
        assert cloud.calls == [], (consent, local, cloud_consent)
        assert sw._off_task is None


test_state_restore_does_not_call_set_controller = test_restore_state_does_not_call_set_controller
