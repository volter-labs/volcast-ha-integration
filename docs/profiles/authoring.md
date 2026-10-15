# Writing a brand profile

A profile is one JSON file that tells the integration how to find, read and (later) control a
hybrid-inverter family. It is data only: no code. The loader validates every profile with
`custom_components/volcast/core/profile_schema.py`; the error messages name the offending path
(`$.write.mode.encode`, ...). Read that file when this guide and the validator disagree.

## 1. Files

| What | Where |
|---|---|
| Profile | `custom_components/volcast/profiles/<id>.json` |
| Test registers (golden image) | `tests/golden/<id with "_">/registers.json` and a `README.md` |
| Profile test | `tests/core/test_profile_<id with "_">.py` |

`<id>` is kebab-case (`^[a-z0-9]+(-[a-z0-9]+)*$`), equals the file name, and equals `"id"` in
the file. Run `.venv/bin/python -m pytest tests/core/test_profile_schema.py -q`; it validates every
file in `profiles/` automatically.

## 2. Draft and verified

* `status: "draft"` — the profile is used for identification, reading, the Home Assistant entity
  mapping and a **read-only test connection** that shows what would be written. On the direct path a
  draft profile is offered once the inverter is identified and the register probe succeeds: the
  per-device verification ladder then starts with a read-only trial, and nothing is written until the
  owner allows control (the control write and the test window, below). The plan follows a draft
  profile only on a device the ladder has verified.
* `status: "verified"` — the ladder starts at the control write. A profile becomes verified only after
  a live trial on a real unit, and the top-level `status` and `modbus.status` are switched together.
* Every integration entry under `ha.integrations` carries its own `status` with the same meaning.
  Entity mode is offered only when both the profile and the integration entry are `verified`; a
  `draft` entry is only a hint for mapping readings, and the cloud is told that no setting can be
  controlled.

New profiles are always `draft` with a `status_note` (section 8).

### Unverified registers in a verified profile (`verify: true`)

A verified profile may gain counter or limit reads (section 3, `read`) before they are checked on a
unit. Each such entry carries `"verify": true` and a `note` naming its source. Its effect:

* the validator accepts the profile and reports a **warning**, not an error, for every `verify: true`
  entry of a `verified` profile (`profile_warnings` in `core/profile_schema.py`); in a `draft` profile
  everything is unverified anyway, so there is no warning;
* the register is read, sent to the cloud (counters, BMS limits) and reported as `register` in
  `driver.meta.readings_source` like any other reading — the owner sees the value, the app shows no
  separate per-reading badge, and the profile's own status (and the owner-facing control offer) does
  not change;
* it never influences a write: the flag is allowed only on informational keys, the write path never
  reads them, and the cloud cross-checks counters against the power average and clips limits to the
  rated power.

The flag is removed (set to `false` or dropped) only after the address, type and scale are checked on a
real unit with a read-only register dump against the inverter's own display or app.

### Where the verification ladder starts

The status only sets the rung where the per-device verification ladder starts:

* `draft` — the ladder starts at identification and reading and runs through the read-only trial
  (it counts the writes it would have made; nothing reaches the inverter during the trial). Entity mode
  stays closed to a `draft` profile or integration entry.
* `verified` — the ladder starts at the control write (the current value is re-written and confirmed
  by read-back).

An optional top-level `verification` block sets the ladder's parameters. Every key is optional and
must lie within its range:

All three values are whole numbers (integers; the validator rejects fractions and booleans), inclusive
at both ends:

* `trial_hours` — integer 1 to 72, the length of the read-only trial in hours.
* `window_minutes` — integer 5 to 60, the length of the short test window in minutes.
* `window_power_w` — integer 100 to 3000, the power of the test window in watts.

Vendor features the schema cannot express yet are listed in `engine-gaps.md`. For a profile that
depends on one of them, the ladder ends at the control write instead of the test window. Two rows were
added for this: "Test window without a guarded forced grid charge" and "Test window for time-window
profiles".

## 3. Top-level keys

All of these are required: `schema_version` (always `1`), `id`, `label`, `status`,
`control_model`, `sources`, `unit_id` (0-255), `transports`, `identify`, `read`, `write`,
`intents`, `baseline`, `write_policy`, `limits`, `ha`, `capabilities`, `modbus`.
Optional: `modes`, `neutral_mode`, `tou`, `status_note`. Unknown keys are rejected.

