"""The Volcast Solar Forecast integration."""

from __future__ import annotations

import logging

try:
    from homeassistant.components.repairs import IssueSeverity
except ImportError:
    IssueSeverity = None
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import (
    CONF_API_URL,
    CONF_BATTERY_SOC_ENTITY,
    CONF_PV_ENERGY_ENTITY,
    CONF_PV_POWER_ENTITY,
    CONF_UPDATE_INTERVAL,
    DEFAULT_API_URL,
    DEFAULT_SUBMIT_URL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
)
from .coordinator import VolcastCoordinator
from .production import VolcastProductionTracker

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Volcast from a config entry."""
    api_key = entry.data[CONF_API_KEY]
    api_url = entry.data.get(CONF_API_URL, DEFAULT_API_URL)
    update_interval = entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)

    coordinator = VolcastCoordinator(hass, api_key, api_url, update_interval)
    await coordinator.async_config_entry_first_refresh()

    # Production tracker — opcjonalny (wymaga skonfigurowanych sensorów)
    energy_entity = entry.options.get(CONF_PV_ENERGY_ENTITY, "")
    power_entity = entry.options.get(CONF_PV_POWER_ENTITY, "")
    battery_soc_entity = entry.options.get(CONF_BATTERY_SOC_ENTITY, "")

    tracker: VolcastProductionTracker | None = None
    if energy_entity or power_entity:
        # submit_url z odpowiedzi API (jeśli dostępny) lub domyślny
        submit_url = DEFAULT_SUBMIT_URL
        if coordinator.data and coordinator.data.submit_url:
            submit_url = coordinator.data.submit_url

        tracker = VolcastProductionTracker(
            hass=hass,
            api_key=api_key,
            submit_url=submit_url,
            energy_entity=energy_entity,
            power_entity=power_entity,
            battery_soc_entity=battery_soc_entity,
            system_capacity_kwp=(
                coordinator.data.system_capacity_kwp if coordinator.data else None
            ),
        )
        await tracker.async_start()

        # Wyczyść ewentualny repair issue (użytkownik już skonfigurował)
        ir.async_delete_issue(hass, DOMAIN, "production_tracking_available")
    else:
        if IssueSeverity is not None:
            ir.async_create_issue(
                hass,
                DOMAIN,
                "production_tracking_available",
                is_fixable=False,
                severity=IssueSeverity.WARNING,
                translation_key="production_tracking_available",
            )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator,
        "tracker": tracker,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        entry_data = hass.data[DOMAIN].pop(entry.entry_id)
        tracker = entry_data.get("tracker")
        if tracker is not None:
            await tracker.async_stop()
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update — reload the integration."""
    await hass.config_entries.async_reload(entry.entry_id)
