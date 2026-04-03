"""Production tracker for Volcast — submits hourly PV production to backend."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)

from .const import DEFAULT_SUBMIT_URL

_LOGGER = logging.getLogger(__name__)


@dataclass
class HourBucket:
    """Akumuluje dane produkcji w ramach jednej godziny."""

    hour: int
    energy_start: float | None = None
    energy_latest: float | None = None
    power_readings: list[tuple[float, float]] = field(default_factory=list)  # (timestamp, watts)
    peak_power_w: float = 0.0
    max_soc: float | None = None


class VolcastProductionTracker:
    """Śledzi state changes na sensorach mocy/energii i co godzinę wysyła do backendu."""

    def __init__(
        self,
        hass: HomeAssistant,
        api_key: str,
        submit_url: str,
        energy_entity: str,
        power_entity: str,
        battery_soc_entity: str = "",
        system_capacity_kwp: float | None = None,
    ) -> None:
        """Initialize."""
        self._hass = hass
        self._api_key = api_key
        self._submit_url = submit_url or DEFAULT_SUBMIT_URL
        self._energy_entity = energy_entity
        self._power_entity = power_entity
        self._battery_soc_entity = battery_soc_entity
        self._capacity_kwp = system_capacity_kwp
        self._last_known_soc: float | None = None

        self._current_bucket: HourBucket | None = None
        self._previous_bucket: HourBucket | None = None
        self._last_flushed_hour: int = -1
        self._unsub_state: callback | None = None
        self._unsub_timer: callback | None = None

        # Stan publiczny (dostępny dla sensorów diagnostycznych)
        self.calibration: dict[str, Any] | None = None
        self.last_submission_time: datetime | None = None
        self.submissions_today: int = 0
        self._last_submission_date: str = ""

    def _get_local_now(self) -> datetime:
        """Zwróć bieżący czas w strefie czasowej HA."""
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(self._hass.config.time_zone)
        except Exception:
            tz = timezone.utc
        return datetime.now(tz)

    async def async_start(self) -> None:
        """Uruchom nasłuchiwanie na state changes i timer godzinowy."""
        entities: list[str] = []
        if self._energy_entity:
            entities.append(self._energy_entity)
        if self._power_entity:
            entities.append(self._power_entity)
        if self._battery_soc_entity:
            entities.append(self._battery_soc_entity)

        if not entities:
            _LOGGER.warning("No production entities configured — tracker idle")
            return

        self._unsub_state = async_track_state_change_event(
            self._hass, entities, self._async_state_changed
        )

        # Timer co 5 minut — flush o :05 każdej godziny
        self._unsub_timer = async_track_time_interval(
            self._hass, self._async_check_flush, timedelta(minutes=5)
        )

        _LOGGER.info(
            "Production tracker started (energy=%s, power=%s, battery_soc=%s, submit_url=%s)",
            self._energy_entity or "none",
            self._power_entity or "none",
            self._battery_soc_entity or "none",
            self._submit_url,
        )

    async def async_stop(self) -> None:
        """Zatrzymaj nasłuchiwanie."""
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None
        _LOGGER.info("Production tracker stopped")

    @callback
    def _async_state_changed(self, event: Event) -> None:
        """Obsłuż zmianę stanu sensora."""
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return

        try:
            value = float(new_state.state)
        except (ValueError, TypeError):
            return

        now = self._get_local_now()
        current_hour = now.hour
        entity_id = event.data.get("entity_id", "")

        # Inicjalizuj bucket jeśli brak lub zmiana godziny
        if self._current_bucket is None or self._current_bucket.hour != current_hour:
            # Zachowaj poprzedni bucket do flushu
            if self._current_bucket is not None and self._current_bucket.hour != current_hour:
                self._previous_bucket = self._current_bucket
            self._current_bucket = HourBucket(hour=current_hour)
            # Przenieś ostatni odczyt energii z poprzedniego bucketa jako start nowego
            # (eliminuje lukę między ostatnim odczytem starej godziny a pierwszym nowej)
            if self._previous_bucket is not None and self._previous_bucket.energy_latest is not None:
                self._current_bucket.energy_start = self._previous_bucket.energy_latest

        bucket = self._current_bucket

        if entity_id == self._energy_entity:
            if bucket.energy_start is None:
                bucket.energy_start = value
            bucket.energy_latest = value

        if entity_id == self._power_entity:
            bucket.power_readings.append((now.timestamp(), value))
            if value > bucket.peak_power_w:
                bucket.peak_power_w = value

        if entity_id == self._battery_soc_entity:
            if bucket.max_soc is None or value > bucket.max_soc:
                bucket.max_soc = value
            self._last_known_soc = value

    async def _async_check_flush(self, _now: datetime) -> None:
        """Co 5 minut sprawdź, czy trzeba wysłać dane z poprzedniej godziny."""
        now = self._get_local_now()
        current_hour = now.hour

        # Flush raz na godzinę po :05 — flag-based (odporny na timer drift)
        if now.minute < 5:
            return

        prev_hour = (current_hour - 1) % 24
        if prev_hour == self._last_flushed_hour:
            return  # Już wysłano w tej godzinie

        # Znajdź bucket do flushu — bieżący (jeśli z prev_hour) lub zachowany previous
        bucket: HourBucket | None = None
        if self._current_bucket is not None and self._current_bucket.hour == prev_hour:
            bucket = self._current_bucket
            self._current_bucket = HourBucket(hour=current_hour)
        elif self._previous_bucket is not None and self._previous_bucket.hour == prev_hour:
            bucket = self._previous_bucket

        self._previous_bucket = None
        self._last_flushed_hour = prev_hour

        if bucket is None:
            return

        # Oblicz actual_kwh
        actual_kwh, data_method = self._compute_energy(bucket)

        if actual_kwh is None or actual_kwh < 0:
            return

        # Resetuj counter jeśli nowy dzień
        today_str = now.strftime("%Y-%m-%d")
        if today_str != self._last_submission_date:
            self.submissions_today = 0
            self._last_submission_date = today_str

        # Określ datę produkcji (jeśli prev_hour=23 a teraz=0, to wczorajsza data)
        if prev_hour == 23 and current_hour == 0:
            production_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            production_date = today_str

        reading: dict[str, Any] = {
            "date": production_date,
            "hour": prev_hour,
            "actual_kwh": round(actual_kwh, 4),
            "data_method": data_method,
        }

        if bucket.peak_power_w > 0:
            reading["peak_power_w"] = round(bucket.peak_power_w, 1)

        # Użyj max_soc z bucketa, fallback na ostatnią znaną wartość
        # (sensory Modbus mogą mieć krótkie przerwy — SoC nie zmienia się gwałtownie)
        soc_value = bucket.max_soc if bucket.max_soc is not None else self._last_known_soc
        if soc_value is not None:
            reading["battery_soc"] = round(soc_value, 1)

        await self._async_submit([reading])

    def _compute_energy(self, bucket: HourBucket) -> tuple[float | None, str]:
        """Oblicz energię z danych w bucket. Zwraca (kwh, method)."""
        # Metoda 1: Energy delta (preferowana)
        if bucket.energy_start is not None and bucket.energy_latest is not None:
            delta = bucket.energy_latest - bucket.energy_start

            # Reset detection (licznik wyzerowany)
            if delta < 0:
                _LOGGER.debug("Energy counter reset detected (delta=%s), fallback to power", delta)
            else:
                # Capacity glitch detection
                if self._capacity_kwp and delta > self._capacity_kwp * 1.2:
                    _LOGGER.warning(
                        "Energy delta %s kWh exceeds capacity %s kWp × 1.2, skipping",
                        delta, self._capacity_kwp,
                    )
                    return None, "energy_delta"
                return delta, "energy_delta"

        # Metoda 2: Power trapezoidal (fallback)
        if len(bucket.power_readings) >= 2:
            total_wh = 0.0
            readings = sorted(bucket.power_readings, key=lambda x: x[0])
            for i in range(len(readings) - 1):
                t0, p0 = readings[i]
                t1, p1 = readings[i + 1]
                dt_hours = (t1 - t0) / 3600.0
                avg_power_w = (p0 + p1) / 2.0
                total_wh += avg_power_w * dt_hours
            kwh = total_wh / 1000.0
            return kwh, "power_average"

        return None, ""

    async def _async_submit(self, readings: list[dict[str, Any]]) -> None:
        """Wyślij dane produkcji do backendu."""
        try:
            session = async_get_clientsession(self._hass)
            async with session.post(
                self._submit_url,
                json={"readings": readings},
                headers={
                    "X-API-Key": self._api_key,
                    "Content-Type": "application/json",
                },
                timeout=15,
            ) as resp:
                if resp.ok:
                    data = await resp.json()
                    self.calibration = data.get("calibration")
                    self.last_submission_time = datetime.now(timezone.utc)
                    self.submissions_today += data.get("accepted", 0)
                    _LOGGER.info(
                        "Production submitted: accepted=%s rejected=%s calibration=%s",
                        data.get("accepted", 0),
                        data.get("rejected", 0),
                        self.calibration,
                    )
                elif resp.status == 429:
                    _LOGGER.warning("Production submit rate limited (429)")
                else:
                    text = await resp.text()
                    _LOGGER.error("Production submit failed (%s): %s", resp.status, text)

        except Exception:
            _LOGGER.exception("Error during production submit")
