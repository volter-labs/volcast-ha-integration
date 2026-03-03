"""Energy platform for Volcast — provides solar forecast to HA Energy Dashboard."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import VolcastCoordinator


async def async_get_solar_forecast(
    hass: HomeAssistant, config_entry_id: str
) -> dict[str, dict[str, float | int]] | None:
    """Get solar forecast for a config entry ID.

    Called by HA Energy Dashboard to display solar production forecast.
    Returns {"wh_hours": {"ISO_TIMESTAMP": wh_value, ...}} or None.
    """
    coordinator: VolcastCoordinator | None = hass.data.get(DOMAIN, {}).get(
        config_entry_id
    )
    if coordinator is None or coordinator.data is None:
        return None

    if not coordinator.data.wh_hours:
        return None

    return {"wh_hours": coordinator.data.wh_hours}
