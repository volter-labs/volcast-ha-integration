"""Tests for the volcast.sync_production service."""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.conftest import FakeHass


class _FakeServiceCall:
    """Minimal ServiceCall double — only .data is used by the handler."""

    def __init__(self, data: dict | None = None):
        self.data = data or {}


def _register(hass: FakeHass):
    """Zarejestruj serwis i zwróć handler z fake rejestru."""
    from custom_components.volcast import _async_register_services
    from custom_components.volcast.const import DOMAIN, SERVICE_SYNC_PRODUCTION

    _async_register_services(hass)
    return hass.services.registered[(DOMAIN, SERVICE_SYNC_PRODUCTION)]


def _hass_with_reconciler(reconciler) -> FakeHass:
    hass = FakeHass()
    hass.data["volcast"] = {"entry1": {"reconciler": reconciler}}
    return hass


def test_register_services_is_idempotent():
    """Podwójna rejestracja (2 config entries) nie nadpisuje handlera."""
    from custom_components.volcast import _async_register_services
    from custom_components.volcast.const import DOMAIN, SERVICE_SYNC_PRODUCTION

    hass = FakeHass()
    _async_register_services(hass)
    first = hass.services.registered[(DOMAIN, SERVICE_SYNC_PRODUCTION)]
    _async_register_services(hass)
    assert hass.services.registered[(DOMAIN, SERVICE_SYNC_PRODUCTION)] is first


@pytest.mark.asyncio
async def test_sync_without_date_calls_reconcile_recent():
    reconciler = MagicMock()
    reconciler.reconcile_recent = AsyncMock()
    hass = _hass_with_reconciler(reconciler)
    handler = _register(hass)

    await handler(_FakeServiceCall())

    reconciler.reconcile_recent.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_with_date_calls_reconcile_day():
    reconciler = MagicMock()
    reconciler.reconcile_day = AsyncMock()
    hass = _hass_with_reconciler(reconciler)
    handler = _register(hass)

    await handler(_FakeServiceCall({"date": "2026-07-23"}))

    reconciler.reconcile_day.assert_awaited_once_with(date(2026, 7, 23))


@pytest.mark.asyncio
async def test_sync_with_date_object_calls_reconcile_day():
    """Selector date w HA może dostarczyć datetime.date, nie str."""
    reconciler = MagicMock()
    reconciler.reconcile_day = AsyncMock()
    hass = _hass_with_reconciler(reconciler)
    handler = _register(hass)

    await handler(_FakeServiceCall({"date": date(2026, 7, 23)}))

    reconciler.reconcile_day.assert_awaited_once_with(date(2026, 7, 23))


@pytest.mark.asyncio
async def test_sync_invalid_date_raises_validation_error():
    from homeassistant.exceptions import ServiceValidationError

    reconciler = MagicMock()
    hass = _hass_with_reconciler(reconciler)
    handler = _register(hass)

    with pytest.raises(ServiceValidationError):
        await handler(_FakeServiceCall({"date": "not-a-date"}))


@pytest.mark.asyncio
async def test_sync_no_reconcilers_raises_validation_error():
    """Entry bez production trackingu (brak energy_entity) → czytelny błąd."""
    from homeassistant.exceptions import ServiceValidationError

    hass = FakeHass()
    hass.data["volcast"] = {"entry1": {"reconciler": None}}
    handler = _register(hass)

    with pytest.raises(ServiceValidationError):
        await handler(_FakeServiceCall())


