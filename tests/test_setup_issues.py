"""Zgłoszenie naprawy prognozy powstaje zawsze, gdy brak czujników produkcji.

Waga zgłoszenia pochodzi z rejestru zgłoszeń (`issue_registry.IssueSeverity`) —
komponent `repairs` jej nie re-eksportuje, więc import stamtąd po cichu wyłączał zgłoszenie.
"""
import sys
from unittest.mock import MagicMock

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
