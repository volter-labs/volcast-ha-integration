# HAEO Compatibility Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make Volcast forecast sensors visible to HAEO's solar element forecast picker — both immediately (via Solcast-format bridge in this repo, v1.6.2) and long-term (via upstream PR adding a dedicated Volcast extractor to hass-energy/haeo).

**Architecture:**
- **Component A** — single additive change in `custom_components/volcast/sensor.py::_detailed_forecast()`: add `pv_estimate` key (kW) to every entry in `detailedForecast`. Makes HAEO's existing `solcast_solar` parser detect Volcast sensors. Shipped as 1.6.2-beta1 → customer test → 1.6.2.
- **Component B** — upstream PR to `hass-energy/haeo` adding `extractors/volcast.py` that detects Volcast natively by the unique `power_w`+`energy_wh` keys. Runs in parallel; independent of A's release.

**Tech Stack:** Python 3.12, pytest, Home Assistant custom integration patterns, HACS pre-release flow, GitHub PR workflow.

**Background reading:** Design rationale in `docs/plans/2026-05-30-haeo-compatibility-design.md`. Do not re-litigate scope — the design was reviewed and approved.

---

## Component A — Volcast HA integration (this repo)

Work happens on branch `claude/haeo-compat-pv-estimate` (already created off `main` from v1.6.1; the design doc commit is `4cb8447`).

### Task 1: Write failing test for `pv_estimate` field in detailedForecast

**Files:**
- Modify: `tests/test_sensor_attributes.py` — add a new test class at end of file

**Step 1: Read existing test patterns**

Run: read `tests/test_sensor_attributes.py` lines 1–260 to confirm:
- Test helper `_make_sensor()` exists
- `make_sample_data(include_detailed=True)` fixture pattern is the way to build a `VolcastData` with 5-min entries
- `TestDetailedHourlyAttribute` is the existing pattern for testing detailedHourly — mirror it

**Step 2: Append the new test class to `tests/test_sensor_attributes.py`**

```python
# ---------------------------------------------------------------------------
# NEW: pv_estimate field on detailedForecast — HAEO Solcast-extractor compat
# ---------------------------------------------------------------------------


class TestDetailedForecastPvEstimate:
    """Tests for the pv_estimate field (kW) added to each detailedForecast
    entry so HAEO's solcast_solar extractor recognises Volcast sensors.

    See docs/plans/2026-05-30-haeo-compatibility-design.md.
    """

    @patch("custom_components.volcast.sensor.datetime")
    def test_detailed_forecast_entries_have_pv_estimate(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 3, 20, 12, 0, tzinfo=TZ)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        data = make_sample_data(include_detailed=True)
        sensor = _make_sensor(VolcastEnergyTodaySensor, data)
        attrs = sensor.extra_state_attributes

        assert "detailedForecast" in attrs
        df = attrs["detailedForecast"]
        assert isinstance(df, list)
        assert len(df) > 0

        for entry in df:
            assert "pv_estimate" in entry, f"missing pv_estimate in {entry!r}"
            assert isinstance(entry["pv_estimate"], float)

    @patch("custom_components.volcast.sensor.datetime")
    def test_pv_estimate_equals_power_w_div_1000(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 3, 20, 12, 0, tzinfo=TZ)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        data = make_sample_data(include_detailed=True)
        sensor = _make_sensor(VolcastEnergyTodaySensor, data)
        df = sensor.extra_state_attributes["detailedForecast"]

        for entry in df:
            assert entry["pv_estimate"] == pytest.approx(
                entry["power_w"] / 1000.0, rel=1e-6
            ), f"pv_estimate {entry['pv_estimate']} != power_w/1000 for {entry!r}"

    @patch("custom_components.volcast.sensor.datetime")
    def test_existing_keys_still_present_regression(self, mock_dt):
        """Adding pv_estimate must not remove existing keys (backward compat
        for user automations / template sensors)."""
        mock_dt.now.return_value = datetime(2026, 3, 20, 12, 0, tzinfo=TZ)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        data = make_sample_data(include_detailed=True)
        sensor = _make_sensor(VolcastEnergyTodaySensor, data)
        df = sensor.extra_state_attributes["detailedForecast"]

        for entry in df:
            assert "period_start" in entry
            assert "power_w" in entry
            assert "energy_wh" in entry

    @patch("custom_components.volcast.sensor.datetime")
    def test_pv_estimate_on_tomorrow_sensor(self, mock_dt):
        """Tomorrow sensor uses the same _detailed_forecast() path —
        validate symmetry."""
        mock_dt.now.return_value = datetime(2026, 3, 20, 12, 0, tzinfo=TZ)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        # Sample data only has detailed entries for date_today by default.
        # Verify behavior on today sensor is enough for unit coverage;
        # tomorrow uses the identical helper.
        data = make_sample_data(include_detailed=True)
        sensor = _make_sensor(VolcastEnergyTomorrowSensor, data)
        attrs = sensor.extra_state_attributes
        # Tomorrow has no detailed entries in fixture → detailedForecast
        # should be absent (the code skips it when list is empty).
        assert attrs.get("detailedForecast", []) == [] or "detailedForecast" not in attrs
```