* `transports`: non-empty list from `goodwe_udp`, `solarman_v5`, `modbus_tcp`, `modbus_rtu`.
  Discovery tries `solarman_v5` when a logger serial is known, otherwise `goodwe_udp`, then
  `modbus_tcp`. `modbus_rtu` is never probed automatically (manual target only).
* `sources`: see section 5.

### Register specs

Used in `identify`, `read` and `limits`:

* `addr` 0-65535 and `type`: `u16`, `i16`, `u32`, `i32`, `f32`, `ascii` (`ascii` needs `len`,
  the number of registers, 1-64).
* Optional `scale`, `sign` (`-1` or `1`), `undef` (raw value meaning "not available"),
  `word_order` (`hi_lo` or `lo_hi`, for 32-bit types), `expect` (list of integers, identification
  only) and `fc` (read function code: `3` holding, the default, or `4` input). Holding and input
  registers are separate address spaces; use `fc: 4` for every register the vendor documents as
  input.
* A `read` entry may instead be `{"sum": [<register spec or {"ref": "<other read key>", "sign": -1}>, ...]}`.
  Cycles are rejected.
* Optional `note` (non-empty text, any register spec): where the address comes from and what is left
  to check. The code never reads it.
* Optional `verify` (boolean, default `false`) — only on the counter and limit keys of `read` (see
  below); anywhere else, including `sum` terms, `identify` and `write`, it is rejected. `verify: true`
  marks an address taken from a vendor map or library that has not been checked on a real unit yet.

### `read`

Only these keys: `soc`, `battery_temp_c`, `battery_voltage_v`, `battery_current_a`,
`battery_power_w`, `pv_power_w`, `grid_power_w`, `load_power_w`, `active_power_w`,
`pv_energy_total_kwh`, `grid_import_total_kwh`, `grid_export_total_kwh`, and the settings read-back
keys `mode_value`, `power_w`, `soc_min`, `soc_max`, `export_limit_w`, `export_limit_enabled`.
`tou_enabled` is allowed for time-window profiles and must use the address of `write.tou_enable`.
Sign conventions follow the existing profiles (`grid_power_w` positive = import); fix a
vendor's opposite convention with `sign`.

Counters and limits (optional; the same closed list as the cloud's reading-source map,
`EXTRA_KEYS` in `core/control/readings.py` — the validator reads it from there):

| Key | Unit | Meaning |
|---|---|---|
| `load_energy_total_kwh` | kWh | house consumption, lifetime total, only grows |
| `load_energy_day_kwh` | kWh | house consumption today, resets at the inverter's local midnight |
| `battery_charge_total_kwh` | kWh | energy into the battery, lifetime total |
| `battery_discharge_total_kwh` | kWh | energy out of the battery, lifetime total |
| `bms_charge_current_max_a` | A | charge current the BMS currently allows |
| `bms_discharge_current_max_a` | A | discharge current the BMS currently allows |
| `battery_capacity_kwh` | kWh | battery capacity |

The four energy counters go to the cloud in `extra.counters` of each reading (cloud telemetry contract for energy counters):
only keys with a valid value (finite, `>= 0`, below 10 GWh), never `0` or `null` in place of a missing
reading; no valid counter means no block, and the cloud then integrates the power average. The PV and
grid counters keep their own reading fields. The cloud cross-checks each counter against the power
average of the same window and falls back to the average when the scale is off (×10 and more), so a
wrong scale costs accuracy, not the energy balance.

These keys are informational: they never reach the write path (settings, read-back, guards, the sell
set-point) — only cloud accounting and the planner's limits. That is why `verify: true` is allowed
on them and nowhere else.

### `identify`

Exactly one of two ways, otherwise validation fails:

* text model: `model_register` (type `ascii`) plus a non-empty `model_regex` list, or
* numeric device type: a register in `identify.registers` with `expect: [codes...]` (typically
  named `device_type`).

`identify.registers` may also hold `serial` (used only for a salted fingerprint, never as the
model) and a rated-power register. `limits.rated_power_register` must name a key of
`identify.registers`; its value must fall in 1000-30000 W to be accepted.

### `write`

Keys: `soc_min`, `soc_max` (`encode: percent`), `power_w`, `export_limit_w` (`encode: watts`),
`export_limit_enabled` (`encode: bool`), `mode` (`encode: mode`). Each is
`{"addr", "type": "u16", "encode": ...}`; the encoding is fixed per key and writes are always one
unscaled 16-bit register.

