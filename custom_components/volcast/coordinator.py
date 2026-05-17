"""DataUpdateCoordinator for Volcast Solar Forecast."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, DEFAULT_UPDATE_INTERVAL
from .http_retry import http_with_retry

_LOGGER = logging.getLogger(__name__)


@dataclass
class DailyForecast:
    """Single day forecast."""

    date: str
    energy_kwh: float
    peak_power_kw: float
    confidence: float | str
    sunshine_hours: float
    cloud_cover_pct: float


@dataclass
class HourlyEntry:
    """Single hour forecast."""

    hour: int
    power_kw: float
    energy_kwh: float


@dataclass
class DetailedEntry:
    """5-minute forecast entry."""

    time: str  # "HH:MM"
    power_w: int
    energy_wh: int


@dataclass
class VolcastData:
    """Parsed API response."""

    energy_today: float
    energy_tomorrow: float
    forecast: list[DailyForecast]
    hourly: dict[str, list[HourlyEntry]]  # date → hours
    detailed: dict[str, list[DetailedEntry]]  # date → 5-min entries (today+tomorrow)
    wh_hours: dict[str, float | int]  # ISO timestamp → Wh (for Energy Dashboard)
    system_capacity_kwp: float | None
    location: str
    generated_at: str
    cache_age_minutes: int
    api_version: int
    api_status: str  # "Active", "Premium required", etc.
    nowcast_applied: bool = False
    nowcast_ratio: float | None = None
    submit_url: str = ""


class VolcastCoordinator(DataUpdateCoordinator[VolcastData]):
    """Coordinator that polls the Volcast API."""

    def __init__(
        self,
        hass: HomeAssistant,
        api_key: str,
        api_url: str,
        update_interval_minutes: int = DEFAULT_UPDATE_INTERVAL,
    ) -> None:
        """Initialize."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=update_interval_minutes),
        )
        self._api_key = api_key
        self._api_url = api_url

    async def _async_update_data(self) -> VolcastData:
        """Fetch data from Volcast API with retry (5/15/45s) on transient errors."""
        url = f"{self._api_url}?key={self._api_key}"
        session = async_get_clientsession(self.hass)

        result = await http_with_retry(
            session,
            method="GET",
            url=url,
            headers={},
        )

        if result.success:
            return _parse_response(result.data, self.hass)

        # Mapowanie statusów po wyczerpaniu retries — zachowujemy istniejącą semantykę
        if result.status == 401:
            raise UpdateFailed("Invalid API key")
        if result.status == 403:
            return _error_data("Premium required")
        if result.status == 503:
            raise UpdateFailed("Forecast not yet available — cache being populated")
        if result.status == 429:
            raise UpdateFailed(
                f"Rate limit exceeded — retry later (after {result.attempts} attempts)"
            )
        if result.status is not None:
            raise UpdateFailed(
                f"Volcast API error: {result.status} (after {result.attempts} attempts)"
            )
        raise UpdateFailed(
            f"Connection error: {result.last_error} (after {result.attempts} attempts)"
        )


def _error_data(status: str) -> VolcastData:
    """Return empty data with an error status."""
    return VolcastData(
        energy_today=0,
        energy_tomorrow=0,
        forecast=[],
        hourly={},
        detailed={},
        wh_hours={},
        system_capacity_kwp=None,
        location="",
        generated_at=datetime.now(timezone.utc).isoformat(),
        cache_age_minutes=0,
        api_version=0,
        api_status=status,
        nowcast_applied=False,
        nowcast_ratio=None,
        submit_url="",
    )


