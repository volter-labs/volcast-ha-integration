"""Tests for the Energy Dashboard solar-forecast provider (energy.py).

The provider must serve the coordinator's *retained* forecast history (past
days + current poll), so the Energy Dashboard keeps showing the forecast line
when the user navigates back to previous days.
"""

from __future__ import annotations

import pytest

from custom_components.volcast.const import DOMAIN
from custom_components.volcast.coordinator import VolcastCoordinator
from custom_components.volcast.energy import async_get_solar_forecast
from tests.conftest import FakeHass


def _coord_with_history(history: dict) -> VolcastCoordinator:
    coord = VolcastCoordinator(
        hass=FakeHass(), api_key="k", api_url="http://stub", entry_id=None,
    )
    coord._forecast_history = dict(history)
    coord._history_loaded = True
    return coord


@pytest.mark.asyncio
async def test_returns_merged_history_including_past_days():
    hass = FakeHass()
    coord = _coord_with_history(
        {"2026-05-30T10:00:00+02:00": 3200, "2026-05-31T10:00:00+02:00": 4100}
    )
    hass.data = {DOMAIN: {"entry1": {"coordinator": coord}}}

    result = await async_get_solar_forecast(hass, "entry1")

    assert result == {
        "wh_hours": {
            "2026-05-30T10:00:00+02:00": 3200,
            "2026-05-31T10:00:00+02:00": 4100,
        }
    }


@pytest.mark.asyncio
async def test_returns_none_when_history_empty():
    hass = FakeHass()
    coord = _coord_with_history({})
    hass.data = {DOMAIN: {"entry1": {"coordinator": coord}}}

    assert await async_get_solar_forecast(hass, "entry1") is None


@pytest.mark.asyncio
async def test_returns_none_when_no_coordinator():
    hass = FakeHass()
    hass.data = {DOMAIN: {}}

    assert await async_get_solar_forecast(hass, "missing") is None