### Control model

Pick one with `control_model`.

**`mode_setpoint`** (the inverter has a mode register plus numeric set-points)

* requires `write.mode`, `modes` (`{name: {"value", "direction", "ha_option"}}`, values unique,
  direction one of `charge`, `discharge`, `idle`, `neutral`), `neutral_mode` (a mode whose direction
  is `neutral` or `idle`) and `baseline: {"mode": <mode>, "export_limit_enabled"?: bool}`.
* all six intents are required, each `{"mode": <name in modes>, "power": ...}` with `power` one of
  `slot`, `slot_live_export`, `zero`, `none`; `slot*` only for `charge_grid`, `sell`, `discharge_forced`.
  `null` and missing intents are rejected. An unsupported intent is written as
  `{"mode": <neutral_mode>, "power": "none"}` with its capability set to `false` (the same thing
  the engine would fall back to).
* no `tou` block.

**`time_window`** (the inverter has a programmable time-of-use table)

* requires `write.tou_program` (`count`, and `start`/`power_w`/`soc` with encodes `hhmm`/`watts`/`percent`,
  `grid_charge` `{addr, bit}`), optional `write.tou_enable` (`addr`, `enable_bit`, `day_mask`), and
  a `tou` block: `programs` (1-12, equal to `tou_program.count`), `time_step_min` (divides 60),
  `soc_tolerance_pp`, `power_tolerance_w`, `field_order` (a permutation of the four program fields).
* `baseline: {"intent": "self_consume"}`; the `self_consume` intent is mandatory, the others are
  `{"grid_charge": bool, "soc": target_or_max|reserve|hold, "power": slot|max}` or `null` when
  unsupported. All six intent keys must be present.
* `capabilities.time_windows` equals `tou.programs`.

Intents are chosen from `charge_grid`, `charge_pv`, `discharge_forced`, `sell`, `self_consume`,
`standby`.

### `write_policy`, `limits`, `capabilities`

* `write_policy.order`: every written key exactly once (`tou` stands for the whole program table);
  `mode` must be last. Also `min_interval_s`, `nvm` (true when the register wears flash),
  `max_direction_changes_per_hour` (1-60 for `mode_setpoint`), `max_state_age_s` (> 0) and optional
  `nvm_budget`, `readback_settle_s` (0-10).
* `limits.battery_temp_c` `{min, max}` and `limits.rated_power_register`.
* Optional `limits.max_charge_register` (`"bms_charge_current_max_a"`), `limits.max_discharge_register`
  (`"bms_discharge_current_max_a"`) and `limits.capacity_register` (`"battery_capacity_kwh"`): each must
  name that key of `read`, and the two current limits also need `read.battery_voltage_v`. On the direct
  path the `driver.limits` block then carries `max_charge_w` / `max_discharge_w` = BMS current ×
  battery voltage, clipped to the rated power and rounded down to 100 W, and `battery_capacity_kwh`
  (a capacity the owner entered wins). The planner needs the battery's capability, not the BMS taper
  at a high state of charge, so a charge sample is taken only below 80 % SoC and a discharge sample
  only above 20 % SoC (unknown SoC: no sample), and the power sent is the peak of such samples over
  the last 24 hours. The peak lives in memory: after an HA restart, an options reload or a first
  pairing, the field is left out until the first sample inside the band, and the cloud uses its
  configured power meanwhile. Implausible readings
  are left out, never sent as `0`: current outside 1-1000 A, battery voltage outside 40-1000 V, power
  outside 100 W to 30 kW, capacity outside 0.5-200 kWh (`core/control/limits.py`). Without these
  fields the readings are still sent as counters/sources, but no limit is derived from them.
* `capabilities`: eight booleans (`force_charge_from_grid`, `sell_from_battery`, `force_discharge`,
  `standby`, `set_power_w`, `limit_export`, `set_soc_floor`, `set_soc_ceiling`) that must reflect
  what can really be written: `set_power_w` <=> a power write, `set_soc_floor` <=> `soc_min`,
  `set_soc_ceiling` <=> `soc_max`, `limit_export` <=> an export-limit write.

## 4. The two integration paths

A profile must make both paths work without a human in the loop.

