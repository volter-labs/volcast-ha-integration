"""DataUpdateCoordinator for Volcast Solar Forecast."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
import re
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    DEFAULT_UPDATE_INTERVAL,
    FORECAST_HISTORY_RETENTION_DAYS,
    FORECAST_HISTORY_STORAGE_VERSION,
    ISSUE_BRAND_PROFILE_READY,
    OPT_PROFILE_ID,
    PROFILE_FEED_PUBLIC_KEYS,
    PROFILE_FEED_STORAGE_KEY,
    PROFILE_FEED_STORAGE_VERSION,
)
from .core.profile_feed import ProfileFeedUpdater, packaged_verified_ids
from .http_retry import RETRIABLE_STATUSES, http_with_retry

_LOGGER = logging.getLogger(__name__)
# Feed profili jest wspólny dla wszystkich wpisów — osobny klucz w hass.data (nie pod DOMAIN,
# gdzie leżą wyłącznie wpisy).
PROFILE_FEED_DATA = "volcast_profile_feed"
_ISSUE_WARNING = getattr(getattr(ir, "IssueSeverity", None), "WARNING", "warning")
_LABEL_MAX = 64
# Tolerancja chwilowej awarii sieci/serwera: tyle nieudanych cykli z rzędu (każdy po wyczerpaniu retry
# w http_with_retry) zachowujemy ostatnie dane, zanim encje przejdą w unavailable.
TRANSIENT_FAILURE_GRACE_CYCLES = 3


class TransientUpdateFailed(UpdateFailed):
    """Błąd przejściowy (sieć/DNS, 429, 5xx) — kwalifikuje się do okresu karencji z ostatnimi danymi."""


async def async_get_profile_feed(hass: HomeAssistant, *, keys: dict[str, str] | None = None
                                 ) -> ProfileFeedUpdater:
    """Wspólny feed profili: przy pierwszym wołaniu feed z `.storage` (ponownie zweryfikowany) trafia do
    rejestru profili — wołać PRZED odczytem profili. Klucze domyślnie z `PROFILE_FEED_PUBLIC_KEYS`;
    puste = feed bezczynny (bez odczytu magazynu i bez pobierania)."""
    feed = hass.data.get(PROFILE_FEED_DATA)
    if feed is None:
        feed = ProfileFeedUpdater(
            Store(hass, PROFILE_FEED_STORAGE_VERSION, PROFILE_FEED_STORAGE_KEY),
            keys=dict(PROFILE_FEED_PUBLIC_KEYS if keys is None else keys),
            run_job=hass.async_add_executor_job,
            on_new_profiles=lambda ids, profiles: async_profile_ready_issues(hass, ids, profiles))
        hass.data[PROFILE_FEED_DATA] = feed
    await feed.async_load()
    return feed


def async_profile_feed_if_loaded(hass: HomeAssistant) -> ProfileFeedUpdater | None:
    return (getattr(hass, "data", None) or {}).get(PROFILE_FEED_DATA)


def _brand(profile_id: str) -> str:
    return profile_id.split("-", 1)[0]


def _install_hints(hass: HomeAssistant, verified_ids) -> tuple[set[str], list[str], set[str], set[str]]:
    """Kandydaci profili i producenci z raportów wykrywania wpisów, marki profili z opcji wpisów oraz
    marki, którymi wpis już steruje profilem zweryfikowanym (dla nich bez Naprawy)."""
    candidates: set[str] = set()
    makers: list[str] = []
    brands: set[str] = set()
    covered: set[str] = set()
    for data in ((getattr(hass, "data", None) or {}).get(DOMAIN) or {}).values():
        report = getattr(data.get("discovery"), "report", None) if isinstance(data, dict) else None
        inverters = report.get("inverters") if isinstance(report, dict) else None
        for inv in inverters if isinstance(inverters, list) else ():
            if not isinstance(inv, dict):
                continue
            cands = inv.get("profile_candidates")
            candidates.update(c for c in (cands if isinstance(cands, list) else ()) if isinstance(c, str))
            devices = inv.get("devices")
            for dev in devices if isinstance(devices, list) else ():
                maker = dev.get("manufacturer") if isinstance(dev, dict) else None
                if isinstance(maker, str) and maker.strip():
                    makers.append(maker.lower())
    entries = getattr(getattr(hass, "config_entries", None), "async_entries", None)
    for entry in (entries(DOMAIN) if callable(entries) else ()):
        pid = (getattr(entry, "options", None) or {}).get(OPT_PROFILE_ID)
        if isinstance(pid, str) and pid:
            (covered if pid in verified_ids else brands).add(_brand(pid))
    return candidates, makers, brands, covered


def _safe_label(raw: Any, fallback: str) -> str:
    # Tekst Napraw to markdown: z etykiety feedu tylko litery, cyfry, spacja i myślnik.
    text = "".join(c if c.isalnum() or c in " -" else " " for c in str(raw or ""))
    return " ".join(text.split())[:_LABEL_MAX].strip() or fallback


def async_profile_ready_issues(hass: HomeAssistant, ids, profiles, *, verified_ids=None) -> list[str]:
    """Naprawa informacyjna dla NOWYCH profili z feedu pasujących do tej instalacji (kandydat z wykrywania,
    marka producenta urządzenia — całe słowo — albo marka profilu wybranego we wpisie). Pomija marki, którymi
    wpis już steruje profilem zweryfikowanym. Niczego nie włącza (profil z feedu jest tylko do odczytu)."""
    verified = packaged_verified_ids() if verified_ids is None else frozenset(verified_ids)
    candidates, makers, brands, covered = _install_hints(hass, verified)
    created: list[str] = []
    for pid in sorted(ids):
        brand = _brand(pid)
        word = re.compile(rf"\b{re.escape(brand)}\b")
        if brand in covered or not (pid in candidates or brand in brands or any(word.search(m) for m in makers)):
            continue
        issue_id = f"{ISSUE_BRAND_PROFILE_READY}_{pid}"
        raw = profiles.get(pid) or {}
        ir.async_create_issue(
            hass, DOMAIN, issue_id, is_fixable=True, severity=_ISSUE_WARNING,
            translation_key=ISSUE_BRAND_PROFILE_READY,
            translation_placeholders={"profile": pid, "label": _safe_label(raw.get("label"), pid)},
            data={"profile_id": pid})
        created.append(issue_id)
    return created


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
    # Źródło produkcji i kalibracja liczone po stronie serwera (brak → None)
    production_source: str | None = None
    production_source_detail: str | None = None
    calibration: dict[str, Any] | None = None


class VolcastCoordinator(DataUpdateCoordinator[VolcastData]):
    """Coordinator that polls the Volcast API."""

    def __init__(
        self,
        hass: HomeAssistant,
        api_key: str,
        api_url: str,
        update_interval_minutes: int = DEFAULT_UPDATE_INTERVAL,
        entry_id: str | None = None,
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
        self._entry_id = entry_id

        # Diagnostyka dla czujnika wieku prognozy: porażki z rzędu i moment
        # ostatniej odpowiedzi serwera (udanej albo 403 — serwer odpowiedział).
        self.consecutive_failures = 0
        self.last_update_success_time: datetime | None = None

        # Rolling history of forecast wh_hours, keyed by ISO timestamp, served to
        # the HA Energy Dashboard so past days keep their forecast line. Persisted
        # per-entry; in-memory only when no entry_id (e.g. unit tests).
        self._forecast_history: dict[str, float | int] = {}
        self._history_loaded = False
        self._history_store: Store | None = (
            Store(hass, FORECAST_HISTORY_STORAGE_VERSION,
                  f"{DOMAIN}.forecast_history.{entry_id}")
            if entry_id is not None
            else None
        )

    async def async_load_forecast_history(self) -> None:
        """Load persisted forecast history once (idempotent).

        Loaded eagerly during setup so the Energy Dashboard still has past-day
        forecasts after a restart even before (or if) the first poll fails.
        """
        if self._history_loaded:
            return
        self._history_loaded = True
        if self._history_store is None:
            return
        try:
            stored = await self._history_store.async_load()
        except Exception:  # noqa: BLE001 — corrupt/missing store must not block setup
            _LOGGER.warning("Failed to load forecast history store", exc_info=True)
            stored = None
        if isinstance(stored, dict):
            wh = stored.get("wh_hours", {})
            if isinstance(wh, dict):
                self._forecast_history = dict(wh)
        self._prune_history()

    def get_solar_forecast_wh_hours(self) -> dict[str, float | int] | None:
        """Return merged forecast (past history + current poll) for the Energy
        Dashboard, or None if we have nothing to show."""
        return self._forecast_history or None

    def _prune_history(self) -> None:
        """Drop history entries older than the retention window."""
        try:
            tz = ZoneInfo(self.hass.config.time_zone)
        except Exception:  # noqa: BLE001
            tz = timezone.utc
        cutoff = datetime.now(tz) - timedelta(days=FORECAST_HISTORY_RETENTION_DAYS)
        for ts in list(self._forecast_history):
            try:
                if datetime.fromisoformat(ts) < cutoff:
                    del self._forecast_history[ts]
            except (ValueError, TypeError):
                # Unparseable key — drop it rather than let it linger forever.
                del self._forecast_history[ts]

    async def _async_merge_forecast_history(
        self, wh_hours: dict[str, float | int]
    ) -> None:
        """Merge a fresh poll's wh_hours into the retained history (newest wins),
        prune, and persist. Past days the API no longer returns stay frozen at
        their last-known forecast value."""
        await self.async_load_forecast_history()
        if wh_hours:
            self._forecast_history.update(wh_hours)
        self._prune_history()
        if self._history_store is not None:
            try:
                await self._history_store.async_save(
                    {"wh_hours": self._forecast_history}
                )
            except Exception:  # noqa: BLE001 — persistence is best-effort
                _LOGGER.warning("Failed to save forecast history store", exc_info=True)

    async def _async_update_data(self) -> VolcastData:
        """Fetch data from Volcast API with retry (5/15/45s) on transient errors."""
        try:
            data = await self._async_fetch()
        except TransientUpdateFailed as err:
            self.consecutive_failures += 1
            if (self.data is not None
                    and self.consecutive_failures <= TRANSIENT_FAILURE_GRACE_CYCLES):
                _LOGGER.warning(
                    "Volcast update failed (%s); keeping last data (failure %d/%d)",
                    err, self.consecutive_failures, TRANSIENT_FAILURE_GRACE_CYCLES)
                return self.data
            raise
        except UpdateFailed:
            self.consecutive_failures += 1
            raise
        self.consecutive_failures = 0
        self.last_update_success_time = datetime.now(timezone.utc)
        return data

    async def _async_fetch(self) -> VolcastData:
        url = f"{self._api_url}?key={self._api_key}"
        session = async_get_clientsession(self.hass)

        result = await http_with_retry(
            session,
            method="GET",
            url=url,
            headers={},
        )

        if result.success:
            data = _parse_response(result.data, self.hass)
            await self._async_merge_forecast_history(data.wh_hours)
            return data

        # Mapowanie statusów po wyczerpaniu retries — zachowujemy istniejącą semantykę
        if result.status == 401:
            raise UpdateFailed("Invalid API key")
        if result.status == 403:
            return _error_data("Premium required")
        if result.status == 503:
            raise TransientUpdateFailed("Forecast not yet available — cache being populated")
        if result.status == 429:
            raise TransientUpdateFailed(
                f"Rate limit exceeded — retry later (after {result.attempts} attempts)"
            )
        if result.status is not None:
            error_cls = (TransientUpdateFailed
                         if result.status >= 500 or result.status in RETRIABLE_STATUSES
                         else UpdateFailed)
            raise error_cls(
                f"Volcast API error: {result.status} (after {result.attempts} attempts)"
            )
        raise TransientUpdateFailed(
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
        production_source=attrs.get("production_source"),
        production_source_detail=attrs.get("production_source_detail"),
        # kubełki mogą mieć samples: null — przekazujemy bez ingerencji
        calibration=attrs.get("calibration"),
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
