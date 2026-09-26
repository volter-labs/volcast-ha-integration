import json
from datetime import datetime, timedelta, timezone

from custom_components.volcast.core.control.history import (
    MAX_BATCH_BYTES,
    MAX_HOURS,
    batches,
    hours_from_statistics,
)

NOW = datetime(2026, 9, 27, 10, 30, tzinfo=timezone.utc)


def row(dt, change):
    return {"start": dt.timestamp(), "change": change}


def test_complete_past_hours_only_in_utc_z_format():
    rows = [row(datetime(2026, 9, 27, 8, tzinfo=timezone.utc), 0.4),
            row(datetime(2026, 9, 27, 9, tzinfo=timezone.utc), 0.5),
            row(datetime(2026, 9, 27, 10, tzinfo=timezone.utc), 0.2)]      # bieżąca — niepełna
    assert hours_from_statistics(rows, load_unit="kWh", now_utc=NOW) == [
        {"start": "2026-09-27T08:00:00Z", "load_kwh": 0.4},
        {"start": "2026-09-27T09:00:00Z", "load_kwh": 0.5}]


def test_units_negative_and_garbage_rows():
    rows = [row(datetime(2026, 9, 27, 8, tzinfo=timezone.utc), 400.0),     # Wh
            row(datetime(2026, 9, 27, 9, tzinfo=timezone.utc), -5.0),      # licznik wyzerowany
            {"start": "x", "change": 1.0}, {"start": NOW.timestamp(), "change": None}]
    assert hours_from_statistics(rows, load_unit="Wh", now_utc=NOW) == [
        {"start": "2026-09-27T08:00:00Z", "load_kwh": 0.4}]
    assert hours_from_statistics(rows, load_unit="W", now_utc=NOW) == []   # nie energia


def test_window_60_days_and_cap():
    old = row(NOW - timedelta(days=61), 1.0)
    assert hours_from_statistics([old], load_unit="kWh", now_utc=NOW) == []
    many = [row(datetime(2026, 7, 1, tzinfo=timezone.utc) + timedelta(hours=i), 0.1) for i in range(2000)]
    assert len(hours_from_statistics(many, load_unit="kWh", now_utc=NOW)) <= MAX_HOURS


def test_optional_series_joined_by_hour_and_datetime_starts():
    t = datetime(2026, 9, 27, 8, tzinfo=timezone.utc)
    out = hours_from_statistics([{"start": t, "change": 0.4}], load_unit="kWh", now_utc=NOW,
                                extra={"pv_kwh": ([row(t, 1.2)], "kWh")})
    assert out == [{"start": "2026-09-27T08:00:00Z", "load_kwh": 0.4, "pv_kwh": 1.2}]


def test_mwh_misaligned_naive_nan_and_bool_rows_skipped():
    t = datetime(2026, 9, 27, 8, tzinfo=timezone.utc)
    rows = [row(t, 0.0005),                                                 # MWh → 0,5 kWh
            row(t + timedelta(minutes=30), 1.0),                            # nie na pełnej godzinie
            {"start": datetime(2026, 9, 27, 7), "change": 1.0},             # bez strefy
            row(t - timedelta(hours=2), float("nan")),
            row(t - timedelta(hours=3), True),
            "śmieć", None]
    assert hours_from_statistics(rows, load_unit="MWh", now_utc=NOW) == [
        {"start": "2026-09-27T08:00:00Z", "load_kwh": 0.5}]
    assert hours_from_statistics(rows, load_unit=None, now_utc=NOW) == []


def test_extra_series_in_wrong_unit_is_left_out_not_the_hour():
    t = datetime(2026, 9, 27, 8, tzinfo=timezone.utc)
    out = hours_from_statistics([row(t, 0.4)], load_unit="kWh", now_utc=NOW,
                                extra={"pv_kwh": ([row(t, 1.2)], "W"),
                                       "import_kwh": ([row(t, 300.0)], "Wh")})
    assert out == [{"start": "2026-09-27T08:00:00Z", "load_kwh": 0.4, "import_kwh": 0.3}]


def test_batches_respect_hour_count_and_byte_budget():
    hours = [{"start": f"2026-09-{d:02d}T{h:02d}:00:00Z", "load_kwh": 0.1}
             for d in range(1, 11) for h in range(24)]                     # 240 godzin
    assert batches([]) == []
    assert batches(hours) == [hours]                                       # jedna partia
    by_count = batches(hours, max_hours=100)
    assert [len(b) for b in by_count] == [100, 100, 40]
    by_bytes = batches(hours, max_bytes=2000)
    assert sum(by_bytes, []) == hours                                      # nic nie ginie, kolejność ta sama
    for b in by_bytes:
        assert len(json.dumps({"source": "ha_recorder", "hours": b}).encode()) <= 2000


def test_full_60_days_with_all_series_fits_one_request():
    start = NOW.replace(minute=0) - timedelta(days=60)
    rows = [row(start + timedelta(hours=i), 12.3456) for i in range(24 * 60)]
    extra = {k: (rows, "kWh") for k in ("pv_kwh", "import_kwh", "export_kwh")}
    hours = hours_from_statistics(rows, load_unit="kWh", now_utc=NOW, extra=extra)
    assert len(hours) == 24 * 60 <= MAX_HOURS
    out = batches(hours)
    assert len(out) == 1
    assert len(json.dumps({"source": "ha_recorder", "hours": out[0]}).encode()) < MAX_BATCH_BYTES