def _parse_response(raw: dict[str, Any], hass: HomeAssistant) -> VolcastData:
    """Parse API JSON into VolcastData."""
    attrs = raw.get("attributes", {})
    api_version = attrs.get("api_version", 1)

    # Daily forecast
    forecast: list[DailyForecast] = []
    for d in attrs.get("forecast", []):
        forecast.append(
            DailyForecast(
                date=d["date"],
                energy_kwh=d.get("energy_kwh", 0),
                peak_power_kw=d.get("peak_power_kw", 0),
                confidence=d.get("confidence", 0),
                sunshine_hours=d.get("sunshine_hours", 0),
                cloud_cover_pct=d.get("cloud_cover_pct", 50),
            )
        )

    # Hourly data — keyed by date
    hourly: dict[str, list[HourlyEntry]] = {}
    raw_hourly = attrs.get("hourly", {})
    for date_str, hours in raw_hourly.items():
        hourly[date_str] = [
            HourlyEntry(
                hour=h.get("hour", 0),
                power_kw=h.get("power_kw", 0),
                energy_kwh=h.get("energy_kwh", 0),
            )
            for h in hours
        ]

    # Detailed 5-min data (api_version >= 2)
    detailed: dict[str, list[DetailedEntry]] = {}
    raw_detailed = attrs.get("detailed", {})
    for date_str, entries in raw_detailed.items():
        detailed[date_str] = [
            DetailedEntry(
                time=e.get("time", "00:00"),
                power_w=e.get("power_w", 0),
                energy_wh=e.get("energy_wh", 0),
            )
            for e in entries
        ]

    # Build wh_hours for HA Energy Dashboard
    ha_tz = hass.config.time_zone
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(ha_tz)
    except Exception:
        tz = timezone.utc

    wh_hours = _build_wh_hours(hourly, detailed, tz)

    # Today/tomorrow energy
    today_str = datetime.now(tz).strftime("%Y-%m-%d")
    tomorrow_dt = datetime.now(tz) + timedelta(days=1)
    tomorrow_str = tomorrow_dt.strftime("%Y-%m-%d")

    # energy_today is derived from the forecast array using HA's local timezone.
    # We intentionally ignore raw["state"] because the server computes it with a
    # UTC date, which is wrong for east-of-UTC users in their local evening
    # (UTC date < local date → "today's" state is actually yesterday's value).
    # Mirror image of the energy_tomorrow logic immediately below.
    energy_today = 0.0
    for d in forecast:
        if d.date == today_str:
            energy_today = d.energy_kwh
            break
    energy_tomorrow = 0.0
    for d in forecast:
        if d.date == tomorrow_str:
            energy_tomorrow = d.energy_kwh
            break

    return VolcastData(
        energy_today=energy_today,
        energy_tomorrow=energy_tomorrow,
        forecast=forecast,
        hourly=hourly,
        detailed=detailed,
        wh_hours=wh_hours,
        system_capacity_kwp=attrs.get("system_capacity_kwp"),
        location=attrs.get("location", ""),
        generated_at=attrs.get("generated_at", ""),
        cache_age_minutes=attrs.get("cache_age_minutes", 0),
        api_version=api_version,
        api_status="Active",
        nowcast_applied=attrs.get("nowcast_applied", False),
        nowcast_ratio=attrs.get("nowcast_ratio"),
        submit_url=attrs.get("submit_url", ""),
    )


def _build_wh_hours(
    hourly: dict[str, list[HourlyEntry]],
    detailed: dict[str, list[DetailedEntry]],
    tz: Any,
) -> dict[str, float | int]:
    """Build wh_hours dict for HA Energy Dashboard.

    For dates with 5-min detailed data: aggregate energy_wh into hourly Wh.
    For other dates: use hourly energy_kwh * 1000.
    """
    wh_hours: dict[str, float | int] = {}

    for date_str, hours in hourly.items():
        if date_str in detailed and detailed[date_str]:
            # Aggregate 5-min entries into hourly Wh
            hourly_wh: dict[int, int] = {}
            for entry in detailed[date_str]:
                try:
                    entry_hour = int(entry.time.split(":")[0])
                except (ValueError, IndexError):
                    continue
                hourly_wh[entry_hour] = hourly_wh.get(entry_hour, 0) + entry.energy_wh

            for hour_num, wh in hourly_wh.items():
                try:
                    dt = datetime(
                        int(date_str[:4]),
                        int(date_str[5:7]),
                        int(date_str[8:10]),
                        hour_num,
                        tzinfo=tz,
                    )
                    wh_hours[dt.isoformat()] = wh
                except (ValueError, IndexError):
                    continue
        else:
            # Fallback: hourly data
            for h in hours:
                try:
                    dt = datetime(
                        int(date_str[:4]),
                        int(date_str[5:7]),
                        int(date_str[8:10]),
                        h.hour,
                        tzinfo=tz,
                    )
                    wh = round(h.energy_kwh * 1000)  # kWh → Wh
                    wh_hours[dt.isoformat()] = wh
                except (ValueError, IndexError):
                    continue

    return wh_hours