@pytest.mark.asyncio
async def test_sync_iterates_all_entries():
    """Serwis bez daty działa na wszystkich config entries z reconcilerem."""
    rec1, rec2 = MagicMock(), MagicMock()
    rec1.reconcile_recent = AsyncMock()
    rec2.reconcile_recent = AsyncMock()
    hass = FakeHass()
    hass.data["volcast"] = {
        "entry1": {"reconciler": rec1},
        "entry2": {"reconciler": rec2},
    }
    handler = _register(hass)

    await handler(_FakeServiceCall())

    rec1.reconcile_recent.assert_awaited_once()
    rec2.reconcile_recent.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_with_datetime_normalizes_to_date():
    """A full datetime (datetime subclasses date) is normalized to its date,
    not passed through — otherwise reconcile_day would TypeError silently."""
    from datetime import datetime

    reconciler = MagicMock()
    reconciler.reconcile_day = AsyncMock()
    hass = _hass_with_reconciler(reconciler)
    handler = _register(hass)

    await handler(_FakeServiceCall({"date": datetime(2026, 7, 23, 15, 30)}))

    reconciler.reconcile_day.assert_awaited_once_with(date(2026, 7, 23))


# ---------------------------------------------------------------------------
# Lifecycle: service removed only on the LAST config-entry unload
# ---------------------------------------------------------------------------


def _make_config_entry(entry_id: str):
    """Minimalny ConfigEntry double — async_unload_entry używa tylko .entry_id."""
    entry = MagicMock()
    entry.entry_id = entry_id
    return entry


@pytest.mark.asyncio
async def test_service_survives_first_of_two_unloads_removed_on_last():
    """Serwis domenowy przeżywa unload jednego z dwóch entry i jest usuwany
    dopiero przy ostatnim (hass.data[DOMAIN] pusty)."""
    from custom_components.volcast import async_unload_entry
    from custom_components.volcast.const import DOMAIN, SERVICE_SYNC_PRODUCTION

    hass = FakeHass()
    # Stub config_entries.async_unload_platforms → True (platformy odłączone OK).
    hass.config_entries = MagicMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)

    # Dwa entry z reconcilerem; tracker stubowany (async_stop musi być awaitable).
    tracker1, tracker2 = MagicMock(), MagicMock()
    tracker1.async_stop = AsyncMock()
    tracker2.async_stop = AsyncMock()
    hass.data[DOMAIN] = {
        "entry1": {"reconciler": MagicMock(), "tracker": tracker1},
        "entry2": {"reconciler": MagicMock(), "tracker": tracker2},
    }
    # Serwis zarejestrowany (jak po async_setup_entry).
    _register(hass)
    assert hass.services.has_service(DOMAIN, SERVICE_SYNC_PRODUCTION)

    # Pierwszy unload — serwis MUSI przetrwać (drugi entry wciąż aktywny).
    assert await async_unload_entry(hass, _make_config_entry("entry1")) is True
    tracker1.async_stop.assert_awaited_once()
    assert hass.services.has_service(DOMAIN, SERVICE_SYNC_PRODUCTION)
    assert "entry1" not in hass.data[DOMAIN]

    # Drugi (ostatni) unload — teraz serwis znika.
    assert await async_unload_entry(hass, _make_config_entry("entry2")) is True
    tracker2.async_stop.assert_awaited_once()
    assert not hass.services.has_service(DOMAIN, SERVICE_SYNC_PRODUCTION)
    assert hass.data[DOMAIN] == {}


@pytest.mark.asyncio
async def test_unload_platforms_failure_keeps_service_and_entry():
    """Gdy async_unload_platforms zwróci False, entry nie jest zdejmowane
    i serwis pozostaje (unload nieudany → brak side-effectów)."""
    from custom_components.volcast import async_unload_entry
    from custom_components.volcast.const import DOMAIN, SERVICE_SYNC_PRODUCTION

    hass = FakeHass()
    hass.config_entries = MagicMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=False)

    tracker = MagicMock()
    tracker.async_stop = AsyncMock()
    hass.data[DOMAIN] = {"entry1": {"reconciler": MagicMock(), "tracker": tracker}}
    _register(hass)

    assert await async_unload_entry(hass, _make_config_entry("entry1")) is False
    # Nic nie zdjęte: entry zostaje, tracker nie zatrzymany, serwis żyje.
    assert "entry1" in hass.data[DOMAIN]
    tracker.async_stop.assert_not_awaited()
    assert hass.services.has_service(DOMAIN, SERVICE_SYNC_PRODUCTION)
