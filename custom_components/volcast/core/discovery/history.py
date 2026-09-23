"""Liczba dni z danymi w statystykach długoterminowych rekordera."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

HISTORY_WINDOW_DAYS = 90


def days_with_statistics(rows: list[dict], tz_name: str) -> int:
    tz = ZoneInfo(tz_name)
    days: set = set()
    for row in rows or []:
        start = row.get("start") if isinstance(row, dict) else None
        if isinstance(start, (int, float)):
            start = datetime.fromtimestamp(start, tz=timezone.utc)
        if isinstance(start, datetime):
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            days.add(start.astimezone(tz).date())
    return len(days)
