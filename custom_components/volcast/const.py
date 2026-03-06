"""Constants for the Volcast Solar Forecast integration."""

from __future__ import annotations

DOMAIN = "volcast"

DEFAULT_API_URL = "https://jzihchpmkhawegqcfbeo.supabase.co/functions/v1/get-forecast-api"
DEFAULT_UPDATE_INTERVAL = 30  # minutes (API is cheap cache read)
DEFAULT_PEAK_THRESHOLD = 80  # percent of today's peak power

CONF_API_URL = "api_url"
CONF_UPDATE_INTERVAL = "update_interval"
CONF_PEAK_THRESHOLD = "peak_threshold"
