"""Zgłoszenie naprawy prognozy powstaje zawsze, gdy brak czujników produkcji.

Waga zgłoszenia pochodzi z rejestru zgłoszeń (`issue_registry.IssueSeverity`) —
komponent `repairs` jej nie re-eksportuje, więc import stamtąd po cichu wyłączał zgłoszenie.
"""
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from .setup_harness import run_setup


@pytest.fixture(autouse=True)
def _no_network_probe(monkeypatch):
    # Wykrywanie w teście nie wysyła pakietów w sieć (jak w pozostałych testach setupu).
    from unittest.mock import AsyncMock

    from custom_components.volcast import discovery_runner
    monkeypatch.setattr(discovery_runner, "probe_udp_48899", AsyncMock(return_value=None))


@pytest.mark.asyncio
async def test_forecast_entry_without_production_sensors_raises_repair_issue(monkeypatch):
    ir = sys.modules["homeassistant.helpers.issue_registry"]
    create = MagicMock()
    monkeypatch.setattr(ir, "async_create_issue", create)
    hass, entry, ok = await run_setup(monkeypatch.setattr)
    assert ok
    issues = [c for c in create.call_args_list if c.args[2] == "production_tracking_available"]
    assert len(issues) == 1
    kw = issues[0].kwargs
    assert kw["translation_key"] == "production_tracking_available" and kw["is_fixable"] is False
    # Atrapa rejestru nie ma enuma wag — wtedy wartość tekstowa, jak w prawdziwym HA (StrEnum).
    assert kw["severity"] == "warning"


@pytest.mark.asyncio
async def test_severity_comes_from_issue_registry_when_available(monkeypatch):
    import custom_components.volcast as integ

    ir = sys.modules["homeassistant.helpers.issue_registry"]
    create = MagicMock()
    monkeypatch.setattr(ir, "async_create_issue", create)
    marker = object()
    monkeypatch.setattr(integ, "_ISSUE_WARNING", marker)
    hass, entry, ok = await run_setup(monkeypatch.setattr)
    assert ok
    kw = next(c.kwargs for c in create.call_args_list if c.args[2] == "production_tracking_available")
    assert kw["severity"] is marker


def test_integration_does_not_import_severity_from_repairs():
    from pathlib import Path

    src = (Path(__file__).parents[1] / "custom_components" / "volcast" / "__init__.py").read_text(encoding="utf-8")
    assert "components.repairs import" not in src


# --- Mapowanie czujników PV nieużywane przy sterowaniu falownikiem ---------------------------

_MAPPED = {"pv_energy_entity": "sensor.pv_e", "pv_power_entity": "sensor.pv_p"}


def _unused_calls(mock, _name=None):
    return [c for c in mock.call_args_list if c.args[2].startswith("production_mapping_unused_")]


def _patch_issues(monkeypatch):
    ir = sys.modules["homeassistant.helpers.issue_registry"]
    create, delete = MagicMock(), MagicMock()
    monkeypatch.setattr(ir, "async_create_issue", create)
    monkeypatch.setattr(ir, "async_delete_issue", delete)
    return create, delete


def _paired(monkeypatch, *, carries: bool):
    import custom_components.volcast as integ
    from .test_setup_paired import fake_runtime

    async def setup_control(hass, entry, *, report):
        rt = fake_runtime()
        if carries:
            # Nadajnik telemetrii z mocą/licznikiem PV — jedyne źródło odpowiedzi „niesie produkcję".
            from custom_components.volcast.control.telemetry import TelemetrySender
            rt.telemetry = TelemetrySender(None, SimpleNamespace(entry_id="e1", options={}), None, None,
                                           choice=None, profile_map={}, grid_negate=False, limits=None,
                                           manual_map={"pv_power_w": "sensor.pv_power"})
        return rt
    monkeypatch.setattr(integ, "async_setup_control", setup_control)


@pytest.mark.asyncio
async def test_paired_with_mapping_raises_mapping_unused_issue(monkeypatch):
    from .test_setup_paired import PAIRED
    create, delete = _patch_issues(monkeypatch)
    _paired(monkeypatch, carries=True)
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED, options=_MAPPED)
    assert ok
    calls = _unused_calls(create, "create")
    assert len(calls) == 1
    assert calls[0].args[2] == f"production_mapping_unused_{entry.entry_id}"
    kw = calls[0].kwargs
    assert kw["translation_key"] == "production_mapping_unused" and kw["is_fixable"] is False
    assert kw["severity"] == "warning"
    assert kw["translation_placeholders"] == {"entities": "sensor.pv_e, sensor.pv_p"}


@pytest.mark.asyncio
async def test_paired_without_mapping_has_no_mapping_unused_issue(monkeypatch):
    from .test_setup_paired import PAIRED
    create, delete = _patch_issues(monkeypatch)
    _paired(monkeypatch, carries=True)
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED)
    assert ok and not _unused_calls(create, "create")
    assert any(c.args[2] == f"production_mapping_unused_{entry.entry_id}" for c in delete.call_args_list)


@pytest.mark.asyncio
async def test_forecast_only_with_mapping_has_no_mapping_unused_issue(monkeypatch):
    create, delete = _patch_issues(monkeypatch)
    hass, entry, ok = await run_setup(monkeypatch.setattr, options=_MAPPED)
    assert ok and not _unused_calls(create, "create")


@pytest.mark.asyncio
async def test_paired_without_production_telemetry_keeps_tracker_and_no_issue(monkeypatch):
    from .test_setup_paired import PAIRED
    create, delete = _patch_issues(monkeypatch)
    _paired(monkeypatch, carries=False)
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED, options=_MAPPED)
    assert ok and not _unused_calls(create, "create")


@pytest.mark.asyncio
async def test_unload_deletes_mapping_unused_issue(monkeypatch):
    import custom_components.volcast as integ
    from .test_setup_paired import PAIRED
    create, delete = _patch_issues(monkeypatch)
    _paired(monkeypatch, carries=True)
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED, options=_MAPPED)
    monkeypatch.setattr(integ, "async_unload_control", AsyncMock())
    delete.reset_mock()
    assert await integ.async_unload_entry(hass, entry) is True
    assert any(c.args[2] == f"production_mapping_unused_{entry.entry_id}" for c in delete.call_args_list)
