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

    We return the coordinator's *merged* history (retained past days + current
    poll), not just the latest response. The Volcast API only returns today +
    future days, so serving the raw response would make the forecast line vanish
    the moment you navigate to a previous day. Retaining past days mirrors how
    Solcast keeps its forecast visible historically.
    """
    entry_data = hass.data.get(DOMAIN, {}).get(config_entry_id, {})
    coordinator: VolcastCoordinator | None = (
        entry_data.get("coordinator") if isinstance(entry_data, dict) else None
    )
    if coordinator is None:
        return None

    wh_hours = coordinator.get_solar_forecast_wh_hours()
    if not wh_hours:
        return None

    return {"wh_hours": wh_hours}
