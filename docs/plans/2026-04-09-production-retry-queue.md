# Production Submission Retry Queue — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Persist failed hourly production readings and retry them on the next successful submission cycle, so DNS timeouts / network errors don't cause permanent data loss.

**Architecture:** Use HA's `homeassistant.helpers.storage.Store` for a JSON-backed persistent queue. On each flush, load queued readings, combine with current reading, submit as a single batch. On success clear queue; on failure append to queue. Max 48 entries (FIFO).

**Tech Stack:** Python 3.12, Home Assistant helpers (Store, aiohttp, events), pytest

---

### Task 1: Add HA stubs for production.py testing

**Files:**
- Modify: `tests/conftest.py`

**Step 1:** Add missing stubs to conftest.py:
- `STATE_UNAVAILABLE = "unavailable"` and `STATE_UNKNOWN = "unknown"` to `homeassistant.const`
- `homeassistant.helpers.storage.Store` — fake class with `async_load()` / `async_save()` / `async_remove()`
- `homeassistant.helpers.event` — `async_track_state_change_event` and `async_track_time_interval` as MagicMock
- `homeassistant.helpers.aiohttp_client` — `async_get_clientsession` as MagicMock

**Step 2:** Verify existing tests still pass: `pytest tests/ -v`

**Step 3:** Commit: `test: add HA stubs for production tracker testing`

---

### Task 2: Write failing tests for retry queue

**Files:**
- Create: `tests/test_production.py`

Tests to write:
1. `test_successful_submit_clears_queue` — submit succeeds, queue is empty afterward
2. `test_failed_submit_queues_reading` — submit raises exception, reading saved to Store
3. `test_queued_readings_sent_with_next_flush` — queued readings combined with new reading in single POST
4. `test_queue_max_48_fifo` — when queue exceeds 48, oldest entries dropped
5. `test_rate_limit_429_queues_reading` — 429 response queues the reading (not lost)
6. `test_queue_persists_across_tracker_restart` — stop/start tracker, queue survives via Store

**Step:** Run `pytest tests/test_production.py -v` — all should FAIL (no queue logic yet)

**Step:** Commit: `test: add failing tests for production retry queue`

---

### Task 3: Implement retry queue in production.py

**Files:**
- Modify: `custom_components/volcast/production.py`

Changes:
1. Import `Store` from `homeassistant.helpers.storage`
2. Add constants: `STORAGE_KEY = "volcast_production_queue"`, `STORAGE_VERSION = 1`, `MAX_QUEUE_SIZE = 48`
3. In `__init__`: create `self._store = Store(hass, STORAGE_KEY, private=True, minor_version=STORAGE_VERSION)`; init `self._queue: list[dict] = []`, `self._queue_loaded = False`
4. Add `async _async_load_queue()`: lazy-load from Store on first use, set `_queue_loaded = True`
5. Add `async _async_save_queue()`: save `self._queue` to Store (or remove file if empty)
6. Modify `_async_check_flush()`: after building reading, call `_async_load_queue()`, prepend queued to readings list
7. Modify `_async_submit()` to return `bool` (success/failure)
8. In `_async_check_flush()`: if submit succeeded → clear queue + save; if failed → append current reading to queue (FIFO cap at 48) + save
9. Add `@property queued_count -> int` for sensor exposure

**Step:** Run `pytest tests/test_production.py -v` — all should PASS

**Step:** Commit: `feat: add persistent retry queue for production submission`

---

### Task 4: Expose queued_readings in sensor diagnostic

**Files:**
- Modify: `custom_components/volcast/sensor.py`

**Step:** In `VolcastApiStatusSensor.extra_state_attributes`, add `queued_readings` attribute reading from `tracker.queued_count`

**Step:** Commit: `feat: expose queued_readings count on api_status sensor`

---

### Task 5: Run full test suite + code review

**Step:** Run `pytest tests/ -v` — all green
**Step:** Spawn code review agent to review all changes against this plan