**Entity mode (`ha.integrations`).** The user already runs a Home Assistant integration for the
brand. List one entry per integration: `domain`, `ems` (true when that integration runs its own
energy management, so the user must choose one controller), `status`, and `entities`:
`{key: {"domain", "unique_id_regex", "transform"?}}`. The `ha` entries are telemetry-mapping
hints until an integration entry is verified: entity mode is offered only for a `verified` entry of
a `verified` profile.

* When another profile lists the same integration `domain` (for example `solarman` or
  `solax_modbus`), add `model_regex`: a non-empty list of regexes searched in the Home Assistant
  device's "manufacturer model" text. Selection narrows the candidates by the brand in the
  manufacturer (the profile id up to the first `-`), then by this `model_regex`, then by
  `identify.model_regex` on the model; if more than one profile is still left, none is selected
  and the discovery sensor lists them in `profile_candidates`.

* Map **every** sensor key the integration exposes and every write key it offers as an entity
  (`mode`, set-points, `tou_1..tou_9` fields). The onboarding screen shows the user which entity
  stands in for which role, and the user can correct it; an incomplete map hides things.
* The entity domain is fixed per key: sensors `sensor`; `mode` `select`; `export_limit_enabled`
  `switch`; other write keys `number`; `tou_N_start` `time`, `tou_N_grid_charge` `switch`,
  `tou_N_power_w` and `tou_N_soc` `number`.
* `unique_id_regex` should be broad enough to match the usual unique ids across versions, but must
  compile. `transform` is `negate` or `invert_percent`.
* Every `*_regex` field of the profile (`identify.model_regex`, `ha.integrations[].model_regex`,
  `unique_id_regex` of entities and of `work_mode.ha`) runs in the Home Assistant event loop. It must
  stay inside a small regex subset, and an upper bound of its backtracking work on a 128-character
  text must stay under a limit chosen by measurement (every accepted pattern finishes `re.search`,
  `re.match` and `re.fullmatch` in a few milliseconds on hostile texts). Schema validation enforces
  it (`core/regex_safety.py`, the same rule as the Volcast profile gates):
  * printable ASCII only (0x20-0x7E), at most 200 characters, and it must compile;
  * allowed: literals; `\d \D \w \W \s \S` and a backslash before ASCII punctuation (`\. \- \( \\`);
    classes `[...]` with no `[` inside, no `]` first, no `--`, `&&`, `||`, `~~`, and ranges only
    between two literals in ascending order (a `-` right after a range is a literal); `.`; anchors
    `^ $ \b`; groups `(...)` and `(?:...)`; alternation `|`; groups nest at most 4 deep;
  * quantifiers `* + ? {n} {n,m}` (n <= m <= 16) only right after a literal, escape, class or `.`;
    after a group only `?` (an optional group). Lazy (`*?`) and possessive (`*+`) quantifiers,
    `{n,}`, `{,m}`, a quantifier after an anchor, `(`, `|`, the start or another quantifier, and an
    unescaped literal `{ } ]` are rejected;
  * `(?i)` only at the very start; lookbehind `(?<=...)`/`(?<!...)` only with 1-16 single
    characters, escapes or classes and no quantifier on it. Anything else after `(?` (lookahead,
    named groups, flags, comments, conditionals, atomic groups), backreferences and `\N \A \Z \x \u`
    are rejected;
  * every alternation branch and every group must contain a character-matching element: an empty
    branch (`(ab|)`, `(|)`, `()`, `(\b)`, a trailing `a|`) is `empty_branch` — write `(ab)?` instead;
    at most 8 `\b` and lookbehind assertions in one pattern (`too_many_assertions`; `^` and `$` do
    not count);
  * cost model: each atom has a character set (ASCII characters, plus "non-ASCII" for `.`, `\d \w \s`,
    their negations, negated classes and every letter under `(?i)`, plus "end of text" for `$` and
    the end of the pattern). A variable quantifier whose set cannot overlap what may follow it does
    not multiply the leaves, but each of its width (max - min + 1, at most 128) backtracking steps
    re-runs what follows up to the first required element, so it adds width x the cost of that
    prefix to the work; otherwise it multiplies the backtracking leaves by its width. A group whose branches start with disjoint sets counts as its largest branch, otherwise as
    the sum of its branches (plus one when optional). Work per leaf is the length of the pattern's
    elements. A pattern whose every top-level branch starts with `^` is tried once, otherwise at all
    129 positions. Starts x leaves x work may be at most 500000.
