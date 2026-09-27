"""Import historii zużycia na prawdziwym rekorderze (statystyki godzinowe, przeliczenie
jednostki przez rekorder, metadane)."""
from __future__ import annotations

from datetime import timedelta

import pytest

from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.core import HomeAssistant
import homeassistant.util.dt as dt_util

from custom_components.volcast.control import history_import as hi

LOAD = "sensor.house_energy"
PV = "sensor.pv_energy"


def _meta(sid: str, unit: str, unit_class: str | None = "energy") -> dict:
    return {"has_sum": True, "mean_type": StatisticMeanType.NONE, "name": None, "source": "recorder",
            "statistic_id": sid, "unit_class": unit_class, "unit_of_measurement": unit}


def _rows(start, hours: int, step: float) -> list[dict]:
    return [{"start": start + timedelta(hours=i), "state": step * (i + 1), "sum": step * (i + 1)}
            for i in range(hours)]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """Rekorder przed `hass` (harness wymaga bazy przed startem HA)."""
    yield


class FakeCloud:
    def __init__(self):
        self.parts = []

    async def async_import_history(self, hours):
        self.parts.append(hours)
        return {"accepted": len(hours), "inserted": len(hours), "skipped_existing": 0}


class FakeExecutor:
    history_imported_at = None

    async def async_mark_history_imported(self, when):
        self.history_imported_at = when


async def _import(hass, sid: str, unit: str, step: float, *, unit_class="energy", hours=30):
    now = dt_util.utcnow()
    start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=hours + 1)
    async_import_statistics(hass, _meta(sid, unit, unit_class), _rows(start, hours, step))
    await async_wait_recording_done(hass)
    return now


async def test_statistics_and_metadata_calls_match_real_recorder(hass: HomeAssistant):
    now = await _import(hass, LOAD, "Wh", 500.0)
    rows = await hi._async_statistics(hass, {LOAD}, now - timedelta(days=10), now)
    units = await hi._async_units(hass, {LOAD})
    assert units == {LOAD: "Wh"}
    changes = [r["change"] for r in rows[LOAD]]
    # Rekorder przelicza Wh → kWh sam (jawnie żądana jednostka); 500 Wh na godzinę = 0,5 kWh.
    assert changes and all(abs(c - 0.5) < 1e-9 for c in changes[1:])


async def test_history_import_sends_kwh_hours_and_marks_done(hass: HomeAssistant):
    now = await _import(hass, LOAD, "Wh", 500.0)
    await _import(hass, PV, "kWh", 1.25)
    cloud, ex = FakeCloud(), FakeExecutor()
    res = await hi.async_import_history_once(hass, cloud, ex, load_entity=LOAD, pv_entity=PV, now_utc=now)
    hours = [h for part in cloud.parts for h in part]
    assert res and res["accepted"] == len(hours) and ex.history_imported_at == now.isoformat()
    assert hours and all(abs(h["load_kwh"] - 0.5) < 1e-6 for h in hours[1:])
    assert all(abs(h["pv_kwh"] - 1.25) < 1e-6 for h in hours[1:] if "pv_kwh" in h)
    assert all(h["start"].endswith(":00:00Z") for h in hours)


async def test_non_energy_statistic_is_skipped(hass: HomeAssistant):
    now = await _import(hass, LOAD, "W", 500.0, unit_class="power")
    cloud, ex = FakeCloud(), FakeExecutor()
    assert await hi.async_import_history_once(hass, cloud, ex, load_entity=LOAD, now_utc=now) is None
    assert cloud.parts == [] and ex.history_imported_at is None
