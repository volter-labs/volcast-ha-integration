"""Constants for the Volcast Solar Forecast integration."""

from __future__ import annotations

DOMAIN = "volcast"

DEFAULT_API_URL = "https://volcast.app/api/forecast"
DEFAULT_SUBMIT_URL = "https://volcast.app/api/submit-production"
DEFAULT_UPDATE_INTERVAL = 60  # minutes — server cache refreshes hourly, no value in polling more often
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

# Tryb wpisu — brak `mode` w entry.data (wpisy sprzed tej funkcji, np. 1.7.2) to
# klasyczny wpis prognozy z kluczem API; "discovery_only" to wpis bez konta,
# tylko wykrywanie instalacji.
CONF_MODE = "mode"
MODE_DISCOVERY_ONLY = "discovery_only"

SERVICE_SYNC_PRODUCTION = "sync_production"
SERVICE_RESUME_CONTROL = "resume_control"
ATTR_DATE = "date"

# Wykrywanie instalacji (tylko odczyt) — sygnał po nowym raporcie i limit czasu przebiegu
SIGNAL_DISCOVERY_UPDATED = "volcast_discovery_updated_{entry_id}"
DISCOVERY_TIMEOUT_S = 30

# Sterowanie i parowanie
CONF_BACKEND = "backend"
CONF_USER_ID = "user_id"
CONF_PAIRED_AT = "paired_at"
CONF_PAIRING = "pairing"
CONF_PAIRING_URL = "pairing_url"
OPT_CONTROL_MODE = "control_mode"
CONTROL_MODE_ENTITIES = "entities"
OPT_PROFILE_ID = "profile_id"
OPT_INVERTER_DOMAIN = "inverter_domain"
OPT_TELEMETRY_MAP = "telemetry_map"
OPT_GRID_NEGATE = "grid_power_negate"
OPT_RATED_POWER_W = "rated_power_w"
OPT_BATTERY_CAPACITY_KWH = "battery_capacity_kwh"
OPT_LOAD_ENERGY = "load_energy_entity"
OPT_PRICE_BUY = "entity_price_buy"
OPT_PRICE_SELL = "entity_price_sell"
OPT_PRICE_CURRENCY = "price_currency"
SIGNAL_CONTROL_UPDATED = "volcast_control_updated_{entry_id}"
# Zmiana stanu onboardingu sterowania (rekomendacja ścieżki, weryfikacja, konflikty) — blok `driver.control`.
SIGNAL_CONTROL_STATE_UPDATED = "volcast_control_state_updated_{entry_id}"
EXECUTOR_INTERVAL_S = 60
# Drabina weryfikacji urządzenia (`core/control/ladder.py`); profil może nadpisać blokiem `verification`.
VERIFY_TRIAL_HOURS = 24
VERIFY_WINDOW_MIN = 15
VERIFY_WINDOW_POWER_W = 500
# Okno próbne rusza tylko poniżej tego SoC — wymuszone ładowanie musi mieć miejsce w baterii.
VERIFY_WINDOW_MAX_SOC = 90.0
# Zgłoszenie w Naprawach po zatrzymaniu drabiny (`verification_stopped_<wpis>`).
ISSUE_VERIFICATION_STOPPED = "verification_stopped"
STOP_WRITE_TIMEOUT_S = 10.0
ERROR_ISSUE_AFTER = 10
# Pre-release backend; replaced on the stable release.
BETA_PAIRING_URL = "https://staging.volcast.app/functions/v1/pairing-session"

# Połączenie bezpośrednie z falownikiem (Modbus)
CONTROL_MODE_DIRECT = "direct"
OPT_DIRECT_TARGET = "direct_target"
OPT_DIRECT_TRIAL = "direct_trial"
OPT_DIRECT_POLL_S = "direct_poll_s"
DIRECT_POLL_S = 10
DIRECT_SLOW_POLL_S = 60
