"""Daily reconciliation — fills hourly production gaps using HA recorder statistics.

Reconciler reads `recorder.statistics_during_period` for the configured
energy_entity, computes hourly delta kWh for a target date, diffs against the
tracker's `_accepted` Store, and POSTs missing hours via http_with_retry with
`is_reconciliation: true` flag (so backend skips Kalman + nowcast).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .http_retry import http_with_retry
from .production import VolcastProductionTracker

_LOGGER = logging.getLogger(__name__)

# Powody odrzucenia, których reconciler NIE powinien retry'ować — semantycznie
# permanentne. Rozszerzona o "older_than_24h" względem PERMANENT_SKIP_REASONS
# w production.py: jeśli backend już odmówił z powodu zbyt starego wpisu,
# kolejna próba też się nie uda.
RECONCILER_PERMANENT_SKIP_REASONS = frozenset({
    "nighttime_hour",
    "negative_production",
    "exceeds_capacity",
    "invalid_date",
    "invalid_hour",
    "missing_fields",
    "older_than_24h",
})

# Maksymalny wiek dnia, który backend jeszcze przyjmuje (godziny).
RECONCILE_WINDOW_HOURS = 36

# Minimalna produkcja, która ma sens do reportowania (próg "noc").
MIN_REPORT_KWH = 0.001


@dataclass
class ReconcileResult:
    """Wynik pojedynczego wywołania reconcile_day."""

    success: bool = False
    submitted: int = 0
    accepted: int = 0
    skipped: bool = False
    reason: str | None = None
    error: str | None = None


class DailyReconciler:
    """Reconciles hourly production from HA statistics with backend records."""

    def __init__(
        self,
        hass: HomeAssistant,
        tracker: VolcastProductionTracker,
        energy_entity: str,
        api_key: str,
        submit_url: str,
    ) -> None:
        self._hass = hass
        self._tracker = tracker
        self._energy_entity = energy_entity
        self._api_key = api_key
        self._submit_url = submit_url

    @property
    def _tz(self) -> ZoneInfo:
        """Strefa czasowa HA — używana do określenia granic dnia 00:00→24:00."""
        try:
            return ZoneInfo(self._hass.config.time_zone)
        except Exception:
            return ZoneInfo("UTC")

    async def _fetch_ha_statistics(self, target_date: date) -> dict[int, float]:
        """Zwróć {hour: kwh} dla target_date z HA recorder statystyk.

        Okno rozszerzone 1h wstecz — godzina 00 dnia X potrzebuje sumy z 23
        dnia X-1 jako baseline do policzenia delty. Wpisy z poprzedniego dnia
        są używane TYLKO jako prev_sum, nie trafiają do wyniku.

        Counter reset (delta < 0): clamping do 0.0 zamiast ujemnej energii.
        """
        start = (
            datetime.combine(target_date, time.min, tzinfo=self._tz)
            - timedelta(hours=1)
        )
        end = start + timedelta(days=1, hours=1)

        raw = await get_instance(self._hass).async_add_executor_job(
            statistics_during_period,
            self._hass,
            start,
            end,
            {self._energy_entity},
            "hour",
            None,
            {"sum", "state"},
        )
        entries = raw.get(self._energy_entity, [])
        if not entries:
            return {}

        hourly: dict[int, float] = {}
        prev_sum: float | None = None
        for entry in sorted(entries, key=lambda e: e["start"]):
            local_start = entry["start"]
            if local_start.tzinfo is None:
                local_start = local_start.replace(tzinfo=timezone.utc)
            local_start = local_start.astimezone(self._tz)
            if local_start.date() != target_date:
                # Baseline-seed (poprzedni dzień) — zachowaj sum, nie raportuj.
                prev_sum = entry.get("sum")
                continue
            cur = entry.get("sum")
            if cur is None or prev_sum is None:
                prev_sum = cur
                continue
            delta = max(cur - prev_sum, 0.0)
            hourly[local_start.hour] = delta
            prev_sum = cur
        return hourly
