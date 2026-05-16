# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Persistent diagnostic counters** — `submissions_today`, `last_submission_time`, and `calibration` (Kalman bias) now survive HA restarts and integration reloads via a new `volcast_tracker_state` Store. Previously these reset to zero/None every time the user reloaded the integration, making the `api_status` sensor attributes useless as continuous health indicators.
  - New `STATE_STORAGE_KEY` / `STATE_STORAGE_VERSION` constants in `production.py`.
  - `_async_load_state()` runs at `async_start` BEFORE timers/listeners fire, so the sensor reads correct values immediately after restart (not zeros for the first poll cycle).
  - `_async_save_state()` runs after every successful POST (alongside the existing `_async_save_queue()` call).
  - Day rollover handled on load: if the stored date is not today's local date, `submissions_today` resets to 0 while `last_submission_time` and `calibration` persist (they remain useful across days).
  - Defensive against malformed stored state (non-dict, invalid ISO timestamps, malformed `submissions_today` casts, storage I/O errors during load).
  - 12 new tests in `tests/test_tracker_state_persistence.py` covering save, load, round-trip across reload, day rollover, malformed inputs, I/O errors during load, and idempotency.

### Fixed

- **Pre-existing test failure**: `tests/test_sensor_attributes.py::test_api_status_attributes_unchanged` was failing because (a) `FakeHass` lacked a `.data` attribute and (b) the `_make_sensor` helper bypassed `__init__` so `_entry_id` was missing. Both addressed. `FakeHass.data` is initialised per-instance via `__init__` (avoids shared-class-dict cross-test pollution); helper sets `sensor._entry_id`.

### Notes

- The new state Store is independent of the existing retry-queue Store (`volcast_production_queue`). Failures to load/save state are warnings only — they do not break submission flow.
- Backward compatible: tracker works correctly on a fresh install where the state Store is empty.
- No new dependencies, no API contract change.