**Step 3: Run the new tests to verify they FAIL**

Run: `cd /c/Users/yakon/Documents/Volter/volcast-ha-integration && python -m pytest tests/test_sensor_attributes.py::TestDetailedForecastPvEstimate -v`

Expected: 3 of 4 tests FAIL with `AssertionError: missing pv_estimate in {...}` or similar. The 4th (tomorrow sensor — checks pv_estimate is absent because fixture has no tomorrow detailed entries) should PASS even without the implementation since it's checking the empty-list case.

If all four pass or all four fail unexpectedly, STOP — the fixture path is wrong or `make_sample_data` doesn't return detailed entries for `date_today=2026-03-20`. Re-check `make_sample_data` in `tests/conftest.py` lines 219–308.

**Step 4: Commit the failing test**

```bash
git add tests/test_sensor_attributes.py
git commit -m "test(sensor): failing tests for pv_estimate field on detailedForecast

Test requirements for HAEO solcast_solar extractor compatibility:
- detailedForecast entries must include pv_estimate (kW)
- pv_estimate == power_w / 1000.0
- existing keys (period_start, power_w, energy_wh) must remain

See docs/plans/2026-05-30-haeo-compatibility-design.md"
```

---

### Task 2: Implement `pv_estimate` in `_detailed_forecast()`

**Files:**
- Modify: `custom_components/volcast/sensor.py` — function `_detailed_forecast()`, currently at lines 146–173

**Step 1: Make the minimal change**

Locate the `result.append(...)` block inside `_detailed_forecast()`:

```python
result.append({
    "period_start": dt.isoformat(),
    "power_w": e.power_w,
    "energy_wh": e.energy_wh,
})
```

Replace with:

```python
result.append({
    "period_start": dt.isoformat(),
    "pv_estimate": e.power_w / 1000.0,  # kW — HAEO solcast_solar parser compatibility
    "power_w": e.power_w,
    "energy_wh": e.energy_wh,
})
```

**Step 2: Run new tests to verify they PASS**

Run: `python -m pytest tests/test_sensor_attributes.py::TestDetailedForecastPvEstimate -v`

Expected: all 4 tests PASS.

**Step 3: Run the entire test suite to verify no regressions**

Run: `python -m pytest tests/ -v`

Expected: all tests pass, same count as before plus the 4 new ones. If any pre-existing test fails, STOP and investigate — the change should be purely additive.

**Step 4: Commit the implementation**

```bash
git add custom_components/volcast/sensor.py
git commit -m "feat(sensor): add pv_estimate (kW) to detailedForecast entries

Make HAEO's solcast_solar extractor recognise Volcast forecast sensors
by including the pv_estimate key (Solcast convention, units kW) alongside
existing power_w / energy_wh keys. Purely additive — no breaking change
for existing automations or template sensors.

Resolves HAEO solar picker exclusion reported by lifetime customer.
See docs/plans/2026-05-30-haeo-compatibility-design.md."
```

---

### Task 3: Bump manifest to 1.6.2-beta1

**Files:**
- Modify: `custom_components/volcast/manifest.json` line 13

**Step 1: Edit manifest**

Change `"version": "1.6.1"` → `"version": "1.6.2-beta1"`.

**Step 2: Commit**

```bash
git add custom_components/volcast/manifest.json
git commit -m "chore(release): bump to 1.6.2-beta1 for customer pre-release test"
```

---

