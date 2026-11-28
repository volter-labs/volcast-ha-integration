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
    {"from": "2026-09-01T11:00:00"},                    # bez strefy
    {"from": "2026-09-01"},                              # sama data
    {"from": "20260901T110000Z"},                        # zapis zbity
    {"from": "2026-09-01 11:00:00Z"},                    # separator spacją
])
def test_one_bad_slot_rejects_whole_plan(bad):
    with pytest.raises(InvalidSchedule) as ei:
        parse_schedule(_doc(_s(), _s(**{"from": "2026-09-01T11:00:00Z",
                                        "to": "2026-09-01T12:00:00Z", **bad})))
    assert ei.value.field.startswith("slots[1]")


def test_generated_at_without_timezone_is_rejected():
    with pytest.raises(InvalidSchedule):
        parse_schedule(_doc(_s(), generated_at="2026-09-01T09:00:00"))


def test_overlapping_slots_are_rejected():
    with pytest.raises(InvalidSchedule):
        parse_schedule(_doc(_s(**{"from": "2026-09-01T10:00:00Z", "to": "2026-09-01T12:00:00Z"}),
                            _s(**{"from": "2026-09-01T11:00:00Z", "to": "2026-09-01T13:00:00Z"})))


def test_adjacent_slots_are_accepted():
    sch = parse_schedule(_doc(_s(**{"from": "2026-09-01T10:00:00Z", "to": "2026-09-01T11:00:00Z"}),
                              _s(**{"from": "2026-09-01T11:00:00Z", "to": "2026-09-01T12:00:00Z"})))
    assert len(sch.slots) == 2


def test_top_level_fields_fail_closed():
    with pytest.raises(InvalidSchedule):
        parse_schedule(_doc(_s(), fallback=[]))
    with pytest.raises(InvalidSchedule):
        parse_schedule(_doc(_s(), fallback=0))
    with pytest.raises(InvalidSchedule):
        parse_schedule(_doc(_s(), fallback=False))
    with pytest.raises(InvalidSchedule):
        parse_schedule(_doc(_s(), schedule_id=123))
    with pytest.raises(InvalidSchedule):
        parse_schedule(_doc(_s(), generated_at=0))
    with pytest.raises(InvalidSchedule):
        parse_schedule(_doc(_s(), fallback={"mode": "x"}))
    with pytest.raises(InvalidSchedule):
        parse_schedule(_doc(_s(), fallback={"soc_reserve": "15"}))


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


# ── limit wieku planu bez kontaktu z chmurą ──────────────────────────────────
from custom_components.volcast.core.slot import STALE_PLAN_S, is_active_slot, plan_stale  # noqa: E402


def test_stale_plan_limit_is_sixty_minutes():
    assert STALE_PLAN_S == 60 * 60


@pytest.mark.parametrize("kw, active", [
    (dict(mode="charge", charge_source="grid", power_w=3000), True),     # ładowanie z sieci
    (dict(mode="charge", power_w=3000), True),                           # stary kontrakt: ładowanie z sieci
    (dict(mode="self_consume", charge_source="grid", power_w=3000), True),
    (dict(mode="discharge", discharge_purpose="sell", power_w=3000), True),
    (dict(mode="discharge", power_w=3000), True),                        # wymuszone rozładowanie
    (dict(mode="charge", charge_source="pv"), False),                    # nadwyżka PV — pasywne
    (dict(mode="discharge", discharge_purpose="self"), False),           # autokonsumpcja
    (dict(mode="self_consume"), False),
    (dict(mode="idle"), False),
    (dict(mode="hold"), False),
])
def test_active_slot_classification(kw, active):
    assert is_active_slot(parse_schedule(_doc(_s(**kw))).slots[0]) is active


def test_plan_stale_by_fetch_age():
    now = datetime(2026, 9, 1, 12, tzinfo=UTC)
    assert plan_stale(now - timedelta(minutes=61), now) is True
    assert plan_stale(now - timedelta(minutes=59), now) is False
    assert plan_stale(now - timedelta(seconds=STALE_PLAN_S), now) is False    # granica: dokładnie 60 min — świeży
    assert plan_stale(None, now) is True                                      # wiek nieznany = przeterminowany
    assert plan_stale(now + timedelta(hours=1), now) is True                  # znacznik z przyszłości (zegar cofnięty)
    assert plan_stale(now + timedelta(seconds=60), now) is False              # drobna korekta zegara


def _mixed():
    return parse_schedule(_doc(
        _s(**{"from": "2026-09-01T10:00:00Z", "to": "2026-09-01T11:00:00Z"}, mode="charge",
           charge_source="grid", power_w=3000, soc_target=90, price_pln_kwh=0.2),
        _s(**{"from": "2026-09-01T11:00:00Z", "to": "2026-09-01T12:00:00Z"}, mode="idle", export_allowed=True),
        _s(**{"from": "2026-09-01T12:00:00Z", "to": "2026-09-01T13:00:00Z"}, mode="discharge",
           discharge_purpose="sell", power_w=4000, price_pln_kwh=1.1),
        _s(**{"from": "2026-09-01T13:00:00Z", "to": "2026-09-01T14:00:00Z"}, mode="discharge", power_w=2000)))


def test_active_slots_become_fallback_passive_stay():
    sch = _mixed()
    masked = sch.active_as_fallback()
    fb = sch.fallback
    assert [s.action for s in masked.slots] == [fb.action, Action.IDLE, fb.action, fb.action]
    for i in (0, 2, 3):
        s = masked.slots[i]
        assert (s.start, s.end) == (sch.slots[i].start, sch.slots[i].end)    # granice slotu planu zostają
        assert s.soc_target == fb.soc_reserve and s.export_allowed is False
        assert s.power_w is None and s.charge_source is None and s.discharge_purpose is None
    assert masked.slots[1] == sch.slots[1]                                    # pasywny bez zmian
    assert (masked.schedule_id, masked.fallback, masked.control_enabled) == (
        sch.schedule_id, sch.fallback, sch.control_enabled)
    assert sch.slots[0].action is Action.CHARGE                               # oryginał nietknięty


def test_effective_slot_reports_fallback_for_masked_slot():
    masked = _mixed().active_as_fallback()
    slot, is_fallback = masked.effective_slot(datetime(2026, 9, 1, 10, 30, tzinfo=UTC))
    assert is_fallback is True and slot.action is Action.SELF_CONSUME
    slot, is_fallback = masked.effective_slot(datetime(2026, 9, 1, 11, 30, tzinfo=UTC))
    assert is_fallback is False and slot.action is Action.IDLE
    # Świeży plan: slot aktywny jak w planie, nie zapasowy.
    slot, is_fallback = _mixed().effective_slot(datetime(2026, 9, 1, 10, 30, tzinfo=UTC))
    assert is_fallback is False and slot.charge_source == "grid"
