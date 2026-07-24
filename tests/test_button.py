"""Tests for the Volcast sync-now button."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.conftest import FakeHass


class _FakeConfigEntryForButton:
    entry_id = "test_entry_id"


@pytest.mark.asyncio
async def test_setup_adds_button_when_reconciler_present():
    from custom_components.volcast.button import async_setup_entry

    hass = FakeHass()
    reconciler = MagicMock()
    hass.data["volcast"] = {"test_entry_id": {"reconciler": reconciler}}

    added: list = []
    await async_setup_entry(
        hass, _FakeConfigEntryForButton(), lambda ents, **kw: added.extend(ents)
    )

    assert len(added) == 1
    assert added[0]._attr_unique_id == "test_entry_id_sync_now"


@pytest.mark.asyncio
async def test_setup_skips_button_without_reconciler():
    """Bez energy_entity nie ma reconcilera — button nie powstaje."""
    from custom_components.volcast.button import async_setup_entry

    hass = FakeHass()
    hass.data["volcast"] = {"test_entry_id": {"reconciler": None}}

    added: list = []
    await async_setup_entry(
        hass, _FakeConfigEntryForButton(), lambda ents, **kw: added.extend(ents)
    )

    assert added == []


@pytest.mark.asyncio
async def test_press_runs_reconcile_recent():
    from custom_components.volcast.button import VolcastSyncButton

    reconciler = MagicMock()
    reconciler.reconcile_recent = AsyncMock(return_value=[])
    button = VolcastSyncButton(reconciler, "test_entry_id")

    await button.async_press()

    reconciler.reconcile_recent.assert_awaited_once()


@pytest.mark.asyncio
async def test_press_logs_summary_for_nonempty_results():
    """Press z niepustymi wynikami wykonuje ścieżkę budowania logu —
    regression guard na rename pól ReconcileResult używanych w f-stringu."""
    from custom_components.volcast.button import VolcastSyncButton
    from custom_components.volcast.reconciler import ReconcileResult

    reconciler = MagicMock()
    reconciler.reconcile_recent = AsyncMock(return_value=[
        ReconcileResult(skipped=True, reason="out_of_window", submitted=0),
        ReconcileResult(success=True, submitted=2, accepted=2),
    ])
    button = VolcastSyncButton(reconciler, "test_entry_id")

    # Must not raise while formatting the log line from real ReconcileResult objects.
    await button.async_press()

    reconciler.reconcile_recent.assert_awaited_once()