### Task 4: Push branch + create GitHub pre-release tag

**Step 1: Push the branch**

```bash
git push -u origin claude/haeo-compat-pv-estimate
```

**Step 2: Create the pre-release tag and GitHub pre-release**

```bash
gh release create v1.6.2-beta1 \
  --target claude/haeo-compat-pv-estimate \
  --title "v1.6.2-beta1 — HAEO compatibility (pre-release)" \
  --prerelease \
  --notes "$(cat <<'EOF'
Pre-release for customer testing — HAEO solar forecast picker compatibility.

## What changed
Volcast forecast sensors (Energy Today, Energy Tomorrow, Energy Day 3-7) now publish a `pv_estimate` field (kW) in each `detailedForecast` entry. This makes HAEO 0.3.3+ recognise Volcast as a valid solar forecast source via its existing Solcast-format detector.

## Why it's a pre-release
This change makes Volcast sensors appear in HAEO's solar element forecast picker. Before merging into main / shipping as v1.6.2, a real-world test with HAEO is needed.

## How to install via HACS
1. In HA, go to HACS → Integrations → Volcast → three-dots menu → "Redownload"
2. Tick "Show beta versions"
3. Select `v1.6.2-beta1` from the version dropdown
4. Restart Home Assistant

## What to verify
- HAEO config flow → Solar element → forecast picker now shows Volcast sensors
- Picking `sensor.volcast_energy_today` runs optimisation successfully
- HAEO sensor output reflects Volcast forecast curve (compare to Volcast sensor attribute `detailedForecast`)

## Rollback
If anything misbehaves, downgrade to v1.6.1 in HACS via the same dropdown.
EOF
)"
```

**Step 3: Verify the release exists**

```bash
gh release view v1.6.2-beta1
```

Expected: shows the pre-release with tag pointing at the latest commit on `claude/haeo-compat-pv-estimate`.

**STOP — handoff to customer.** Send the user the release URL plus the install instructions from the release notes. Wait for explicit confirmation that the customer has tested before continuing to Task 5.

---

### Task 5: Promote to v1.6.2 (after customer confirmation)

> **Only run this task after the user explicitly says the customer confirmed HAEO works with the pre-release.**

**Step 1: Bump manifest to final version**

Edit `custom_components/volcast/manifest.json` line 13: `"version": "1.6.2-beta1"` → `"version": "1.6.2"`.

**Step 2: Commit**

```bash
git add custom_components/volcast/manifest.json
git commit -m "chore(release): bump to 1.6.2 after customer HAEO pre-release verification"
```

**Step 3: Push**

```bash
git push
```

**Step 4: Open PR to main**

```bash
gh pr create --base main --title "feat: HAEO solar picker compatibility (v1.6.2)" --body "$(cat <<'EOF'
## Summary

- Adds `pv_estimate` (kW) field to each entry in `detailedForecast` on Volcast forecast sensors
- Makes HAEO 0.3.3+ recognise Volcast as a valid solar forecast source via its built-in Solcast-format detector
- Purely additive — existing `power_w` / `energy_wh` keys retained, no breaking change for user automations or template sensors

## Test plan

- [x] Unit tests in `tests/test_sensor_attributes.py::TestDetailedForecastPvEstimate` cover field presence, value correctness, and backward-compat key retention
- [x] Full pytest suite green
- [x] Pre-release `v1.6.2-beta1` verified by customer in real HAEO setup (lifetime user, HAEO 0.3.3 on Home Assistant)

## Design

See `docs/plans/2026-05-30-haeo-compatibility-design.md` and `docs/plans/2026-05-30-haeo-compatibility-plan.md`.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

**STOP — wait for user explicit instruction to merge.** Per repo CLAUDE.md, only merge to `main` on explicit user instruction.

**Step 5: After merge instruction — merge PR + tag final release**

```bash
gh pr merge --squash --delete-branch
git checkout main && git pull
gh release create v1.6.2 \
  --title "v1.6.2 — HAEO compatibility" \
  --notes "Adds pv_estimate field to detailedForecast for HAEO 0.3.3+ solar picker compatibility. Purely additive; no breaking changes. See PR for details."
