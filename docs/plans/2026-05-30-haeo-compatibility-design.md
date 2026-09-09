# HAEO Compatibility — Design

**Date:** 2026-05-30
**Status:** approved (brainstorming → ready for write-plan)
**Trigger:** Lifetime customer reported HAEO (Home Assistant Energy Optimiser, v0.3.3) cannot "see" Volcast forecast sensors in its solar picker, only `Power Now`. Solcast forecast sensors work for them in HAEO.

## Problem

HAEO does not use HA's generic `device_class` / `state_class` entity filter for forecast sources. Instead, `data/loader/extractors/__init__.py` runs a chain of per-integration parsers (`solcast_solar`, `open_meteo_solar_forecast`, `haeo`, `emhass`, ...). Each parser:

1. `detect(state)` — checks state-attribute shape unique to that integration
2. `extract(state)` — returns `(timeseries, unit, device_class)`

The parser's returned `unit` becomes the entity's `unit_of_measurement` in HAEO's inclusion map — overriding the sensor's declared unit. HAEO's solar element filters entities by `UnitOfPower` (kW/W), so any sensor whose chain returned `kW` or `W` shows up.

For Volcast sensors:
- `solcast_solar.detect()` requires `state.attributes["detailedForecast"]` with each item containing `period_start` AND `pv_estimate`. Volcast publishes `period_start` + `power_w` + `energy_wh` — **missing `pv_estimate`** → detect returns False.
- No other parser matches.
- Fallback reads `state.attributes["unit_of_measurement"]` = `kWh` for energy sensors → does not match `UnitOfPower` → **excluded from picker**.
- `Power Now` declares unit `W` → matches `UnitOfPower` → appears in picker, but only as a scalar reading (no forecast curve).

## Decision

Ship two changes in parallel:

- **A (this repo, ships as v1.6.2):** Add `pv_estimate` (in kW) to every entry produced by `_detailed_forecast()` in `sensor.py`. Existing keys (`power_w`, `energy_wh`) remain untouched. This makes HAEO's existing `solcast_solar` parser match Volcast, so all forecast sensors appear in the picker with full forecast curve consumed.
- **B (upstream PR to `hass-energy/haeo`):** Add a dedicated `extractors/volcast.py` mirroring `solcast_solar.py`, detecting Volcast by the unique `power_w`/`energy_wh` keys. Lets Volcast stop riding on Solcast's coattails long-term; independent of A's release cycle.

A is a 1-line additive change; B is the cleanest long-term home. Together they give the customer a working fix today AND a clean upstream story.

### Alternatives considered

- **B only.** Cleanest but blocks customer for unknown HAEO release cycle. Customer is non-technical, cannot drop a file into `custom_components/haeo/extractors/` as a stopgap. Rejected.
- **HAEO native format** — add `forecast: [{time, value}]` + matching `unit_of_measurement` on a new POWER sensor. Bigger surface, new entity ID, collides with existing `forecast` attribute on `energy_today`. Rejected — adds nothing A+B don't already cover.
- **Add `state_class=TOTAL` to energy sensors** — does not help HAEO at all (HAEO ignores state_class). Has independent value for HA Energy Dashboard / hassfest hygiene but is unrelated to this brief. Not in scope.

## Consequences

- **A makes Volcast's `detailedForecast` look like Solcast's to any consumer that detects by `pv_estimate` presence.** That is intentional for HAEO; any other Solcast-detecting community add-on would treat Volcast the same way. Treated as a feature, not a regression — Volcast still publishes its own `power_w`/`energy_wh` alongside.
- **Zero impact on existing Volcast users not on HAEO.** Atrybut `detailedForecast` is already `_unrecorded_attributes`, so no DB / recorder change. Adds ~25 bytes per entry × ~576 entries (today + tomorrow at 5-min granularity) ≈ ~12 KB extra in HA state memory per sensor. Negligible.
- **Zero impact on Volcast backend / `get-forecast-api`.** No API contract changes. Coordinator parses the same payload.
- **B makes Volcast a first-class integration in HAEO.** Once merged + released by HAEO maintainers, Volcast appears in their docs / config flow as a known solar source.

## Components

### Component 1 — `pv_estimate` field (Volcast HA integration, v1.6.2)

**Files modified:**
- `custom_components/volcast/sensor.py` — `_detailed_forecast()` method only
- `tests/test_sensor_attributes.py` — add regression test for new field
- `manifest.json` — version bump `1.6.1` → `1.6.2-beta1` → `1.6.2`

**Change:** Each entry in the returned list gets one extra key, `"pv_estimate": e.power_w / 1000.0`. Solcast convention (kW). Existing `power_w` (W) and `energy_wh` (Wh) keys retained for backward compatibility with any user automations / template sensors.

**Behavior:**
- Volcast `Energy Today` / `Energy Tomorrow` / `Energy Day 3-7` sensors expose `detailedForecast` with `pv_estimate` per entry.
- HAEO's `solcast_solar.Parser.detect()` returns True for these sensors.
- HAEO assigns unit = kW, device_class = POWER to them in its inclusion map.
- They appear in HAEO's Solar element forecast picker.
- HAEO consumes the timeseries for optimization.

