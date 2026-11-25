"""Osierocone encje diagnostyczne znikają z rejestru, gdy ich źródło nie powstaje.

`submit_queue_depth` wymaga trackera, `last_reconciliation` — reconcilera; oba bywają
nieobecne (telemetria niesie produkcję, brak zmapowanych czujników, brak licznika energii),
a wpisy z wcześniejszych konfiguracji zostawały w rejestrze jako „niedostępne".
"""
from types import SimpleNamespace

import pytest

from .setup_harness import FakeEntityRegistry, run_setup
from .test_setup_issues import _paired, _patch_issues
from .test_setup_paired import PAIRED

QUEUE = "test_entry_id_submit_queue_depth"
RECON = "test_entry_id_last_reconciliation"
OTHER_QUEUE = "other_entry_submit_queue_depth"
OTHER_RECON = "other_entry_last_reconciliation"


class _Registry(FakeEntityRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.removed: list[str] = []

    def async_remove(self, entity_id) -> None:
        self.removed.append(entity_id)
        self.entities = {k: v for k, v in self.entities.items() if v.entity_id != entity_id}


def _registry() -> _Registry:
    reg = _Registry()
    for uid in (QUEUE, RECON, OTHER_QUEUE, OTHER_RECON, "test_entry_id_control_plan", "test_entry_id_forecast_today"):
        reg.entities[uid] = SimpleNamespace(entity_id=f"sensor.{uid}", unique_id=uid)
    return reg


def _removed(reg: _Registry) -> set[str]:
    return set(reg.removed)


@pytest.mark.asyncio
async def test_telemetry_carries_production_removes_both(monkeypatch):
    _patch_issues(monkeypatch)
    _paired(monkeypatch, carries=True)
    reg = _registry()
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED,
                                      options={"pv_energy_entity": "sensor.pv_e"}, entity_registry=reg)
    assert ok
    assert _removed(reg) == {f"sensor.{QUEUE}", f"sensor.{RECON}"}


@pytest.mark.asyncio
async def test_tracker_without_energy_entity_removes_only_reconciliation(monkeypatch):
    _patch_issues(monkeypatch)
    _paired(monkeypatch, carries=False)
    reg = _registry()
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED,
                                      options={"pv_power_entity": "sensor.pv_p"}, entity_registry=reg)
    assert ok
    assert hass.data["volcast"][entry.entry_id]["tracker"] is not None
    assert _removed(reg) == {f"sensor.{RECON}"}


@pytest.mark.asyncio
async def test_tracker_and_reconciler_keep_both(monkeypatch):
    _patch_issues(monkeypatch)
    _paired(monkeypatch, carries=False)
    reg = _registry()
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED,
                                      options={"pv_energy_entity": "sensor.pv_e"}, entity_registry=reg)
    assert ok
    assert reg.removed == []


@pytest.mark.asyncio
async def test_other_entities_and_other_config_entry_untouched(monkeypatch):
    _patch_issues(monkeypatch)
    _paired(monkeypatch, carries=True)
    reg = _registry()
    await run_setup(monkeypatch.setattr, data=PAIRED, entity_registry=reg)
    left = {e.unique_id for e in reg.entities.values()}
    assert {OTHER_QUEUE, OTHER_RECON, "test_entry_id_control_plan", "test_entry_id_forecast_today"} <= left
    assert QUEUE not in left and RECON not in left


@pytest.mark.asyncio
async def test_forecast_only_without_mapping_removes_both(monkeypatch):
    reg = _registry()
    hass, entry, ok = await run_setup(monkeypatch.setattr, entity_registry=reg)
    assert ok
    assert _removed(reg) == {f"sensor.{QUEUE}", f"sensor.{RECON}"}
