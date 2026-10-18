"""Choose the entities that feed production tracking.

In direct control mode the integration reads the inverter itself, so production
tracking must not depend on sensors provided by another integration: the
integration's own direct-mode sensors (PV energy total and PV power) are used.

An explicit external mapping (``pv_energy_entity`` / ``pv_power_entity`` in the
entry options) always wins when configured, in any control mode.

The direct-mode sensors are looked up in the entity registry by unique id. The
registry survives restarts, so from the second start on their ids are known
before the platforms load; on the first start they are registered only once the
platforms are forwarded, and the caller re-resolves after that.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_PV_ENERGY_ENTITY,
    CONF_PV_POWER_ENTITY,
    CONTROL_MODE_DIRECT,
    DOMAIN,
    OPT_CONTROL_MODE,
)

# Klucze sensorów trybu direct (control/direct_sensors.py: unique_id
# f"{entry_id}_control_direct_{key}").
_DIRECT_ENERGY_KEY = "pv_energy_total_kwh"
_DIRECT_POWER_KEY = "pv_power_w"


@dataclass(frozen=True)
class ProductionEntities:
    """Encje produkcji i ich źródło (None — brak źródła)."""

    energy_entity: str
    power_entity: str
    source: Literal["external", "direct"] | None


def resolve_production_entities(hass: HomeAssistant, entry: ConfigEntry) -> ProductionEntities:
    """Wybierz encje produkcji: mapowanie zewnętrzne, potem sensory direct."""
    energy = entry.options.get(CONF_PV_ENERGY_ENTITY, "") or ""
    power = entry.options.get(CONF_PV_POWER_ENTITY, "") or ""
    if energy or power:
        return ProductionEntities(energy, power, "external")

    if entry.options.get(OPT_CONTROL_MODE) == CONTROL_MODE_DIRECT:
        registry = er.async_get(hass)

        def _lookup(key: str) -> str:
            unique_id = f"{entry.entry_id}_control_direct_{key}"
            return registry.async_get_entity_id("sensor", DOMAIN, unique_id) or ""

        energy = _lookup(_DIRECT_ENERGY_KEY)
        power = _lookup(_DIRECT_POWER_KEY)
        if energy or power:
            return ProductionEntities(energy, power, "direct")

    return ProductionEntities("", "", None)
