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
