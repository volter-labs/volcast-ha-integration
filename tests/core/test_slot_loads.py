from custom_components.volcast.core.ev.model import LoadSlot, LoadsMeta, parse_loads
from custom_components.volcast.core.slot import parse_schedule

_SLOT = {"device_id": "wallbox", "from": "2026-10-01T10:00:00Z", "to": "2026-10-01T11:00:00Z",
         "mode": "forced", "power_w": 7400, "min_power_w": 1380,
         "session_id": "s1", "battery_guard": "bypass"}
_META = {"session_id": "s1", "load_key": "wallbox", "control_enabled": True,
         "ready_by": "2026-10-01T18:00:00Z", "remaining_kwh": 12.5, "max_power_w": 11040,
         "phases": 3, "control_unit": "A", "min": 6, "max": 16, "step": 1,
         "plan_generated_at": "2026-10-01T09:00:00Z"}


def test_parse_full():
    loads, meta = parse_loads({"loads": [_SLOT], "loads_meta": _META})
    assert loads == [LoadSlot(
        device_id="wallbox", start=loads[0].start, end=loads[0].end, mode="forced",
        power_w=7400.0, min_power_w=1380.0, session_id="s1", battery_guard="bypass")]
    assert loads[0].start.tzinfo is not None
    assert isinstance(meta, LoadsMeta)
    assert (meta.session_id, meta.load_key, meta.phases, meta.control_unit) == ("s1", "wallbox", 3, "A")
    assert (meta.min, meta.max, meta.step, meta.control_enabled) == (6.0, 16.0, 1.0, True)
    assert meta.remaining_kwh == 12.5 and meta.ready_by is not None


def test_missing_or_garbage_is_empty():
    for raw in ({}, {"loads": None}, {"loads": "x", "loads_meta": 5}, {"loads": [1, None, "a"]}, None, 7):
        assert parse_loads(raw) == ([], None)


def test_bad_slots_dropped_individually():
    bad = [dict(_SLOT, mode="turbo"), dict(_SLOT, to=_SLOT["from"]), dict(_SLOT, power_w="x"),
           dict(_SLOT, **{"from": "2026-10-01T10:00:00"}), dict(_SLOT, device_id="")]
    loads, _ = parse_loads({"loads": [bad[0], _SLOT, *bad[1:]]})
    assert len(loads) == 1


def test_optional_slot_fields_default():
    raw = {k: v for k, v in _SLOT.items() if k not in ("session_id", "battery_guard")}
    loads, _ = parse_loads({"loads": [dict(raw, power_w=None, min_power_w=None)]})
    assert loads[0].session_id is None and loads[0].battery_guard is None and loads[0].power_w is None


def test_meta_without_identity_is_none():
    assert parse_loads({"loads_meta": {"phases": 3}})[1] is None
    assert parse_loads({"loads_meta": dict(_META, session_id=None)})[1] is None


def test_meta_bad_fields_become_none_and_phases_default():
    _, meta = parse_loads({"loads_meta": dict(_META, phases="x", control_unit="kW", min="a", ready_by="zle")})
    assert meta is not None and meta.phases == 1 and meta.control_unit is None
    assert meta.min is None and meta.ready_by is None


def test_schedule_without_loads_still_parses():
    sch = parse_schedule({"schedule_id": "t", "slots": [], "control_enabled": True})
    assert sch.loads == () and sch.loads_meta is None


def test_schedule_carries_loads():
    sch = parse_schedule({"schedule_id": "t", "slots": [], "loads": [_SLOT], "loads_meta": _META})
    assert len(sch.loads) == 1 and sch.loads_meta.load_key == "wallbox"


def test_schedule_bad_loads_do_not_invalidate_battery_plan():
    sch = parse_schedule({"schedule_id": "t", "slots": [], "loads": "junk", "loads_meta": []})
    assert sch.loads == () and sch.loads_meta is None
