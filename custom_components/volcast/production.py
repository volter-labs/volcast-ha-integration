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
from homeassistant.helpers.storage import Store

from .const import DEFAULT_SUBMIT_URL
from .http_retry import http_with_retry

STORAGE_KEY = "volcast_production_queue"
STORAGE_VERSION = 1
MAX_QUEUE_SIZE = 48

# Backend odrzuca zadania z wieksza liczba odczytow kodem HTTP 400
# (submit-production/index.ts: "Maximum 24 readings per request"), a 400 NIE jest
# w RETRIABLE_STATUSES. Bez tego ograniczenia kolejka, ktora raz przekroczyla 24
# wpisy, zakleszczala sie na stale: kazdy POST dostawal 400, sciezka porazki
# zapisywala te same wpisy z powrotem, a stan przezywal restart HA i naprawe
# backendu. Odblokowanie wymagalo recznego usuniecia pliku Store u uzytkownika.
MAX_READINGS_PER_REQUEST = 24

# Accepted-hours store — pamięć "co już udało się dostarczyć", używana przez
# reconciler żeby nie reposyłał tych samych godzin w nieskończoność.
ACCEPTED_STORAGE_KEY = "volcast_accepted_hours"
ACCEPTED_STORAGE_VERSION = 1
ACCEPTED_RETENTION_DAYS = 7

# Powody odrzucenia readinga przez backend, których NIE warto retry'ować:
# semantycznie permanentne — kolejne wysyłanie zawsze da ten sam rezultat.
# Reconciler powinien traktować je jak "już dostarczone" by nie spamować.
PERMANENT_SKIP_REASONS = frozenset({
    "nighttime_hour",
    "negative_production",
    "exceeds_capacity",
    "invalid_date",
    "invalid_hour",
    "missing_fields",
})

_LOGGER = logging.getLogger(__name__)


def _utcnow_date():
    """Return today's date in UTC. Module-level seam for test monkeypatching."""
    return datetime.now(timezone.utc).date()


