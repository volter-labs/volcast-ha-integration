from datetime import datetime, timezone
from custom_components.volcast.core.discovery.history import days_with_statistics


def test_counts_distinct_local_days_epoch_and_datetime():
    rows = [
        {"start": datetime(2026, 9, 1, 22, 0, tzinfo=timezone.utc)},   # 2.09 w Warszawie
        {"start": datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc).timestamp()},
        {"start": datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc).timestamp()},
    ]
    assert days_with_statistics(rows, "Europe/Warsaw") == 2


def test_empty_and_malformed_rows_are_zero():
    assert days_with_statistics([], "Europe/Warsaw") == 0
    assert days_with_statistics([{"nope": 1}, {"start": None}], "Europe/Warsaw") == 0
