"""Tests for production tracker retry queue."""

from __future__ import annotations

import asyncio
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
) -> VolcastProductionTracker:
    """Create a tracker with sensible defaults and injectable Store."""
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
    # Wstrzyknij fake Store jeśli podany
    if store is not None:
        tracker._store = store
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
    """Return a mock aiohttp session whose POST returns 200 OK."""
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


def _mock_session_error(status: int = 500, text: str = "Internal Server Error"):
    """Return a mock aiohttp session whose POST returns an error."""
    resp = AsyncMock()
    resp.ok = False
    resp.status = status
    resp.text = AsyncMock(return_value=text)

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.post = MagicMock(return_value=ctx)
    return session


def _mock_session_timeout():
    """Return a mock aiohttp session whose POST raises a timeout."""
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(side_effect=TimeoutError("DNS timeout"))
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
        """Gdy POST rzuci wyjątek (DNS timeout), reading powinien trafić do kolejki."""
        store = _FakeStore()
        tracker = _make_tracker(store=store)

        session = _mock_session_timeout()

        with patch(
            "custom_components.volcast.production.async_get_clientsession",
            return_value=session,
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
        store = _FakeStore()
        # Pre-populate z 48 wpisami (unikalne date+hour combo)
        full_queue = [
            _make_reading(date=f"2026-04-{(i // 24 + 1):02d}", hour=i % 24, kwh=float(i))
            for i in range(48)
        ]
        await store.async_save(full_queue)

        tracker = _make_tracker(store=store)
        await tracker._async_load_queue()

        session = _mock_session_timeout()

        with patch(
            "custom_components.volcast.production.async_get_clientsession",
            return_value=session,
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
        """429 rate limit powinien kolejkować reading, nie tracić go."""
        store = _FakeStore()
        tracker = _make_tracker(store=store)

        session = _mock_session_error(status=429, text="rate limited")

        with patch(
            "custom_components.volcast.production.async_get_clientsession",
            return_value=session,
        ):
            readings = [_make_reading(hour=11, kwh=3.5)]
            success = await tracker._async_submit(readings)

        assert success is False
        assert len(tracker._queue) == 1

    @pytest.mark.asyncio
    async def test_queue_persists_across_tracker_restart(self):
        """Kolejka powinna przetrwać restart trackera (Store persystencja)."""
        store = _FakeStore()

        # Tracker 1: fail → queue reading
        tracker1 = _make_tracker(store=store)
        session = _mock_session_timeout()

        with patch(
            "custom_components.volcast.production.async_get_clientsession",
            return_value=session,
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