### Component 2 — Volcast extractor (upstream PR to `hass-energy/haeo`)

**Files added (in fork of `hass-energy/haeo`):**
- `custom_components/haeo/data/loader/extractors/volcast.py` — mirror of `solcast_solar.py`
- Tests under HAEO's test layout (TBD — check their repo conventions)

**Files modified (in fork):**
- `custom_components/haeo/data/loader/extractors/__init__.py` — import + add to `FORMATS` + add to `extract()` detection chain

**Detection key:** `state.attributes["detailedForecast"]` exists AND each item has `period_start` AND `power_w` AND `energy_wh`. The combination of `power_w` (W, not kW) + `energy_wh` is unique to Volcast — does not collide with Solcast (`pv_estimate` only) or any other current extractor.

**Detection order:** Volcast parser tried BEFORE Solcast in the chain. Once A ships, Volcast sensors also contain `pv_estimate`, so without order discipline the Solcast parser would match them first. Volcast-first ensures correct identification long-term.

**Returned values:**
- `unit` = `UnitOfPower.WATT` (Volcast publishes `power_w` natively in W)
- `device_class` = `SensorDeviceClass.POWER`
- `timeseries` = list of `(timestamp, power_w)` tuples

## Data flow

```
Volcast backend  →  get-forecast-api edge function  →  HA coordinator  →  VolcastData  →  sensor.extra_state_attributes
                                                                                              │
                                                                                              ▼
                                                                              detailedForecast = [{period_start, pv_estimate, power_w, energy_wh}, ...]
                                                                                              │
                                              ┌───────────────────────────────────────────────┴─────────────────────┐
                                              ▼ (current HAEO 0.3.3)                                                ▼ (HAEO after B merged)
                                solcast_solar.Parser.detect → True                                volcast.Parser.detect → True (tried first)
                                returns (timeseries_kW, "kW", POWER)                              returns (timeseries_W, "W", POWER)
                                              │                                                                    │
                                              └───────────────────────────────┬────────────────────────────────────┘
                                                                              ▼
                                                              EntityMetadata(unit_of_measurement = kW or W)
                                                                              ▼
                                                              matches UnitOfPower filter in build_inclusion_map
                                                                              ▼
                                                              Appears in HAEO Solar element forecast picker
```

## Testing strategy

**Unit (this repo):**
- `tests/test_sensor_attributes.py` — new test asserting:
  - Each entry in `detailedForecast` contains `pv_estimate` key
  - Value is `float`
  - Value equals `power_w / 1000.0` to 6 decimal places
  - Existing `power_w`, `energy_wh`, `period_start` keys still present (regression)
- Apply to `EnergyToday`, `EnergyTomorrow`, and at least one `EnergyDay` sensor.

**Manual end-to-end (NAS HA at 100.88.64.93):**
- Install 1.6.2-beta1 via HACS (point HACS at feature branch / pre-release tag)
- Restart HA
- Open HAEO config flow → Solar element → verify Volcast sensors appear in forecast picker
- Pick Volcast Energy Today, save, run HAEO optimization
- Verify HAEO sensor output uses Volcast forecast (compare optimized power schedule shape to Volcast forecast curve)

**Customer test (paying lifetime user):**
- Push customer a HACS pre-release / direct download instructions
- Customer installs, confirms HAEO sees Volcast sensors
- Customer runs their HAEO setup end-to-end
- Wait 3-5 days for stability confirmation
- Only then: tag 1.6.2, release on HACS

**Upstream PR (B):**
- Add tests in HAEO's existing test layout — at minimum a copy of `test_solcast_solar.py` patterns with Volcast fixture
- CI green on HAEO repo
- Maintainer review

## Rollback

- **A:** Revert single commit, tag 1.6.3 — no data loss, no user-visible breakage. Anyone who started using HAEO with Volcast will see their Volcast entries disappear from HAEO picker on next HA restart, but no errors / no DB corruption.
- **B:** PR not merged → nothing to roll back. If merged and breaking, HAEO maintainers handle their own release.

## Success criteria

- HAEO solar forecast picker lists all 7 Volcast forecast sensors (Energy Today, Tomorrow, Day 3-7)
- HAEO optimization run uses Volcast forecast curve (verifiable in HAEO debug output)
- Existing Volcast unit tests still pass (no regression in attribute shapes)
- No new warnings in `home-assistant.log` from Volcast or HAEO
- Customer confirms HAEO setup works end-to-end with Volcast as the solar source
- (B success: PR accepted by HAEO maintainers, Volcast extractor in next HAEO release)

## Out of scope

- `state_class=TOTAL` on energy sensors (separate hygiene fix, unrelated to HAEO)
- New "Forecast Power" sensor (Option C from brainstorm — not needed)
- HACS pre-release tooling changes (use existing flow)
- Backend / Supabase changes (none required)
