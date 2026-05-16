# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`sensor.volcast_forecast_age` — wall-clock age of the server's last forecast generation, in minutes.**
  - Diagnostic sensor (`EntityCategory.DIAGNOSTIC`), `state_class=measurement`, unit `min`.
  - Computes `now() - generated_at` locally on every read, with a 1-min self-refresh timer so the value advances independent of the integration's poll interval (default 30 min).
  - Distinguishes **server-side staleness** from local integration issues. The existing `cache_age_minutes` attribute on the API status sensor is the server's self-reported value and only refreshes when the integration polls; this new sensor surfaces the real wall-clock age between polls.
  - Returns `None` (unavailable) when `generated_at` is missing, empty, or unparseable.
  - Defensive: handles ISO with `Z` suffix, ISO with `+00:00` offset, and naive datetimes (assumed UTC). Future timestamps (server clock ahead of ours) clamp to 0 instead of producing negative values.
  - Two attributes: `generated_at` (passed through from the API) and `server_reported_cache_age_minutes` (for cross-check against the server's own claim).
  - 15 new tests in `tests/test_forecast_age_sensor.py` cover the fresh / stale / Z-suffix / naive / future / empty / missing / malformed cases plus `async_setup_entry` registration and self-refresh timer registration / teardown.
- New `forecast_age` entry in `strings.json` and `translations/en.json`.
- README entry under "Sensors" documenting the new entity.

### Fixed

- **Default poll interval inconsistency** — README claimed 60 minutes in two places; `const.py:DEFAULT_UPDATE_INTERVAL = 30` is the actual default. README aligned to match code.
- **Test infrastructure**: `tests/conftest.py` now stubs `UnitOfTime` and `CALLBACK_TYPE`; `_FakeCoordinatorEntity` gains `async_added_to_hass` / `async_will_remove_from_hass` / `async_write_ha_state` so parent-class `super()` calls don't AttributeError in tests for the new self-refresh wiring.

### Notes

- Pure addition. No behavioural change to existing sensors. No new dependencies, no API contract change.
- The 1-min self-refresh cadence matches the integer resolution of the sensor (unit = minutes), so every published change actually represents a 1-tick advance.
