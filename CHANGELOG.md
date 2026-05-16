# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **Sparse-event hour falsely reports `actual_kwh=0`** — when an hour fires only ONE `state_changed` event for the configured energy entity (typical at dawn first ramp, dusk last ramp, or overnight midnight reset), the previous implementation set both `bucket.energy_start` and `bucket.energy_latest` to the same value, producing `delta=0`. The `0` then short-circuited the fallback method, so real production (often visible in concurrent power readings) was reported as zero and the calibration submission landed with bogus data.
  - `HourBucket` now tracks `energy_event_count` and `energy_start_carried` (set when the previous hour's last energy reading is carried over as this hour's start).
  - `_compute_energy` requires either `energy_start_carried` or `energy_event_count >= 2` before trusting method 1. Otherwise it falls through to method 2 (trapezoidal power integration).
  - `_async_check_flush` rollover path also carries `energy_latest` into the next bucket's `energy_start` (mirrors the existing carry-over in `_async_state_changed`), so the first event of the new hour is treated as a delta against the carried value.
  - 13 new tests in `tests/test_sparse_hour_delta.py` covering the dawn-edge case, the carried-over case, the existing multi-event case, counter resets, capacity glitches, bucket-metadata wiring, and the flush rollover carry-over.

### Notes

- Pure logic fix. No new dependencies, no API contract change. Backward compatible for the API — submissions still post the same JSON shape, just with a non-zero `actual_kwh` value where reality warrants.