* `identify.model_regex` (needed for feed profiles; the packaged ones follow it too) is a list of
  patterns that each start with `^` (not `(?i)^`) and match neither an empty model nor any of `A`,
  `Q`, `z`, `0`, `7`, `-`, `QX`, `ZZZZZZZZ`, `Q0-Q0`, `00000000`, `xyz123`, `NOT-A-MODEL`;
  `identify.registers.device_type.expect` has at most 64 values and never 0 or 65535. A signed
  feed holds at most 500 profiles and 2 MiB; over that, clients reject it as a whole.

  A rejected pattern is reported as `niebezpieczny regex (<code>): <hint>`; the hint says how to fix
  it, e.g. `quantifier_after_group` → use a class like `[ab]+` instead of `(a|b)+`; `too_costly` →
  use `\d{1,3}` instead of `\d+` where the length is known and follow each `+`/`*` with a character
  it cannot match (`[0-9]+K`, not `.+K`); `too_costly_unanchored` → start the pattern with `^`;
  `repeat_too_large` → `{n,m}` with m <= 16 instead of `{n,}`. Identification problems use the codes
  `identify_not_anchored`, `identify_too_broad` and `identify_expect`.

  For feed profiles and in the generic profile test, a second guard also checks the parse tree Python
  `re` actually executes (only the subset's node kinds); if that private parser module is unavailable
  or returns a node it does not know, the feed profile is rejected. Packaged profiles are loaded with
  the text rule only, so they never depend on it. At run time every profile pattern is matched through
  `core/regex_safety.py` (`safe_search` / `safe_match`): a model, device text or unique id longer than
  128 characters never matches (it is not truncated), and such a device cannot be claimed by a feed
  profile instead of a packaged one.

  The cases shared with the Volcast profile gates, each rejected one with its code, are in
  `tests/fixtures/regex-safety.json` (the generic test reads the Volcast copy from
  `VOLCAST_PROFILES_DIR/fixtures/` when that is set).

**Direct mode (`modbus`).** No integration exists, so the integration talks to the inverter. The
whole block is required:

* `status`, `write_function` (`6` or `16`), `max_read_registers` (1-125).
* `transport_options`: for each transport used, `{port, timeout_ms (200-10000), gap_ms (0-2000)}`;
  names must appear in `transports`.
* `identify_reads`: non-empty list of `{addr, count, fc?}` blocks that cover every identification
  register.
* `probe_keys`: write keys (a subset of what the profile writes, no repeats) that the automatic
  check may try.
* `verify_blocks`: read blocks (`{addr, count}`) that together cover **every write address**;
  each block must include at least one write register. The automatic verification reads the
  register back through them after a write.
* `echo_only` (optional): write keys that can only be confirmed by the echo of the write because
  the register cannot be read.

The automatic verification ladder works per device: identify, read, dry-run ("what would be
written"), one reversible write of the current value with read-back, then a short low-power forced
window under the safety guards. `probe_keys` and `verify_blocks` are what it stands on, so fill
them completely. Say in `status_note` which write is a safe "re-write of the current value".

## 5. Sources

Every register address, scale and code must be traceable. Put a `sources` entry
`{"what": "<what this documents>", "ref": "<public URL>"}` for each public source (vendor
protocol document, open-source integration file). Take **facts** (addresses, scales, enum values),
never text or file structure copied from third-party repositories; respect their licences.
Unconfirmed facts belong in `status_note`, not silently in the profile.

## 6. Golden fixtures and the profile test

`tests/golden/<id_>/registers.json`:

```json
{"source": "synthetic, from the vendor protocol document",
 "profile": "<id>",
 "registers": {"<addr>": <u16>},
 "input_registers": {"<addr>": <u16>}}
```

`registers` is the holding space (FC 3); `input_registers` is optional and is the input space
(FC 4), a separate address space. Values are raw 16-bit words. Addresses are decimal strings.

* **Synthetic**: hand-made from documentation, with a fake serial. State this in the fixture's
  `README.md` ("synthetic until recorded") and give a plausible reference state.
* **Recorded**: captured from a real unit, scrubbed of serials and other identifying data (see
  `tests/golden/README.md`) and imported with the tools in `tools/golden/`.

Load it with `golden_image("<profile-id>")` from `tests/core/modbus/helpers.py`. The reusable
assertions are in `tests/core/profile_golden.py`:

* `assert_identify_matches(profile, image)` / `assert_identify_rejects(profile, image)` (the
  profile must not claim another brand's image);
* `decode_reads(profile, image)` returns every `read` key decoded through the core decoder;
  assert the expected reference values;
* `assert_intents_consistent(profile)`, `assert_ha_regexes_compile(profile)`,
  `assert_capabilities_match_writes(profile)`.

`tests/core/test_profile_template.py` shows the pattern; copy it into the profile's own test.

### Generated profiles: golden files beside the profile

A profile kept outside the package (for example a generated one, before it ships) carries its
golden data next to the profile file instead of under `tests/golden/`:

```
<dir>/<id>.json
<dir>/<id>.golden/registers.json   # or frames.json (recorded AA55 frames, GoodWe)
<dir>/<id>.golden/expected.json
<dir>/<id>.golden/README.md        # where the data comes from
```

`expected.json`:

```json
{"origin": "auto",
 "reference": {"<read key>": <value>},
 "intents": ["<intent>", ...],
 "modes": ["<mode>", ...]}
```

* `reference` is the state the golden image encodes, in core conventions (battery positive =
  discharge, grid positive = import). `null` is accepted only for a hand-written profile whose data
  is recorded frames or an image without a documented reference state; a generated profile always
  needs one.
* `intents` / `modes` list the profile's keys; they only check that the files agree with each
  other, they are not an independent oracle.
* `origin` is `auto` for a generated profile and `manual` for a hand-written one. The rules fail
  closed:
  * a profile whose id is not already packaged must have `origin: "auto"` (missing or `manual`
    fails);
  * an `auto` profile must stay `draft` (both `status` and `modbus.status`) and must have
    `registers.json` (recorded `frames.json` alone is not enough);
  * it must read at least one of `soc`, `battery_power_w`, `grid_power_w`, `pv_power_w`, and its
    `reference` must cover every one of them it reads, with non-zero battery and grid power, so a
    wrong sign cannot pass;
  * a packaged id may omit `origin` (hand-written); any other value than `auto`/`manual` fails.

`tests/core/test_profiles_generic.py` runs every `*.json` in the profile directory through the same
checks: schema validation, narrow identification (for every profile, generated or not: each
`identify.model_regex` starts with `^` and matches neither an empty model nor an arbitrary
unrelated model such as `A`, `0` or `NOT-A-MODEL`, so `^.` or `^SP1|.` fail; `device_type.expect`
holds no blank-register value `0`/`0xFFFF` and at most 64 values), identification of its own
golden image, rejection of every other profile's golden image (the packaged `tests/golden/*` images and the images beside the other
profiles in the directory), every `read` key decoding from its own image, the reference state
decoded key by key (floats with tolerance), intent consistency, capabilities matching the write
keys, HA regexes, public sources, the `origin` rules, and acceptance by the profile feed loader
(a profile this test passes can never make clients reject the signed feed). Point it at another directory with
`VOLCAST_PROFILES_DIR`:

```bash
VOLCAST_PROFILES_DIR=/path/to/profiles python -m pytest tests/core/test_profiles_generic.py -q
```

The variable is read by this test only; the integration always loads its packaged profiles.
Without the variable the test runs over the packaged profiles with their golden data from
`tests/golden/` (no `expected.json` there; the per-profile tests hold their reference states).
`schema-lite.json` in the directory is skipped. The per-profile tests stay: they cover what is
specific to each brand.

## 7. Engine limitations

If the vendor's control does not fit the schema (for example two mode registers, 32-bit or float
writes, scaled writes, dynamic scale factors, start/end slots with a current), do **not** work
around it in the profile. Mark the intent unsupported (section 3), set the capability to `false`, and describe the gap
(brand, what is needed, which registers) in `docs/profiles/engine-gaps.md`. A workaround that
writes the wrong thing to hardware is worse than a missing feature.

## 8. `status_note` checklist

For a draft, `status_note` (top-level, and `modbus.status_note`) lists what a live trial must
confirm:

* phase and voltage variant (single/three-phase, low/high-voltage battery) the addresses assume;
* word order of 32-bit values and the scale of each measurement;
* device-type codes or model strings seen, and which variants are not covered;
* the write function (`6` or `16`) and any register that needs another;
* which write is a safe re-write of the current value, and how to restore the previous state;
* anything guessed rather than taken from a source.
