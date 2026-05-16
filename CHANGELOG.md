# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Comprehensive observability logging across coordinator and production tracker.
  - Coordinator emits DEBUG at refresh start/end with timing, server `generated_at`, and `cache_age_minutes`.
  - Coordinator emits WARNING when server-side cache is stale (`cache_age_minutes >= 180`) — clearly attributes the staleness to the server, not the integration.
  - Production tracker emits DEBUG on every flush tick with full state (current/previous bucket hours, queue depth, last-flushed hour).
  - Production tracker emits WARNING with bucket detail when a flush is skipped due to missing or insufficient data — previously these paths returned silently and looked identical to "tracker dead."
  - Production tracker emits INFO immediately before a POST fires, naming the hour, kWh, method, peak power, and queue depth.
  - `_compute_energy` emits DEBUG showing which branch ran (`energy_delta`, `power_average`, or `no method viable`).
- Test coverage: 10 new tests in `tests/test_observability_logging.py` verifying each log path.

### Notes

- No behavioural changes in this release. All additions are observability-only.
- Pre-existing test failure in `tests/test_sensor_attributes.py::test_api_status_attributes_unchanged` (FakeHass missing `.data` attribute) is **not** addressed in this PR. Will be fixed in a later PR that updates the production-tracker state model.
