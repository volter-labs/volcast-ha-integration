"""Tests for choosing the entities that feed production tracking."""

from __future__ import annotations

import types

import pytest

from tests.conftest import FakeHass

from custom_components.volcast.const import (
    CONF_PV_ENERGY_ENTITY,
    CONF_PV_POWER_ENTITY,
    CONTROL_MODE_DIRECT,
    DOMAIN,
    OPT_CONTROL_MODE,
)
from custom_components.volcast.production_source import (
    ProductionEntities,
    resolve_production_entities,
)

ENTRY_ID = "entry123"
ENERGY_UID = f"{ENTRY_ID}_control_direct_pv_energy_total_kwh"
POWER_UID = f"{ENTRY_ID}_control_direct_pv_power_w"


class _DictRegistry:
    """Rejestr encji: (domain, platform, unique_id) -> entity_id."""

    def __init__(self, ids: dict[tuple[str, str, str], str] | None = None) -> None:
        self.ids = dict(ids or {})
        self.calls: list[tuple[str, str, str]] = []

    def async_get_entity_id(self, domain, platform, unique_id):
        self.calls.append((domain, platform, unique_id))
        return self.ids.get((domain, platform, unique_id))


def _hass(registry: _DictRegistry | None = None) -> FakeHass:
    hass = FakeHass()
    # Stub `er.async_get(hass)` z conftest zwraca hass.data["entity_registry"].
    hass.data["entity_registry"] = registry or _DictRegistry()
    return hass


def _entry(**options) -> types.SimpleNamespace:
    return types.SimpleNamespace(entry_id=ENTRY_ID, options=dict(options))


def _direct_registry(*, energy: bool = True, power: bool = True) -> _DictRegistry:
    ids = {}
    if energy:
        ids[("sensor", DOMAIN, ENERGY_UID)] = "sensor.volcast_pv_energy_total"
    if power:
        ids[("sensor", DOMAIN, POWER_UID)] = "sensor.volcast_pv_power"
    return _DictRegistry(ids)


def test_external_mapping_wins_over_direct() -> None:
    hass = _hass(_direct_registry())
    entry = _entry(**{
        CONF_PV_ENERGY_ENTITY: "sensor.ext_energy",
        CONF_PV_POWER_ENTITY: "sensor.ext_power",
        OPT_CONTROL_MODE: CONTROL_MODE_DIRECT,
    })

    assert resolve_production_entities(hass, entry) == ProductionEntities(
        "sensor.ext_energy", "sensor.ext_power", "external")


def test_direct_without_external_uses_registry_ids() -> None:
    registry = _direct_registry()
    hass = _hass(registry)
    entry = _entry(**{OPT_CONTROL_MODE: CONTROL_MODE_DIRECT})

    result = resolve_production_entities(hass, entry)

    assert result == ProductionEntities(
        "sensor.volcast_pv_energy_total", "sensor.volcast_pv_power", "direct")
    assert ("sensor", DOMAIN, ENERGY_UID) in registry.calls
    assert ("sensor", DOMAIN, POWER_UID) in registry.calls


def test_direct_with_only_energy_registered() -> None:
    hass = _hass(_direct_registry(power=False))
    entry = _entry(**{OPT_CONTROL_MODE: CONTROL_MODE_DIRECT})

    assert resolve_production_entities(hass, entry) == ProductionEntities(
        "sensor.volcast_pv_energy_total", "", "direct")


def test_direct_before_registration_resolves_none() -> None:
    hass = _hass(_DictRegistry())
    entry = _entry(**{OPT_CONTROL_MODE: CONTROL_MODE_DIRECT})

    assert resolve_production_entities(hass, entry) == ProductionEntities("", "", None)


@pytest.mark.parametrize("mode", ["entities", None])
def test_non_direct_without_mapping_resolves_none(mode) -> None:
    # Rejestr ma encje direct (np. pozostałość po zmianie trybu) — poza direct się nie liczą.
    hass = _hass(_direct_registry())
    options = {} if mode is None else {OPT_CONTROL_MODE: mode}

    assert resolve_production_entities(hass, _entry(**options)) == ProductionEntities(
        "", "", None)


def test_only_external_energy_is_external_with_empty_power() -> None:
    hass = _hass(_direct_registry())
    entry = _entry(**{
        CONF_PV_ENERGY_ENTITY: "sensor.ext_energy",
        OPT_CONTROL_MODE: CONTROL_MODE_DIRECT,
    })

    assert resolve_production_entities(hass, entry) == ProductionEntities(
        "sensor.ext_energy", "", "external")


def test_production_entities_is_frozen() -> None:
    src = ProductionEntities("a", "b", "external")
    with pytest.raises(AttributeError):
        src.energy_entity = "c"  # type: ignore[misc]
