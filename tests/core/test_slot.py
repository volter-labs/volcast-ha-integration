from datetime import datetime, timedelta, timezone

import pytest

from custom_components.volcast.core.slot import (
    Action, InvalidSchedule, Slot, effective_action, parse_schedule, slot_direction,
)

UTC = timezone.utc


def _s(**kw):
    base = {"from": "2026-09-01T10:00:00Z", "to": "2026-09-01T11:00:00Z", "mode": "self_consume"}
    base.update(kw)
    return base


def _doc(*slots, **kw):
    d = {"schedule_id": "t", "generated_at": "2026-09-01T09:00:00Z", "slots": list(slots),
         "fallback": {"mode": "self_consume", "soc_reserve": 15}, "control_enabled": True}
    d.update(kw)
    return d


def test_parses_full_slot():
    sch = parse_schedule(_doc(_s(mode="charge", charge_source="grid", power_w=3000,
                                 soc_target=90, price_pln_kwh=0.3, export_allowed=False,
                                 export_limit_w=None)))
    s = sch.slots[0]
    assert (s.action, s.charge_source, s.power_w, s.soc_target, s.export_allowed) == (
        Action.CHARGE, "grid", 3000.0, 90.0, False)
    assert s.hours == 1.0 and sch.control_enabled is True and sch.fallback.soc_reserve == 15.0


def test_missing_slots_key_is_error():
    with pytest.raises(InvalidSchedule):
        parse_schedule({"schedule_id": "x"})


def test_missing_control_enabled_is_false():
    doc = _doc(_s())
    del doc["control_enabled"]
    assert parse_schedule(doc).control_enabled is False


@pytest.mark.parametrize("bad", [
    {"power_w": "300"}, {"power_w": True}, {"power_w": float("nan")},
    {"export_allowed": "false"}, {"mode": "sell"}, {"mode": None},
    {"charge_source": "PV"}, {"mode": "idle", "charge_source": "pv"},
    {"mode": "hold", "discharge_purpose": "sell"}, {"mode": "charge", "discharge_purpose": "self"},
    {"mode": "discharge", "charge_source": "grid"},
    {"charge_source": "pv", "discharge_purpose": "self"},
    {"export_limit_w": -1}, {"to": "2026-09-01T10:00:00Z"},
])
def test_one_bad_slot_rejects_whole_plan(bad):
    with pytest.raises(InvalidSchedule):
        parse_schedule(_doc(_s(), _s(**{"from": "2026-09-01T11:00:00Z",
                                        "to": "2026-09-01T12:00:00Z", **bad})))


def test_overlapping_slots_are_rejected():
    with pytest.raises(InvalidSchedule):
        parse_schedule(_doc(_s(**{"from": "2026-09-01T10:00:00Z", "to": "2026-09-01T12:00:00Z"}),
                            _s(**{"from": "2026-09-01T11:00:00Z", "to": "2026-09-01T13:00:00Z"})))


def test_slots_sorted_stably_by_start():
    sch = parse_schedule(_doc(_s(**{"from": "2026-09-01T12:00:00Z", "to": "2026-09-01T13:00:00Z"}),
                              _s()))
    assert [s.start.hour for s in sch.slots] == [10, 12]


def test_effective_slot_falls_back_without_price_and_export():
    sch = parse_schedule(_doc(_s()))
    slot, fb = sch.effective_slot(datetime(2026, 9, 1, 15, 30, tzinfo=UTC))
    assert fb is True
    assert (slot.action, slot.soc_target, slot.export_allowed, slot.price_pln_kwh) == (
        Action.SELF_CONSUME, 15.0, False, None)
    assert slot.end - slot.start == timedelta(hours=1)


def _slot(**kw):
    return Slot(start=datetime(2026, 9, 1, tzinfo=UTC), end=datetime(2026, 9, 1, 1, tzinfo=UTC),
                action=kw.pop("action", Action.SELF_CONSUME), **kw)


def test_direction_from_descriptive_fields():
    assert slot_direction(_slot(discharge_purpose="self")) is Action.DISCHARGE
    assert slot_direction(_slot(charge_source="pv")) is Action.CHARGE
    assert slot_direction(_slot()) is None
    assert slot_direction(_slot(action=Action.IDLE)) is None


def test_effective_action_never_none():
    assert effective_action(_slot()) is Action.SELF_CONSUME
    assert effective_action(_slot(action=Action.IDLE)) is Action.IDLE
    assert effective_action(_slot(action=Action.HOLD)) is Action.HOLD
    assert effective_action(_slot(discharge_purpose="sell")) is Action.DISCHARGE
