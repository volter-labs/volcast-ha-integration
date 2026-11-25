"""Tests for production tracker retry queue."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import FakeHass, _FakeStore

from custom_components.volcast.production import VolcastProductionTracker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tracker(
    hass: Any | None = None,
    store: _FakeStore | None = None,
    accepted_store: _FakeStore | None = None,
    baseline_store: _FakeStore | None = None,
) -> VolcastProductionTracker:
    """Create a tracker with sensible defaults and injectable Stores."""
    if hass is None:
        hass = FakeHass()
    tracker = VolcastProductionTracker(
        hass=hass,
        api_key="test-key",
        submit_url="https://example.com/api/submit-production",
        energy_entity="sensor.pv_energy",
        power_entity="sensor.pv_power",
        system_capacity_kwp=6.0,
    )
    # Wstrzyknij fake Stores jeśli podane
    if store is not None:
        tracker._store = store
    if accepted_store is not None:
        tracker._accepted_store = accepted_store
    if baseline_store is not None:
        tracker._baseline_store = baseline_store
    return tracker


def _make_reading(date: str = "2026-04-09", hour: int = 11, kwh: float = 3.5) -> dict:
    """Create a minimal production reading dict."""
    return {
        "date": date,
        "hour": hour,
        "actual_kwh": kwh,
        "data_method": "energy_delta",
    }


def _mock_session_ok(accepted: int = 1, rejected: int = 0, calibration: dict | None = None):
    """Return a mock aiohttp session whose POST returns 200 OK.

    Used by the success-path tests that exercise the real http_with_retry
    against a mocked session (verifies merge / payload shape end-to-end).
    """
    resp = AsyncMock()
    resp.ok = True
    resp.status = 200
    resp.json = AsyncMock(return_value={
        "accepted": accepted,
        "rejected": rejected,
        "calibration": calibration,
    })

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.post = MagicMock(return_value=ctx)
    return session


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRetryQueue:
    """Tests for the persistent retry queue in production tracker."""

    @pytest.mark.asyncio
    async def test_successful_submit_clears_queue(self):
        """Po udanym POST, kolejka powinna być pusta."""
        store = _FakeStore()
        # Pre-populate queue z jednym starym readingiem
        await store.async_save([_make_reading(hour=10, kwh=2.0)])

        tracker = _make_tracker(store=store)
        session = _mock_session_ok(accepted=2)

        with patch(
            "custom_components.volcast.production.async_get_clientsession",
            return_value=session,
        ):
            readings = [_make_reading(hour=11, kwh=3.5)]
            success = await tracker._async_submit(readings)

        assert success is True
        # Kolejka powinna być pusta
        assert tracker._queue == []
        saved = await store.async_load()
        assert saved is None  # Store wyczyszczony

    @pytest.mark.asyncio
    async def test_failed_submit_queues_reading(self):
        """Gdy http_with_retry zwróci network failure, reading powinien trafić do kolejki."""
        from custom_components.volcast.http_retry import RetryResult

        store = _FakeStore()
        tracker = _make_tracker(store=store)

        result = RetryResult(
            success=False, status=None, attempts=4,
            last_error="network: DNS timeout", retriable=True,
        )
        with patch(
            "custom_components.volcast.production.http_with_retry",
            return_value=result,
        ), patch(
            "custom_components.volcast.production.async_get_clientsession",
            return_value=MagicMock(),
        ):
            readings = [_make_reading(hour=11, kwh=3.5)]
            success = await tracker._async_submit(readings)

        assert success is False
        # Reading powinien być w kolejce
        assert len(tracker._queue) == 1
        assert tracker._queue[0]["hour"] == 11
        # I zapisany w Store
        saved = await store.async_load()
        assert len(saved) == 1

    @pytest.mark.asyncio
    async def test_queued_readings_sent_with_next_flush(self):
        """Zakolejkowane readingi powinny być wysłane razem z nowym (merge w submit)."""
        store = _FakeStore()
        # Pre-populate queue z jednym starym readingiem
        queued = [_make_reading(hour=10, kwh=2.0)]
        await store.async_save(queued)

        tracker = _make_tracker(store=store)

        captured_payload = {}
        ok_session = _mock_session_ok(accepted=2)

        original_post = ok_session.post
        def capture_post(url, **kwargs):
            captured_payload.update(kwargs.get("json", {}))
            return original_post(url, **kwargs)

        ok_session.post = capture_post

        with patch(
            "custom_components.volcast.production.async_get_clientsession",
            return_value=ok_session,
        ):
            # Submit TYLKO nowy reading — submit powinien sam załadować kolejkę i połączyć
            new_reading = _make_reading(hour=11, kwh=3.5)
            success = await tracker._async_submit([new_reading])

        assert success is True
        # Payload powinien mieć 2 readingi (1 z kolejki + 1 nowy)
        assert len(captured_payload["readings"]) == 2
        hours = [r["hour"] for r in captured_payload["readings"]]
        assert 10 in hours
        assert 11 in hours

    @pytest.mark.asyncio
    async def test_queue_max_48_fifo(self):
        """Kolejka nie powinna przekroczyć 48 wpisów — najstarsze usuwane."""
        from custom_components.volcast.http_retry import RetryResult

        store = _FakeStore()
        # Pre-populate z 48 wpisami (unikalne date+hour combo)
        full_queue = [
            _make_reading(date=f"2026-04-{(i // 24 + 1):02d}", hour=i % 24, kwh=float(i))
            for i in range(48)
        ]
        await store.async_save(full_queue)

        tracker = _make_tracker(store=store)
        await tracker._async_load_queue()

        result = RetryResult(
            success=False, status=None, attempts=4,
            last_error="network: DNS timeout", retriable=True,
        )
        with patch(
            "custom_components.volcast.production.http_with_retry",
            return_value=result,
        ), patch(
            "custom_components.volcast.production.async_get_clientsession",
            return_value=MagicMock(),
        ):
            # Próba wysłania nowego readinga (unikalna data) — fail → dodaje do kolejki
            new_reading = _make_reading(date="2026-04-10", hour=12, kwh=99.0)
            await tracker._async_submit([new_reading])

        # Kolejka max 48 — najstarszy usunięty, nowy na końcu
        assert len(tracker._queue) == 48
        assert tracker._queue[-1]["actual_kwh"] == 99.0
        # Pierwszy z oryginalnych (kwh=0.0) powinien być usunięty
        assert tracker._queue[0]["actual_kwh"] != 0.0

    @pytest.mark.asyncio
    async def test_rate_limit_429_queues_reading(self):
        """429 rate limit (po wyczerpaniu retry) powinien kolejkować reading, nie tracić go."""
        from custom_components.volcast.http_retry import RetryResult

        store = _FakeStore()
        tracker = _make_tracker(store=store)

        # 429 jest retriable, zakładamy że po 4 próbach nadal trzyma rate limit
        result = RetryResult(
            success=False, status=429, attempts=4,
            last_error="HTTP 429", retriable=True,
        )
        with patch(
            "custom_components.volcast.production.http_with_retry",
            return_value=result,
        ), patch(
            "custom_components.volcast.production.async_get_clientsession",
            return_value=MagicMock(),
        ):
            readings = [_make_reading(hour=11, kwh=3.5)]
            success = await tracker._async_submit(readings)

        assert success is False
        assert len(tracker._queue) == 1

    @pytest.mark.asyncio
    async def test_queue_persists_across_tracker_restart(self):
        """Kolejka powinna przetrwać restart trackera (Store persystencja)."""
        from custom_components.volcast.http_retry import RetryResult

        store = _FakeStore()

        # Tracker 1: fail → queue reading
        tracker1 = _make_tracker(store=store)

        result = RetryResult(
            success=False, status=None, attempts=4,
            last_error="network: DNS timeout", retriable=True,
        )
        with patch(
            "custom_components.volcast.production.http_with_retry",
            return_value=result,
        ), patch(
            "custom_components.volcast.production.async_get_clientsession",
            return_value=MagicMock(),
        ):
            await tracker1._async_submit([_make_reading(hour=11, kwh=3.5)])

        assert len(tracker1._queue) == 1

        # Tracker 2 (restart) — powinien załadować kolejkę z Store
        tracker2 = _make_tracker(store=store)
        await tracker2._async_load_queue()
        assert len(tracker2._queue) == 1
        assert tracker2._queue[0]["hour"] == 11

    @pytest.mark.asyncio
    async def test_queued_count_property(self):
        """queued_count powinien zwracać liczbę zakolejkowanych readingów."""
        store = _FakeStore()
        tracker = _make_tracker(store=store)
        assert tracker.queued_count == 0

        tracker._queue = [_make_reading(), _make_reading()]
        assert tracker.queued_count == 2


# ---------------------------------------------------------------------------
# Accepted-hours store + _mark_accepted (Task 14)
# ---------------------------------------------------------------------------


class TestAcceptedHoursStore:
    """Tests for the persistent accepted-hours Store and _mark_accepted."""

    @pytest.mark.asyncio
    async def test_mark_accepted_persists_to_store(self, monkeypatch):
        """_mark_accepted writes (date, hour) into the in-memory dict and Store."""
        from datetime import datetime, timezone
        from custom_components.volcast import production as production_mod

        # Freeze "today" close to the hard-coded dates so the 7-day retention
        # GC in _mark_accepted does not wipe them on subsequent calls.
        fixed_now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(production_mod, "_utcnow_date", lambda: fixed_now.date())

        accepted_store = _FakeStore()
        tracker = _make_tracker(accepted_store=accepted_store)

        await tracker._mark_accepted("2026-05-10", 12)
        await tracker._mark_accepted("2026-05-10", 13)
        await tracker._mark_accepted("2026-05-09", 23)

        # Verify in-memory state
        assert 12 in tracker._accepted.get("2026-05-10", [])
        assert 13 in tracker._accepted.get("2026-05-10", [])
        assert 23 in tracker._accepted.get("2026-05-09", [])

        # Verify persisted state (fresh tracker reads same Store)
        tracker2 = _make_tracker(accepted_store=accepted_store)
        accepted = await tracker2._load_accepted_store()
        assert 12 in accepted.get("2026-05-10", [])
        assert 13 in accepted.get("2026-05-10", [])
        assert 23 in accepted.get("2026-05-09", [])

    @pytest.mark.asyncio
    async def test_mark_accepted_dedup(self):
        """Marking same (date, hour) twice is idempotent."""
        accepted_store = _FakeStore()
        tracker = _make_tracker(accepted_store=accepted_store)

        await tracker._mark_accepted("2026-05-10", 12)
        await tracker._mark_accepted("2026-05-10", 12)
        await tracker._mark_accepted("2026-05-10", 12)

        accepted = await tracker._load_accepted_store()
        assert accepted["2026-05-10"].count(12) == 1

    @pytest.mark.asyncio
    async def test_mark_accepted_gc_after_7_days(self, monkeypatch):
        """Entries older than ACCEPTED_RETENTION_DAYS dropped on next mark."""
        from datetime import datetime, timezone
        from custom_components.volcast import production as production_mod

        # Fixed "today" = 2026-05-17 (>7 days after 2026-05-09)
        fixed_now = datetime(2026, 5, 17, 0, 30, tzinfo=timezone.utc)
        monkeypatch.setattr(
            production_mod, "_utcnow_date", lambda: fixed_now.date()
        )

        accepted_store = _FakeStore()
        tracker = _make_tracker(accepted_store=accepted_store)

        # Pre-populate without GC (bypass _mark_accepted to inject old data)
        tracker._accepted = {"2026-05-09": [12], "2026-05-15": [12]}
        tracker._accepted_loaded = True  # don't overwrite from store on load

        await tracker._mark_accepted("2026-05-17", 12)

        # 2026-05-09 is 8 days before 2026-05-17 → cutoff = 2026-05-10 → drop
        assert "2026-05-09" not in tracker._accepted
        # 2026-05-15 is 2 days before → keep
        assert "2026-05-15" in tracker._accepted
        assert "2026-05-17" in tracker._accepted


# ---------------------------------------------------------------------------
# _async_submit refactored to delegate to http_with_retry (Task 15)
# ---------------------------------------------------------------------------


def _patch_http_with_retry(retry_result):
    """Helper: patch production.http_with_retry to return a fixed RetryResult."""
    return patch(
        "custom_components.volcast.production.http_with_retry",
        return_value=retry_result,
    )


class TestSubmitWithHttpRetry:
    """Tests for the http_with_retry-driven _async_submit path."""

    @pytest.mark.asyncio
    async def test_submit_intra_flush_retry_success(self):
        """http_with_retry returns success after attempts=2 — queue clears, hour marked accepted."""
        from custom_components.volcast.http_retry import RetryResult

        store = _FakeStore()
        accepted_store = _FakeStore()
        tracker = _make_tracker(store=store, accepted_store=accepted_store)

        result = RetryResult(
            success=True, status=200, attempts=2,
            data={"accepted": 1, "rejected": 0, "rejections": []},
        )
        with _patch_http_with_retry(result), patch(
            "custom_components.volcast.production.async_get_clientsession",
            return_value=MagicMock(),
        ):
            ok = await tracker._async_submit(
                [{"date": "2026-05-10", "hour": 12, "actual_kwh": 1.5}]
            )

        assert ok is True
        assert tracker._queue == []
        assert 12 in tracker._accepted.get("2026-05-10", [])
        # Diagnostic state populated (consumed by Task 20 sensors)
        assert tracker._last_submit_status == "ok"
        assert tracker._last_submit_attempts == 2

    @pytest.mark.asyncio
    async def test_submit_exhaustion_queues_readings(self):
        """http_with_retry returns failure with status=502 (retriable, exhausted) — readings queued."""
        from custom_components.volcast.http_retry import RetryResult

        store = _FakeStore()
        accepted_store = _FakeStore()
        tracker = _make_tracker(store=store, accepted_store=accepted_store)

        result = RetryResult(
            success=False, status=502, attempts=4,
            last_error="HTTP 502", retriable=True,
        )
        with _patch_http_with_retry(result), patch(
            "custom_components.volcast.production.async_get_clientsession",
            return_value=MagicMock(),
        ):
            ok = await tracker._async_submit(
                [{"date": "2026-05-10", "hour": 12, "actual_kwh": 1.5}]
            )

        assert ok is False
        assert len(tracker._queue) == 1
        assert tracker._queue[0]["hour"] == 12
        # Hour should NOT be marked accepted on failure
        assert 12 not in tracker._accepted.get("2026-05-10", [])
        # Diagnostic state populated even on failure (consumed by Task 20 sensors)
        assert "502" in (tracker._last_submit_status or "")
        assert tracker._last_submit_attempts == 4

    @pytest.mark.asyncio
    async def test_submit_non_retriable_still_queues(self):
        """401 from http_with_retry → readings still queued (in case API key fixed later)."""
        from custom_components.volcast.http_retry import RetryResult

        store = _FakeStore()
        accepted_store = _FakeStore()
        tracker = _make_tracker(store=store, accepted_store=accepted_store)

        result = RetryResult(
            success=False, status=401, attempts=1,
            last_error="non-retriable HTTP 401", retriable=False,
        )
        with _patch_http_with_retry(result), patch(
            "custom_components.volcast.production.async_get_clientsession",
            return_value=MagicMock(),
        ):
            ok = await tracker._async_submit(
                [{"date": "2026-05-10", "hour": 12, "actual_kwh": 1.5}]
            )

        assert ok is False
        # Reading preserved for retry once API key issue is addressed
        assert len(tracker._queue) == 1

    @pytest.mark.asyncio
    async def test_permanent_skip_rejection_marks_accepted(self):
        """Backend returns 'nighttime_hour' rejection → mark accepted to stop reconciler retries."""
        from custom_components.volcast.http_retry import RetryResult

        store = _FakeStore()
        accepted_store = _FakeStore()
        tracker = _make_tracker(store=store, accepted_store=accepted_store)

        result = RetryResult(
            success=True, status=200, attempts=1,
            data={
                "accepted": 0,
                "rejected": 1,
                "rejections": [
                    {"date": "2026-05-10", "hour": 22, "reason": "nighttime_hour"}
                ],
            },
        )
        with _patch_http_with_retry(result), patch(
            "custom_components.volcast.production.async_get_clientsession",
            return_value=MagicMock(),
        ):
            await tracker._async_submit(
                [{"date": "2026-05-10", "hour": 22, "actual_kwh": 0.1}]
            )

        assert 22 in tracker._accepted.get("2026-05-10", []), (
            "permanent-skip rejections must be marked to prevent reconciler retries"
        )


class TestGapCoveredHours:
    """Odczyt z `covers_from` oznacza jako dostarczone cala objeta rozpietosc godzin."""

    @staticmethod
    def _span_reading(date: str, hour: int, from_date: str, from_hour: int) -> dict:
        return {
            "date": date,
            "hour": hour,
            "actual_kwh": 4.0,
            "covers_from": {"date": from_date, "hour": from_hour},
        }

    @pytest.mark.asyncio
    async def test_accepted_span_marks_every_covered_hour(self):
        from custom_components.volcast.http_retry import RetryResult
        from custom_components.volcast.production import _utcnow_date

        today = _utcnow_date().isoformat()
        tracker = _make_tracker(store=_FakeStore(), accepted_store=_FakeStore())
        result = RetryResult(
            success=True, status=200, attempts=1,
            data={"accepted": 1, "rejected": 0, "rejections": []},
        )
        with _patch_http_with_retry(result), patch(
            "custom_components.volcast.production.async_get_clientsession",
            return_value=MagicMock(),
        ):
            await tracker._async_submit([self._span_reading(today, 13, today, 10)])

        assert tracker._accepted[today] == [10, 11, 12, 13]
        # przetrwa restart (ten sam Store)
        tracker2 = _make_tracker(accepted_store=tracker._accepted_store)
        assert (await tracker2._load_accepted_store())[today] == [10, 11, 12, 13]

    @pytest.mark.asyncio
    async def test_span_crossing_midnight_uses_right_date_per_hour(self):
        from custom_components.volcast.http_retry import RetryResult
        from custom_components.volcast.production import _utcnow_date

        today = _utcnow_date()
        yesterday = (today - timedelta(days=1)).isoformat()
        today = today.isoformat()
        tracker = _make_tracker(store=_FakeStore(), accepted_store=_FakeStore())
        result = RetryResult(
            success=True, status=200, attempts=1,
            data={"accepted": 1, "rejected": 0, "rejections": []},
        )
        with _patch_http_with_retry(result), patch(
            "custom_components.volcast.production.async_get_clientsession",
            return_value=MagicMock(),
        ):
            await tracker._async_submit([self._span_reading(today, 1, yesterday, 22)])

        assert tracker._accepted[yesterday] == [22, 23]
        assert tracker._accepted[today] == [0, 1]

    @pytest.mark.asyncio
    async def test_rejected_span_marks_nothing(self):
        from custom_components.volcast.http_retry import RetryResult
        from custom_components.volcast.production import _utcnow_date

        today = _utcnow_date().isoformat()
        tracker = _make_tracker(store=_FakeStore(), accepted_store=_FakeStore())
        result = RetryResult(
            success=True, status=200, attempts=1,
            data={
                "accepted": 0,
                "rejected": 1,
                "rejections": [{"date": today, "hour": 13, "reason": "invalid_value"}],
            },
        )
        with _patch_http_with_retry(result), patch(
            "custom_components.volcast.production.async_get_clientsession",
            return_value=MagicMock(),
        ):
            await tracker._async_submit([self._span_reading(today, 13, today, 10)])

        assert tracker._accepted.get(today, []) == []

    @pytest.mark.asyncio
    async def test_failed_send_marks_nothing(self):
        from custom_components.volcast.http_retry import RetryResult
        from custom_components.volcast.production import _utcnow_date

        today = _utcnow_date().isoformat()
        tracker = _make_tracker(store=_FakeStore(), accepted_store=_FakeStore())
        result = RetryResult(success=False, status=500, attempts=3, data=None)
        with _patch_http_with_retry(result), patch(
            "custom_components.volcast.production.async_get_clientsession",
            return_value=MagicMock(),
        ):
            ok = await tracker._async_submit([self._span_reading(today, 13, today, 10)])

        assert ok is False
        assert tracker._accepted.get(today, []) == []


class TestBatchCap:
    """Backend odrzuca >24 odczyty kodem 400, a 400 nie jest retriable.

    Bez ograniczenia paczki kolejka rosnąca powyżej 24 wpisów zakleszczała się na
    stałe: każdy POST dostawał 400, ścieżka porażki zapisywała te same wpisy z
    powrotem, a stan przeżywał restart HA i naprawę backendu. Wymagało to ręcznego
    usunięcia pliku Store u każdego dotkniętego użytkownika.
    """

    @pytest.mark.asyncio
    async def test_payload_nigdy_nie_przekracza_limitu_backendu(self):
        """Nawet przy pełnej kolejce POST niesie najwyżej MAX_READINGS_PER_REQUEST."""
        from custom_components.volcast.production import MAX_READINGS_PER_REQUEST

        store = _FakeStore()
        full_queue = [
            _make_reading(date=f"2026-04-{(i // 24 + 1):02d}", hour=i % 24, kwh=float(i))
            for i in range(48)
        ]
        await store.async_save(full_queue)
        tracker = _make_tracker(store=store)
        await tracker._async_load_queue()

        captured = {}

        async def _capture(session, **kwargs):
            from custom_components.volcast.http_retry import RetryResult
            captured["n"] = len(kwargs["payload"]["readings"])
            return RetryResult(success=True, status=200, attempts=1,
                               last_error=None, retriable=False,
                               data={"accepted": captured["n"], "rejected": 0})

        with patch(
            "custom_components.volcast.production.http_with_retry", new=_capture
        ), patch(
            "custom_components.volcast.production.async_get_clientsession",
            return_value=MagicMock(),
        ):
            await tracker._async_submit([_make_reading(date="2026-04-10", hour=12, kwh=99.0)])

        assert captured["n"] <= MAX_READINGS_PER_REQUEST, (
            f"paczka {captured['n']} > limit backendu {MAX_READINGS_PER_REQUEST} — "
            "to jest dokladnie warunek zakleszczenia"
        )

    @pytest.mark.asyncio
    async def test_niewyslana_reszta_zostaje_w_kolejce_po_sukcesie(self):
        """Sukces czyści tylko to, co poszło — nadmiar czeka na następny flush."""
        store = _FakeStore()
        full_queue = [
            _make_reading(date=f"2026-04-{(i // 24 + 1):02d}", hour=i % 24, kwh=float(i))
            for i in range(40)
        ]
        await store.async_save(full_queue)
        tracker = _make_tracker(store=store)
        await tracker._async_load_queue()

        async def _ok(session, **kwargs):
            from custom_components.volcast.http_retry import RetryResult
            n = len(kwargs["payload"]["readings"])
            return RetryResult(success=True, status=200, attempts=1,
                               last_error=None, retriable=False,
                               data={"accepted": n, "rejected": 0})

        with patch(
            "custom_components.volcast.production.http_with_retry", new=_ok
        ), patch(
            "custom_components.volcast.production.async_get_clientsession",
            return_value=MagicMock(),
        ):
            await tracker._async_submit([])

        # 40 w kolejce, 24 poszlo → 16 zostaje, nie 0 i nie 40.
        assert len(tracker._queue) == 16, (
            f"oczekiwano 16 odlozonych, jest {len(tracker._queue)}"
        )


# ---------------------------------------------------------------------------
# Utrata energii przy restarcie trackera (zgloszenie andras.m.vass, 2026-08-20)
# ---------------------------------------------------------------------------

class TestRestartEnergyBaseline:
    """Po restarcie tracker musi znac stan licznika z POCZATKU biezacej godziny.

    Bez tego `energy_start` ustawia sie na pierwszy odczyt PO restarcie i cala
    energia od poczatku godziny do restartu przepada. Na prodzie 2026-08-20
    dalo to 3.7 kWh zamiast 7.7 dla godziny 13 i dokladne 0.0000 dla godziny 9.
    Sygnatura kohortowa: godziny z restartem maja 2.5x wyzszy odsetek zanizen
    (13.9% vs 5.5% na czystych godzinach), a godzina NASTEPNA jest normalna —
    czyli energia jest tracona, nie przesuwana.
    """

    @pytest.mark.asyncio
    async def test_baseline_survives_restart_mid_hour(self, monkeypatch):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/Warsaw")
        clock = {"t": datetime(2026, 8, 20, 12, 59, tzinfo=tz)}

        def _ev(entity_id: str, value: float):
            st = MagicMock()
            st.state = str(value)
            ev = MagicMock()
            ev.data = {"new_state": st, "entity_id": entity_id}
            return ev

        baseline_store = _FakeStore()

        # --- instancja przed restartem ---
        a = _make_tracker(baseline_store=baseline_store)
        a._capacity_kwp = 10.34  # instalacja ze zgloszenia — 7.7 kWh/h musi przejsc
        monkeypatch.setattr(a, "_get_local_now", lambda: clock["t"])
        a._async_submit = AsyncMock(return_value=True)
        await a.async_start()

        a._async_state_changed(_ev("sensor.pv_energy", 100.0))     # 12:59, licznik 100.0

        clock["t"] = datetime(2026, 8, 20, 13, 0, 10, tzinfo=tz)
        a._async_state_changed(_ev("sensor.pv_energy", 100.1))     # rollover -> godzina 13

        clock["t"] = datetime(2026, 8, 20, 13, 5, tzinfo=tz)
        await a._async_check_flush(None)                            # tick: utrwal baseline godziny 13

        # --- RESTART: nowa instancja, ten sam Store, nic w pamieci ---
        b = _make_tracker(baseline_store=baseline_store)
        b._capacity_kwp = 10.34
        monkeypatch.setattr(b, "_get_local_now", lambda: clock["t"])
        submitted: list[list[dict]] = []
        b._async_submit = AsyncMock(side_effect=lambda r: submitted.append(r) or True)
        await b.async_start()

        clock["t"] = datetime(2026, 8, 20, 13, 30, tzinfo=tz)
        b._async_state_changed(_ev("sensor.pv_energy", 104.0))      # pierwszy odczyt po restarcie

        clock["t"] = datetime(2026, 8, 20, 13, 59, tzinfo=tz)
        b._async_state_changed(_ev("sensor.pv_energy", 107.7))      # koniec godziny 13

        clock["t"] = datetime(2026, 8, 20, 14, 0, 20, tzinfo=tz)
        b._async_state_changed(_ev("sensor.pv_energy", 107.8))      # rollover -> godzina 14

        clock["t"] = datetime(2026, 8, 20, 14, 5, tzinfo=tz)
        await b._async_check_flush(None)                            # flush godziny 13

        assert submitted, "godzina 13 powinna zostac wyslana"
        reading = submitted[-1][0]
        assert reading["hour"] == 13
        # 107.7 - 100.0 = 7.7 (a NIE 107.7 - 104.0 = 3.7)
        assert reading["actual_kwh"] == pytest.approx(7.7, abs=1e-4), (
            f"po restarcie stracono energie od 13:00 do 13:30: {reading['actual_kwh']}"
        )

    @pytest.mark.asyncio
    async def test_stale_baseline_from_other_hour_is_ignored(self, monkeypatch):
        """Baseline ze starszej godziny NIE moze byc uzyty.

        Gdyby HA bylo wylaczone przez cala godzine, delta liczona od takiego
        baseline'u objelaby takze godzine, ktorej nigdy nie zaraportowano —
        czyli zawyzylaby biezaca. Wolimy stracic czesc godziny (i zostawic ja
        reconcilerowi, ktory czyta statystyki HA) niz zaraportowac zla liczbe.
        """
        from datetime import datetime
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/Warsaw")
        clock = {"t": datetime(2026, 8, 20, 13, 30, tzinfo=tz)}

        def _ev(entity_id: str, value: float):
            st = MagicMock()
            st.state = str(value)
            ev = MagicMock()
            ev.data = {"new_state": st, "entity_id": entity_id}
            return ev

        baseline_store = _FakeStore()
        # Baseline dotyczy godziny 12, a jestesmy w 13.
        await baseline_store.async_save({"date": "2026-08-20", "hour": 12, "value": 100.0})

        t = _make_tracker(baseline_store=baseline_store)
        t._capacity_kwp = 10.34
        monkeypatch.setattr(t, "_get_local_now", lambda: clock["t"])
        submitted: list[list[dict]] = []
        t._async_submit = AsyncMock(side_effect=lambda r: submitted.append(r) or True)
        await t.async_start()

        t._async_state_changed(_ev("sensor.pv_energy", 104.0))
        clock["t"] = datetime(2026, 8, 20, 13, 59, tzinfo=tz)
        t._async_state_changed(_ev("sensor.pv_energy", 107.7))
        clock["t"] = datetime(2026, 8, 20, 14, 0, 20, tzinfo=tz)
        t._async_state_changed(_ev("sensor.pv_energy", 107.8))
        clock["t"] = datetime(2026, 8, 20, 14, 5, tzinfo=tz)
        await t._async_check_flush(None)

        assert submitted
        # 107.7 - 104.0 = 3.7. NIE 7.7 — baseline godziny 12 jest nieadekwatny.
        assert submitted[-1][0]["actual_kwh"] == pytest.approx(3.7, abs=1e-4)

    @pytest.mark.asyncio
    async def test_normal_rollover_still_carries_previous_bucket(self, monkeypatch):
        """Sciezka bez restartu bez zmian — baseline bierze sie z poprzedniego bucketa.

        Regresja: odzyskiwanie z Store to gałąź `else`, nie moze przeslonic
        normalnego przeniesienia energy_latest miedzy kubelkami.
        """
        from datetime import datetime
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/Warsaw")
        clock = {"t": datetime(2026, 8, 20, 12, 30, tzinfo=tz)}

        def _ev(entity_id: str, value: float):
            st = MagicMock()
            st.state = str(value)
            ev = MagicMock()
            ev.data = {"new_state": st, "entity_id": entity_id}
            return ev

        t = _make_tracker(baseline_store=_FakeStore())  # Store pusty
        t._capacity_kwp = 10.34
        monkeypatch.setattr(t, "_get_local_now", lambda: clock["t"])
        submitted: list[list[dict]] = []
        t._async_submit = AsyncMock(side_effect=lambda r: submitted.append(r) or True)
        await t.async_start()

        t._async_state_changed(_ev("sensor.pv_energy", 90.0))
        clock["t"] = datetime(2026, 8, 20, 12, 59, tzinfo=tz)
        t._async_state_changed(_ev("sensor.pv_energy", 100.0))   # koniec godziny 12
        clock["t"] = datetime(2026, 8, 20, 13, 0, 10, tzinfo=tz)
        t._async_state_changed(_ev("sensor.pv_energy", 100.1))   # rollover -> 13
        clock["t"] = datetime(2026, 8, 20, 13, 59, tzinfo=tz)
        t._async_state_changed(_ev("sensor.pv_energy", 107.7))
        clock["t"] = datetime(2026, 8, 20, 14, 0, 20, tzinfo=tz)
        t._async_state_changed(_ev("sensor.pv_energy", 107.8))
        clock["t"] = datetime(2026, 8, 20, 14, 5, tzinfo=tz)
        await t._async_check_flush(None)

        h13 = [r for batch in submitted for r in batch if r["hour"] == 13]
        assert h13, "godzina 13 powinna byc wyslana"
        assert h13[0]["actual_kwh"] == pytest.approx(7.7, abs=1e-4)


# ---------------------------------------------------------------------------
# Battery charge power: steady sensor value must carry over between hours
# ---------------------------------------------------------------------------

def _ev(entity_id: str, value: float):
    st = MagicMock()
    st.state = str(value)
    ev = MagicMock()
    ev.data = {"new_state": st, "entity_id": entity_id}
    return ev


def _make_battery_tracker(monkeypatch, clock: dict) -> tuple[VolcastProductionTracker, list]:
    tracker = _make_tracker()
    tracker._battery_soc_entity = "sensor.batt_soc"
    tracker._battery_charge_power_entity = "sensor.batt_charge"
    monkeypatch.setattr(tracker, "_get_local_now", lambda: clock["t"])
    submitted: list[list[dict]] = []
    tracker._async_submit = AsyncMock(side_effect=lambda r: submitted.append(r) or True)
    return tracker, submitted


class TestChargePowerCarryOver:
    """HA emits state_changed only on change. A full battery sits at 0 W for
    hours without a single event, so the hour that matters most for
    curtailment detection would otherwise ship without charge power at all."""

    @pytest.mark.asyncio
    async def test_steady_zero_charge_power_carries_into_next_hour(self, monkeypatch):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/Berlin")
        clock = {"t": datetime(2026, 9, 6, 12, 30, tzinfo=tz)}
        tracker, submitted = _make_battery_tracker(monkeypatch, clock)
        await tracker.async_start()

        tracker._async_state_changed(_ev("sensor.pv_energy", 100.0))
        tracker._async_state_changed(_ev("sensor.batt_soc", 100.0))
        tracker._async_state_changed(_ev("sensor.batt_charge", 0.0))   # last event ever

        clock["t"] = datetime(2026, 9, 6, 13, 0, 10, tzinfo=tz)
        tracker._async_state_changed(_ev("sensor.pv_energy", 100.5))  # rollover -> 13
        clock["t"] = datetime(2026, 9, 6, 13, 30, tzinfo=tz)
        tracker._async_state_changed(_ev("sensor.pv_energy", 101.0))
        tracker._async_state_changed(_ev("sensor.pv_power", 845.0))
        clock["t"] = datetime(2026, 9, 6, 14, 0, 10, tzinfo=tz)
        tracker._async_state_changed(_ev("sensor.pv_energy", 101.2))  # rollover -> 14
        clock["t"] = datetime(2026, 9, 6, 14, 5, tzinfo=tz)
        await tracker._async_check_flush(None)

        assert submitted, "hour 13 should have been sent"
        reading = submitted[-1][0]
        assert reading["hour"] == 13
        assert reading["battery_soc"] == 100.0
        assert reading["battery_charge_power_avg"] == 0.0
        assert reading["battery_charge_power_max"] == 0.0

    @pytest.mark.asyncio
    async def test_no_charge_event_ever_leaves_fields_absent(self, monkeypatch):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/Berlin")
        clock = {"t": datetime(2026, 9, 6, 13, 30, tzinfo=tz)}
        tracker, submitted = _make_battery_tracker(monkeypatch, clock)
        await tracker.async_start()

        tracker._async_state_changed(_ev("sensor.pv_energy", 100.0))
        clock["t"] = datetime(2026, 9, 6, 14, 0, 10, tzinfo=tz)
        tracker._async_state_changed(_ev("sensor.pv_energy", 101.0))
        clock["t"] = datetime(2026, 9, 6, 14, 5, tzinfo=tz)
        await tracker._async_check_flush(None)

        reading = submitted[-1][0]
        assert "battery_charge_power_avg" not in reading
        assert "battery_charge_power_max" not in reading

    @pytest.mark.asyncio
    async def test_events_within_hour_take_precedence_over_carry_over(self, monkeypatch):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/Berlin")
        clock = {"t": datetime(2026, 9, 6, 12, 30, tzinfo=tz)}
        tracker, submitted = _make_battery_tracker(monkeypatch, clock)
        await tracker.async_start()

        tracker._async_state_changed(_ev("sensor.pv_energy", 100.0))
        tracker._async_state_changed(_ev("sensor.batt_charge", 0.0))
        clock["t"] = datetime(2026, 9, 6, 13, 0, 10, tzinfo=tz)
        tracker._async_state_changed(_ev("sensor.pv_energy", 100.5))
        tracker._async_state_changed(_ev("sensor.batt_charge", 800.0))
        tracker._async_state_changed(_ev("sensor.batt_charge", 400.0))
        clock["t"] = datetime(2026, 9, 6, 14, 0, 10, tzinfo=tz)
        tracker._async_state_changed(_ev("sensor.pv_energy", 101.2))
        clock["t"] = datetime(2026, 9, 6, 14, 5, tzinfo=tz)
        await tracker._async_check_flush(None)

        reading = submitted[-1][0]
        assert reading["battery_charge_power_avg"] == 600.0
        assert reading["battery_charge_power_max"] == 800.0


# ---------------------------------------------------------------------------
# Silent counter entity: energy accumulated during a gap must not be lost
# ---------------------------------------------------------------------------

class _GapHarness:
    """Drives a tracker through a scripted day with a controllable clock."""

    def __init__(self, monkeypatch, baseline_store=None, day=(2026, 9, 14)):
        from zoneinfo import ZoneInfo
        self.monkeypatch = monkeypatch
        self.tz = ZoneInfo("Europe/Warsaw")
        self.day = day
        self.baseline_store = baseline_store if baseline_store is not None else _FakeStore()
        self.submitted: list[dict] = []
        self.clock = None
        self.tracker = None

    def at(self, hour: int, minute: int = 0, day_offset: int = 0):
        from datetime import datetime, timedelta
        y, m, d = self.day
        self.clock = datetime(y, m, d, hour, minute, tzinfo=self.tz) + timedelta(days=day_offset)

    async def boot(self):
        self.tracker = _make_tracker(baseline_store=self.baseline_store)
        self.tracker._capacity_kwp = 6.0
        self.monkeypatch.setattr(self.tracker, "_get_local_now", lambda: self.clock)
        self.tracker._async_submit = AsyncMock(
            side_effect=lambda r: self.submitted.extend(r) or True
        )
        await self.tracker.async_start()

    def energy(self, value: float):
        self.tracker._async_state_changed(_ev("sensor.pv_energy", value))

    async def tick(self):
        await self.tracker._async_check_flush(None)

    def reading(self, hour: int) -> dict:
        found = [r for r in self.submitted if r["hour"] == hour]
        assert found, f"hour {hour} was not submitted: {self.submitted}"
        return found[-1]


class TestGapRecovery:
    """Counter silent for hours, then returns with a jump: the total survives."""

    async def _normal_hour_then_silence(self, h: _GapHarness):
        h.at(8, 10)
        await h.boot()
        h.energy(100.0)
        h.at(8, 55)
        h.energy(100.8)
        h.at(9, 5)
        await h.tick()  # flush hour 8 -> 0.8 kWh

    async def _silent_ticks(self, h: _GapHarness, hours=(9, 10, 11, 12)):
        for hh in hours:
            h.at(hh, 5)
            await h.tick()

    @pytest.mark.asyncio
    async def test_silent_gap_total_is_recovered(self, monkeypatch):
        h = _GapHarness(monkeypatch)
        await self._normal_hour_then_silence(h)
        await self._silent_ticks(h)
        h.at(12, 20)
        h.energy(103.4)  # counter returns with the jump
        h.at(12, 55)
        h.energy(103.4)
        h.at(13, 5)
        await h.tick()

        assert h.reading(8)["actual_kwh"] == pytest.approx(0.8, abs=1e-4)
        r12 = h.reading(12)
        assert r12["actual_kwh"] == pytest.approx(2.6, abs=1e-4)
        assert r12["covers_from"] == {"date": "2026-09-14", "hour": 9}
        assert "covers_from" not in h.reading(8)
        total = sum(r["actual_kwh"] for r in h.submitted)
        assert total == pytest.approx(3.4, abs=1e-4)

    @pytest.mark.asyncio
    async def test_restart_during_gap_keeps_state(self, monkeypatch):
        store = _FakeStore()
        h = _GapHarness(monkeypatch, baseline_store=store)
        await self._normal_hour_then_silence(h)
        h.at(10, 5)
        await h.tick()
        # HA restarts in the middle of the gap: new instance, same Store.
        h.at(10, 30)
        await h.boot()
        await self._silent_ticks(h, hours=(11, 12))
        h.at(12, 20)
        h.energy(103.4)
        h.at(12, 55)
        h.energy(103.4)
        h.at(13, 5)
        await h.tick()

        r12 = h.reading(12)
        assert r12["actual_kwh"] == pytest.approx(2.6, abs=1e-4)
        assert r12["covers_from"] == {"date": "2026-09-14", "hour": 9}

    @pytest.mark.asyncio
    async def test_restart_before_last_hour_flushed_covers_that_hour(self, monkeypatch):
        store = _FakeStore()
        h = _GapHarness(monkeypatch, baseline_store=store)
        h.at(8, 10)
        await h.boot()
        h.energy(100.0)
        h.at(8, 40)
        h.energy(100.5)
        h.at(8, 50)
        await h.tick()  # persists state; hour 8 not flushed yet
        # HA down across the flush of hour 8, back at 12:20 with a new instance.
        h.at(12, 20)
        await h.boot()
        h.energy(103.4)
        h.at(12, 55)
        h.energy(103.4)
        h.at(13, 5)
        await h.tick()

        r12 = h.reading(12)
        assert r12["covers_from"] == {"date": "2026-09-14", "hour": 8}
        assert r12["actual_kwh"] == pytest.approx(3.4, abs=1e-4)

    @pytest.mark.asyncio
    async def test_counter_reset_after_gap_is_not_recovered(self, monkeypatch):
        h = _GapHarness(monkeypatch)
        await self._normal_hour_then_silence(h)
        await self._silent_ticks(h)
        h.at(12, 20)
        h.energy(2.0)  # lower than the last known value: counter was reset
        h.at(12, 55)
        h.energy(2.5)
        h.at(13, 5)
        await h.tick()

        r12 = h.reading(12)
        assert "covers_from" not in r12
        assert r12["actual_kwh"] == pytest.approx(0.5, abs=1e-4)

    @pytest.mark.asyncio
    async def test_gap_longer_than_36h_has_no_covers_from(self, monkeypatch):
        h = _GapHarness(monkeypatch)
        await self._normal_hour_then_silence(h)
        h.at(12, 20, day_offset=2)
        h.energy(110.0)
        h.at(12, 55, day_offset=2)
        h.energy(110.4)
        h.at(13, 5, day_offset=2)
        await h.tick()

        r12 = h.reading(12)
        assert "covers_from" not in r12
        assert r12["actual_kwh"] == pytest.approx(0.4, abs=1e-4)

    @pytest.mark.asyncio
    async def test_capacity_check_scales_with_hours_covered(self, monkeypatch):
        h = _GapHarness(monkeypatch)
        await self._normal_hour_then_silence(h)
        await self._silent_ticks(h)
        # 4 hours covered (9..12): 20 kWh is plausible for 6 kWp x 1.2 x 4 = 28.8,
        # although it would exceed the single-hour limit of 7.2.
        h.at(12, 20)
        h.energy(120.8)
        h.at(12, 55)
        h.energy(120.8)
        h.at(13, 5)
        await h.tick()
        assert h.reading(12)["actual_kwh"] == pytest.approx(20.0, abs=1e-4)

    @pytest.mark.asyncio
    async def test_implausible_jump_after_gap_is_still_rejected(self, monkeypatch):
        h = _GapHarness(monkeypatch)
        await self._normal_hour_then_silence(h)
        await self._silent_ticks(h)
        h.at(12, 20)
        h.energy(160.8)  # 60 kWh in 4 hours from 6 kWp
        h.at(12, 55)
        h.energy(160.8)
        h.at(13, 5)
        await h.tick()
        assert not [r for r in h.submitted if r["hour"] == 12]

    @pytest.mark.asyncio
    async def test_no_gap_payload_has_no_covers_from(self, monkeypatch):
        h = _GapHarness(monkeypatch)
        h.at(12, 10)
        await h.boot()
        h.energy(100.0)
        h.at(12, 55)
        h.energy(101.0)
        h.at(13, 1)
        h.energy(101.1)  # normal rollover: previous bucket carries the counter
        h.at(13, 5)
        await h.tick()
        h.at(13, 58)
        h.energy(102.0)
        h.at(14, 5)
        await h.tick()

        assert set(h.reading(12)) == {"date", "hour", "actual_kwh", "data_method"}
        r13 = h.reading(13)
        assert set(r13) == {"date", "hour", "actual_kwh", "data_method"}
        assert r13["actual_kwh"] == pytest.approx(1.0, abs=1e-4)

    @pytest.mark.asyncio
    async def test_restart_after_recovery_keeps_covers_from(self, monkeypatch):
        store = _FakeStore()
        h = _GapHarness(monkeypatch, baseline_store=store)
        await self._normal_hour_then_silence(h)
        await self._silent_ticks(h)
        h.at(12, 20)
        h.energy(103.4)  # recovery happens here
        h.at(12, 25)
        await h.tick()  # state persisted, including covers_from
        # HA restarts within the recovered hour.
        h.at(12, 30)
        await h.boot()
        h.at(12, 55)
        h.energy(103.6)
        h.at(13, 5)
        await h.tick()

        r12 = h.reading(12)
        assert r12["covers_from"] == {"date": "2026-09-14", "hour": 9}
        assert r12["actual_kwh"] == pytest.approx(2.8, abs=1e-4)