```

---

## Component B — Upstream PR to `hass-energy/haeo`

Independent of A. Can start immediately in parallel; will be developed in a separate clone of HAEO outside this repo.

### Task 6: Fork and clone hass-energy/haeo

**Step 1: Fork via gh CLI**

```bash
gh repo fork hass-energy/haeo --clone=true --remote=true /c/tmp/haeo-fork
```

If the path already exists, choose an alternate dir under `/c/tmp/`. The remote `upstream` should point at `hass-energy/haeo` and `origin` at the fork.

**Step 2: Verify forked clone**

```bash
cd /c/tmp/haeo-fork && git remote -v
```

Expected: `origin` = fork, `upstream` = `hass-energy/haeo`.

**Step 3: Read HAEO repo conventions**

Read the following files from the cloned fork to learn its conventions before writing anything:

- `custom_components/haeo/data/loader/extractors/solcast_solar.py` (model for our new file)
- `custom_components/haeo/data/loader/extractors/__init__.py` (registration pattern)
- The HAEO test directory — find with: `find /c/tmp/haeo-fork -path '*test*solcast*' -type f`
- `CONTRIBUTING.md` if present at repo root
- `.github/workflows/*` for the CI shape (lint/test/etc.)

Capture the test path / test fixture pattern they use for `solcast_solar`. The new Volcast tests will mirror it exactly.

**Step 4: Create feature branch in fork**

```bash
git checkout -b feat/volcast-extractor
```

---

### Task 7: Write failing test for Volcast extractor

**Files:**
- Create: `<HAEO test path>/test_volcast.py` — mirror of `test_solcast_solar.py`

**Step 1: Read `test_solcast_solar.py` end-to-end** to understand what cases they cover (detect / extract / unit / device_class / edge cases).

**Step 2: Write `test_volcast.py`** covering at minimum:

- `test_detect_returns_true_for_valid_volcast_state` — state with `detailedForecast` containing entries that have `period_start` + `power_w` + `energy_wh` → True
- `test_detect_returns_false_for_solcast_state` — state with only `period_start` + `pv_estimate` (no `power_w`/`energy_wh`) → False
- `test_detect_returns_false_for_missing_attribute` — no `detailedForecast` attribute → False
- `test_detect_returns_false_for_empty_list` — `detailedForecast: []` → False
- `test_detect_returns_false_for_invalid_timestamp` — `period_start` not parseable → False
- `test_detect_returns_false_for_non_numeric_power_w` → False
- `test_extract_returns_timeseries_in_watts` — known input, verify output `(timestamp, power_w)` pairs, unit = `W`, device_class = `POWER`
- `test_extract_sorts_by_timestamp` — out-of-order entries → output sorted ascending
- `test_volcast_takes_precedence_over_solcast` — state containing BOTH `power_w` and `pv_estimate` (which is what Volcast v1.6.2+ publishes) → routed to Volcast parser, not Solcast (asserts via `extractors.extract(state)` returning W not kW)

Use the exact fixture style HAEO already uses in `test_solcast_solar.py`.

**Step 3: Run tests, verify they FAIL** with `ModuleNotFoundError` or `AttributeError` (Parser class doesn't exist yet).

Run: `pytest <HAEO test path>/test_volcast.py -v`

**Step 4: Commit failing tests**

```bash
git add <HAEO test path>/test_volcast.py
git commit -m "test: failing tests for volcast extractor"
```

---

### Task 8: Implement `extractors/volcast.py`

**Files:**
- Create: `custom_components/haeo/data/loader/extractors/volcast.py`

**Step 1: Write the file** by mirroring `solcast_solar.py` with Volcast-specific changes:

```python
"""Volcast solar forecast parser.

Volcast (https://volcast.app) exposes solar forecast data on its
energy_today / energy_tomorrow / energy_day_N sensors via a
`detailedForecast` attribute whose entries have:
  - period_start (ISO timestamp)
  - power_w (instantaneous power in W)
  - energy_wh (5-minute energy in Wh)
  - pv_estimate (power in kW — included for Solcast-format compatibility)

The combination of `power_w` AND `energy_wh` keys is unique to Volcast
and distinguishes it from Solcast (which only publishes pv_estimate).
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
import logging
from typing import Literal, Protocol, TypedDict, TypeGuard

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import UnitOfPower
from homeassistant.core import State

from .utils import is_parsable_to_datetime, parse_datetime_to_timestamp

_LOGGER = logging.getLogger(__name__)

Format = Literal["volcast"]
DOMAIN: Format = "volcast"


class VolcastForecastEntry(TypedDict, total=False):
    period_start: str | datetime
    power_w: float
    energy_wh: float
    pv_estimate: float  # optional — present for HAEO solcast_solar compat


class VolcastAttributes(TypedDict):
    detailedForecast: Sequence[VolcastForecastEntry]


class VolcastState(Protocol):
    attributes: VolcastAttributes


class Parser:
    """Parser for Volcast solar forecast data."""

    DOMAIN: Format = DOMAIN
    UNIT: str = UnitOfPower.WATT  # Volcast publishes power_w natively in W
    DEVICE_CLASS: SensorDeviceClass = SensorDeviceClass.POWER

    @staticmethod
    def detect(state: State) -> TypeGuard[VolcastState]:
        if "detailedForecast" not in state.attributes:
            return False

        detailed_forecast = state.attributes["detailedForecast"]
        if (
            not (isinstance(detailed_forecast, Sequence) and not isinstance(detailed_forecast, (str, bytes)))
            or not detailed_forecast
        ):
            return False

        return all(
            isinstance(item, Mapping)
            and "period_start" in item
            and "power_w" in item
            and "energy_wh" in item
            and isinstance(item["power_w"], (int, float))
            and isinstance(item["energy_wh"], (int, float))
            and is_parsable_to_datetime(item["period_start"])
            for item in detailed_forecast
        )

    @staticmethod
    def extract(state: VolcastState) -> tuple[Sequence[tuple[int, float]], str, SensorDeviceClass]:
        parsed: list[tuple[int, float]] = [
            (parse_datetime_to_timestamp(item["period_start"]), float(item["power_w"]))
            for item in state.attributes["detailedForecast"]
        ]
        parsed.sort(key=lambda x: x[0])
        return parsed, Parser.UNIT, Parser.DEVICE_CLASS
```

**Step 2: Run only the detect / extract unit tests** (not the precedence test yet — that needs the registration in Task 9):

Run: `pytest <HAEO test path>/test_volcast.py -v -k "not precedence"`

Expected: those tests PASS. Precedence test still FAILS until registration done in Task 9.

**Step 3: Commit**

```bash
git add custom_components/haeo/data/loader/extractors/volcast.py
git commit -m "feat(extractors): add volcast parser

Volcast solar forecast integration publishes detailedForecast attributes
with period_start + power_w + energy_wh per entry. This parser detects
that shape (unique signature: power_w AND energy_wh keys present, which
no other current extractor uses) and emits a forecast timeseries in W."
```

---

### Task 9: Register Volcast parser in `extractors/__init__.py`

**Files:**
- Modify: `custom_components/haeo/data/loader/extractors/__init__.py`

**Step 1: Add the import**

In the existing multi-import line (currently `from . import aemo_nem, amber2mqtt, amberelectric, emhass, flow_power, haeo, open_meteo_solar_forecast, solcast_solar`), add `volcast`:

`from . import aemo_nem, amber2mqtt, amberelectric, emhass, flow_power, haeo, open_meteo_solar_forecast, solcast_solar, volcast`

**Step 2: Add `volcast.Format` to `ExtractorFormat` union and `type[volcast.Parser]` to `DataExtractor` union.**

**Step 3: Add to `FORMATS` dict**: `volcast.DOMAIN: volcast.Parser,`

**Step 4: Add to `extract()` detection chain — BEFORE `solcast_solar`**

In the long `elif` chain inside `extract()`, add this branch immediately before the `solcast_solar.Parser.detect(state)` branch:

```python
elif volcast.Parser.detect(state):
    data, unit, device_class = volcast.Parser.extract(state)
```

Order matters: after A ships, Volcast `detailedForecast` entries will contain BOTH `power_w` AND `pv_estimate`. Without Volcast-first ordering, the `solcast_solar` parser would match Volcast sensors and emit kW instead of W. Volcast-first keeps the native unit correct.

**Step 5: Run the full HAEO test suite**

Run: `pytest -v` (in the HAEO repo root)

Expected: all tests pass — including the precedence test from Task 7.

**Step 6: Run any linting/type checks the HAEO repo defines** (check `.github/workflows/` from Task 6 step 3 for the exact commands — likely `ruff check`, `mypy`, similar).

**Step 7: Commit**

```bash
git add custom_components/haeo/data/loader/extractors/__init__.py
git commit -m "feat(extractors): register volcast parser before solcast_solar

Volcast sensors carry both power_w (Volcast-native) and pv_estimate
(Solcast-compat) in detailedForecast entries. Ordering Volcast before
Solcast in the detection chain keeps the native W unit + POWER class
when both parsers could match."
```

---

### Task 10: Push fork branch + open upstream PR

**Step 1: Push to fork**

```bash
git push -u origin feat/volcast-extractor
```

**Step 2: Open PR against `hass-energy/haeo` `main`**

```bash
gh pr create --repo hass-energy/haeo \
  --base main \
  --head <fork-owner>:feat/volcast-extractor \
  --title "Add Volcast solar forecast extractor" \
  --body "$(cat <<'EOF'
## Summary

Adds a new extractor for the [Volcast](https://volcast.app) solar forecast integration. Volcast publishes detailed (5-minute) forecast data via the `detailedForecast` state attribute on its energy forecast sensors, with each entry containing `period_start`, `power_w`, and `energy_wh`.

## Why

Without this extractor, the default fallback path reads Volcast's energy forecast sensors as scalar kWh totals, which fails HAEO's solar element unit filter (`UnitOfPower`) so the sensors never appear in the solar forecast picker.

Volcast already publishes a `pv_estimate` field (kW) inside `detailedForecast` for compatibility with the existing `solcast_solar` extractor, so this PR is not strictly required for Volcast to work in HAEO. But:

- Without this extractor, Volcast appears to HAEO as a "Solcast-format" source (wrong identity, wrong unit — kW instead of native W).
- The unique combination of `power_w` + `energy_wh` keys lets us identify Volcast cleanly and emit its native W unit.

## What's in this PR

- `extractors/volcast.py` — Parser class mirroring `solcast_solar.py` structure
- Registration in `extractors/__init__.py`, ordered **before** `solcast_solar` in the detection chain so Volcast sensors (which carry both `power_w` and `pv_estimate`) are correctly identified as Volcast
- Tests covering detect/extract paths and Volcast-precedence-over-Solcast

## Volcast format reference

```json
{
  "detailedForecast": [
    {"period_start": "2026-05-30T08:00:00+02:00", "power_w": 1234.5, "energy_wh": 102.9, "pv_estimate": 1.2345},
    {"period_start": "2026-05-30T08:05:00+02:00", "power_w": 1345.7, "energy_wh": 112.1, "pv_estimate": 1.3457}
  ]
}
```

Source: `custom_components/volcast/sensor.py::_detailed_forecast()` in https://github.com/volter-labs/volcast-ha-integration

## Testing

- New `test_volcast.py` covers all detect cases (valid Volcast, Solcast-shaped, missing attribute, empty list, invalid types) and extract path (timeseries shape, ordering, unit, device class)
- Precedence test verifies that a state carrying both Volcast and Solcast keys is parsed as Volcast (not Solcast)
- Full repo test suite green
EOF
)"
```

**Step 3: Capture PR URL and report to user.**

Run: `gh pr view --json url --jq .url` and surface the URL.

**STOP — handoff complete.** B is in HAEO maintainers' hands now. No further action in this plan.

---

## Cross-component completion criteria

Plan is fully complete when ALL of the following hold:

1. v1.6.2 tagged + released on `volter-labs/volcast-ha-integration` (Component A merged to `main`)
2. Upstream PR open at `hass-energy/haeo` (Component B handoff done; merge depends on their maintainers)
3. Customer confirms HAEO solar picker shows Volcast sensors and HAEO optimisation consumes the forecast
4. Design doc + plan doc both committed in the repo at `docs/plans/2026-05-30-haeo-compatibility-*.md`

## What NOT to do (out of scope)

- Do NOT add `state_class=TOTAL` to energy sensors in this plan — separate hygiene fix, separate PR. Mentioned only to explicitly exclude it.
- Do NOT add a new "Forecast Power" sensor (option C from brainstorm).
- Do NOT modify Volcast backend, `get-forecast-api`, `_shared/solar/`, or the Volcast app — zero backend changes are required.
- Do NOT bump version past `1.6.2` in this plan. Any further hygiene fixes ship in `1.6.3+`.
