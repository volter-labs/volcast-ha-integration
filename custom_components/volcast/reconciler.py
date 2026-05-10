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

from .production import PERMANENT_SKIP_REASONS

# Powody odrzucenia, których reconciler NIE powinien retry'ować — bazujemy na
# zestawie z production.py + dorzucamy "older_than_24h" (jeśli backend już
# odmówił z powodu zbyt starego wpisu, kolejna próba też się nie uda).
# Dziedziczenie z production.py zapobiega driftowi gdy ktoś rozszerzy zestaw
# w jednym miejscu a zapomni o drugim.
RECONCILER_PERMANENT_SKIP_REASONS = PERMANENT_SKIP_REASONS | frozenset({"older_than_24h"})

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

    async def reconcile_day(self, target_date: date) -> ReconcileResult:
        """Uzgodnij produkcję dla pojedynczego dnia. Idempotentne.

        Skip-gates (kolejność):
         1. age > RECONCILE_WINDOW_HOURS → reason='out_of_window'
         2. _fetch_ha_statistics zwrócił {} → reason='no_stats'
         3. brak luk (każda godzina z statystyk już w _accepted lub kwh<próg)
            → success=True, submitted=0, brak POSTu

        Inaczej: zbiera brakujące godziny, POST przez _post_reconciliation
        z `is_reconciliation: true` w payloadzie.
        """
        today = datetime.now(self._tz).date()
        age_hours = (today - target_date).days * 24
        if age_hours > RECONCILE_WINDOW_HOURS:
            _LOGGER.debug(
                "Skip reconcile %s — too old (%dh > %dh window)",
                target_date, age_hours, RECONCILE_WINDOW_HOURS,
            )
            return ReconcileResult(skipped=True, reason="out_of_window")

        hourly_stats = await self._fetch_ha_statistics(target_date)
        if not hourly_stats:
            _LOGGER.warning(
                "No statistics for %s — recorder unavailable or no data?",
                target_date,
            )
            return ReconcileResult(skipped=True, reason="no_stats")

        await self._tracker._load_accepted_store()
        accepted_hours = set(self._tracker._accepted.get(target_date.isoformat(), []))

        missing: list[dict] = []
        for hour, kwh in sorted(hourly_stats.items()):
            if hour in accepted_hours:
                continue
            if kwh < MIN_REPORT_KWH:
                continue
            missing.append({
                "date": target_date.isoformat(),
                "hour": hour,
                "actual_kwh": round(kwh, 4),
                "data_method": "energy_delta_reconciliation",
            })

        if not missing:
            _LOGGER.debug("Reconcile %s — no gaps", target_date)
            return ReconcileResult(success=True, submitted=0)

        return await self._post_reconciliation(missing)

    async def _post_reconciliation(self, readings: list[dict]) -> ReconcileResult:
        """POST brakujących godzin z flagą is_reconciliation=true.

        Backend rozpoznaje tę flagę i pomija Kalman+nowcast (Phase 1 commits).
        Na sukces oznacza godziny jako accepted; non-permanent-skip rejections
        zostają NIE oznaczone, żeby kolejny przebieg reconciler je powtórzył.
        """
        session = async_get_clientsession(self._hass)
        result = await http_with_retry(
            session,
            method="POST",
            url=self._submit_url,
            payload={"readings": readings, "is_reconciliation": True},
            headers={
                "X-API-Key": self._api_key,
                "Content-Type": "application/json",
            },
        )

        if result.success:
            data = result.data or {}
            rejections = data.get("rejections") or []
            rejected_keys: dict[tuple[str, int], str] = {}
            for rej in rejections:
                try:
                    rejected_keys[(rej["date"], rej["hour"])] = rej.get("reason", "")
                except (KeyError, TypeError):
                    continue

            for r in readings:
                key = (r["date"], r["hour"])
                reason = rejected_keys.get(key)
                if reason is None or reason in RECONCILER_PERMANENT_SKIP_REASONS:
                    await self._tracker._mark_accepted(r["date"], r["hour"])

            accepted_count = data.get("accepted", 0)
            rejected_count = data.get("rejected", len(rejected_keys))
            _LOGGER.info(
                "Reconciliation: submitted %d hours for %s, accepted=%d rejected=%d",
                len(readings), readings[0]["date"], accepted_count, rejected_count,
            )
            return ReconcileResult(
                success=True,
                submitted=len(readings),
                accepted=accepted_count,
            )

        _LOGGER.warning(
            "Reconciliation submit failed after %d attempts (%s); will retry next schedule",
            result.attempts, result.last_error,
        )
        return ReconcileResult(
            success=False,
            submitted=len(readings),
            error=result.last_error,
        )

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
