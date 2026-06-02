"""Constants for the Volcast Solar Forecast integration."""

from __future__ import annotations

DOMAIN = "volcast"

DEFAULT_API_URL = "https://volcast.app/api/forecast"
DEFAULT_SUBMIT_URL = "https://volcast.app/api/submit-production"
DEFAULT_UPDATE_INTERVAL = 30  # minutes (API is cheap cache read)
DEFAULT_PEAK_THRESHOLD = 80  # percent of today's peak power

# Forecast history persistence — retain past-day forecasts so the HA Energy
# Dashboard can display "what was forecast" when navigating to previous days.
# The Volcast API only returns today + future days; without retention the
# past-day forecast line vanishes the moment the day rolls over (unlike
# Solcast, which keeps a rolling history). We snapshot each poll's wh_hours,
# merge newest-wins, and prune anything older than the retention window.
FORECAST_HISTORY_STORAGE_VERSION = 1
FORECAST_HISTORY_RETENTION_DAYS = 14

CONF_API_URL = "api_url"
CONF_UPDATE_INTERVAL = "update_interval"
CONF_PEAK_THRESHOLD = "peak_threshold"
CONF_PV_ENERGY_ENTITY = "pv_energy_entity"
CONF_PV_POWER_ENTITY = "pv_power_entity"
CONF_BATTERY_SOC_ENTITY = "battery_soc_entity"
CONF_BATTERY_CHARGE_POWER_ENTITY = "battery_charge_power_entity"