@dataclass
class HourBucket:
    """Akumuluje dane produkcji w ramach jednej godziny."""

    hour: int
    energy_start: float | None = None
    energy_latest: float | None = None
    power_readings: list[tuple[float, float]] = field(default_factory=list)  # (timestamp, watts)
    peak_power_w: float = 0.0
    max_soc: float | None = None
    # Battery charge power — do detekcji curtailmentu i ładowania z sieci
    charge_power_sum: float = 0.0
    charge_power_count: int = 0
    charge_power_max: float | None = None


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
        battery_charge_power_entity: str = "",
        system_capacity_kwp: float | None = None,
    ) -> None:
        """Initialize."""
        self._hass = hass
        self._api_key = api_key
        self._submit_url = submit_url or DEFAULT_SUBMIT_URL
        self._energy_entity = energy_entity
        self._power_entity = power_entity
        self._battery_soc_entity = battery_soc_entity
        self._battery_charge_power_entity = battery_charge_power_entity
        self._capacity_kwp = system_capacity_kwp
        self._last_known_soc: float | None = None

        self._current_bucket: HourBucket | None = None
        self._previous_bucket: HourBucket | None = None
        self._last_flushed_hour: int = -1
        self._unsub_state: callback | None = None
        self._unsub_timer: callback | None = None

        # Persystentna kolejka retry — readingi, które nie dotarły do backendu
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._queue: list[dict[str, Any]] = []
        self._queue_loaded: bool = False

        # Persystentna mapa "godziny już zaakceptowane przez backend" — używana
        # przez reconciler, GC po ACCEPTED_RETENTION_DAYS dniach.
        self._accepted_store = Store(hass, ACCEPTED_STORAGE_VERSION, ACCEPTED_STORAGE_KEY)
        self._accepted: dict[str, list[int]] = {}
        self._accepted_loaded: bool = False

        # Stan publiczny (dostępny dla sensorów diagnostycznych)
        self.calibration: dict[str, Any] | None = None
        self.last_submission_time: datetime | None = None
        self.submissions_today: int = 0
        self._last_submission_date: str = ""
        # Diagnostyka ostatniego POSTu (dla sensora, Task 20)
        self._last_submit_status: str = ""
        self._last_submit_attempts: int = 0

    @property
    def queued_count(self) -> int:
        """Liczba zakolejkowanych readingów czekających na retry."""
        return len(self._queue)

    async def _async_load_queue(self) -> None:
        """Załaduj kolejkę retry z persystentnego Store (lazy, raz)."""
        if self._queue_loaded:
            return
        try:
            data = await self._store.async_load()
            if isinstance(data, list):
                self._queue = data
                if self._queue:
                    _LOGGER.info("Loaded %d queued readings from store", len(self._queue))
        except Exception:
            _LOGGER.warning("Failed to load retry queue from storage", exc_info=True)
        self._queue_loaded = True

    async def _async_save_queue(self) -> None:
        """Zapisz kolejkę do Store (lub wyczyść jeśli pusta)."""
        try:
            if self._queue:
                await self._store.async_save(self._queue)
            else:
                await self._store.async_remove()
        except Exception:
            _LOGGER.warning("Failed to persist retry queue to storage", exc_info=True)

    async def _load_accepted_store(self) -> dict[str, list[int]]:
        """Załaduj mapę zaakceptowanych godzin z persystentnego Store (lazy, raz)."""
        if self._accepted_loaded:
            return self._accepted
        try:
            data = await self._accepted_store.async_load()
            if isinstance(data, dict):
                self._accepted = {
                    k: list(v) for k, v in data.items() if isinstance(v, list)
                }
        except Exception:
            _LOGGER.warning("Failed to load accepted-hours store", exc_info=True)
        self._accepted_loaded = True
        return self._accepted

    async def _mark_accepted(self, date_str: str, hour: int) -> None:
        """Oznacz (date, hour) jako dostarczone do backendu i zapisz do Store.

        GC: usuwa wpisy starsze niż ACCEPTED_RETENTION_DAYS (po dacie produkcji).
        Idempotentne — tę samą godzinę można oznaczyć wielokrotnie bez efektu.
        """
        await self._load_accepted_store()
        cutoff = (_utcnow_date() - timedelta(days=ACCEPTED_RETENTION_DAYS)).isoformat()
        # GC pre-existing entries older than retention window. The new mark
        # itself is NOT retention-checked here — it survives one cycle even
        # if its date_str < cutoff (e.g. backfill of stale rejections).
        # The next mark on a different date will GC it. By design.
        self._accepted = {k: v for k, v in self._accepted.items() if k >= cutoff}
        if hour not in self._accepted.setdefault(date_str, []):
            self._accepted[date_str].append(hour)
            self._accepted[date_str].sort()
        try:
            await self._accepted_store.async_save(self._accepted)
        except Exception:
            _LOGGER.warning("Failed to persist accepted-hours store", exc_info=True)

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
        if self._battery_charge_power_entity:
            entities.append(self._battery_charge_power_entity)

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
            "Production tracker started (energy=%s, power=%s, battery_soc=%s, charge_power=%s, submit_url=%s)",
            self._energy_entity or "none",
            self._power_entity or "none",
            self._battery_soc_entity or "none",
            self._battery_charge_power_entity or "none",
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

        if entity_id == self._battery_charge_power_entity:
            # Akumuluj do obliczenia avg i max mocy ładowania
            bucket.charge_power_sum += value
            bucket.charge_power_count += 1
            if bucket.charge_power_max is None or value > bucket.charge_power_max:
                bucket.charge_power_max = value

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

        # Battery charge power — avg i max do detekcji curtailmentu
        if bucket.charge_power_count > 0:
            avg_power = bucket.charge_power_sum / bucket.charge_power_count
            reading["battery_charge_power_avg"] = round(avg_power, 1)
        if bucket.charge_power_max is not None:
            reading["battery_charge_power_max"] = round(bucket.charge_power_max, 1)

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

    async def _async_submit(self, readings: list[dict[str, Any]]) -> bool:
        """Wyślij dane produkcji do backendu. Zwraca True jeśli sukces.

        Łączy zakolejkowane readingi (retry) z bieżącymi i wysyła w jednym POST
        z retry 5/15/45s wewnątrz jednego wywołania (http_with_retry).
        Na sukces:
         - czyści kolejkę,
         - oznacza każdy reading jako "accepted" (poza odrzuceniami z powodów
           NIE-PERMANENT_SKIP_REASONS — te zostaną retry'owane przez reconciler).
        Na fail: zapisuje wszystkie readingi do kolejki (cap MAX_QUEUE_SIZE FIFO).
        """
        await self._async_load_queue()
        await self._load_accepted_store()

        # Połącz zakolejkowane + nowe (dedup po date+hour)
        all_readings = list(self._queue)
        seen = {(r["date"], r["hour"]) for r in all_readings}
        for r in readings:
            key = (r["date"], r["hour"])
            if key not in seen:
                all_readings.append(r)
                seen.add(key)

        # Paczka nie moze przekroczyc limitu backendu. Wysylamy NAJNOWSZE odczyty —
        # sa najbardziej wartosciowe (kalibracja i nowcast dzialaja na dzisiejszych
        # godzinach), a starsze i tak wpadaja w backendowy prog "older_than_24h".
        # Nadmiar czeka w kolejce na kolejny flush zamiast blokowac wszystko.
        if len(all_readings) > MAX_READINGS_PER_REQUEST:
            deferred = all_readings[:-MAX_READINGS_PER_REQUEST]
            batch = all_readings[-MAX_READINGS_PER_REQUEST:]
            _LOGGER.debug(
                "Batch capped at %d readings; %d deferred to next flush",
                len(batch), len(deferred),
            )
        else:
            deferred = []
            batch = all_readings

        session = async_get_clientsession(self._hass)
        result = await http_with_retry(
            session,
            method="POST",
            url=self._submit_url,
            payload={"readings": batch},
            headers={
                "X-API-Key": self._api_key,
                "Content-Type": "application/json",
            },
        )

        self._last_submit_attempts = result.attempts

        if result.success:
            data = result.data or {}
            self.calibration = data.get("calibration")
            self.last_submission_time = datetime.now(timezone.utc)
            self.submissions_today += data.get("accepted", 0)

            # Map rejections by (date, hour) → reason. Readings without a
            # rejection (and those rejected for permanent-skip reasons) are
            # marked accepted to prevent reconciler retry spam.
            rejections = data.get("rejections") or []
            rejected_keys: dict[tuple[str, int], str] = {}
            for rej in rejections:
                try:
                    rejected_keys[(rej["date"], rej["hour"])] = rej.get("reason", "")
                except (KeyError, TypeError):
                    continue

            for r in all_readings:
                key = (r["date"], r["hour"])
                reason = rejected_keys.get(key)
                if reason is None or reason in PERMANENT_SKIP_REASONS:
                    await self._mark_accepted(r["date"], r["hour"])

            # Sukces czysci tylko to, co faktycznie poszlo. Nadmiar ponad limit
            # paczki czeka na kolejny flush — wyczyszczenie calej kolejki gubiloby
            # odczyty, ktorych backend nigdy nie widzial.
            self._queue = deferred
            await self._async_save_queue()
            self._last_submit_status = "ok"
            if result.attempts > 1:
                _LOGGER.info(
                    "Production submitted after %d attempts: accepted=%s rejected=%s",
                    result.attempts,
                    data.get("accepted", 0),
                    data.get("rejected", 0),
                )
            else:
                _LOGGER.info(
                    "Production submitted: accepted=%s rejected=%s calibration=%s",
                    data.get("accepted", 0),
                    data.get("rejected", 0),
                    self.calibration,
                )
            return True

        # Failure path — zapisz wszystkie readingi do kolejki retry (FIFO cap)
        self._queue = (deferred + batch)[-MAX_QUEUE_SIZE:]
        await self._async_save_queue()
        self._last_submit_status = result.last_error or f"HTTP {result.status}"
        _LOGGER.warning(
            "Production submit failed after %d attempts (%s); %d readings queued",
            result.attempts,
            result.last_error or f"HTTP {result.status}",
            len(self._queue),
        )
        return False
