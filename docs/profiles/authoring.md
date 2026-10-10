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
  mapping and a **read-only test connection** that shows what would be written. It never writes
  to the inverter.
* `status: "verified"` — writes are allowed. A profile becomes verified only after a live trial on
  a real unit, and the top-level `status` and `modbus.status` are switched together.
* Every integration entry under `ha.integrations` carries its own `status` with the same meaning.
  Entity mode is offered only when both the profile and the integration entry are `verified`; a
  `draft` entry is only a hint for mapping readings, and the cloud is told that no setting can be
  controlled.

New profiles are always `draft` with a `status_note` (section 8).

### Where the verification ladder starts

The status only sets the rung where the per-device verification ladder starts:

* `draft` — the ladder starts at identification and reading and runs through the read-only trial
  (it counts the writes it would have made; nothing reaches the inverter).
* `verified` — the ladder starts at the control write (the current value is re-written and confirmed
  by read-back).

An optional top-level `verification` block sets the ladder's parameters. Every key is optional and
must lie within its range:

* `trial_hours` — 1 to 72, the length of the read-only trial.
* `window_minutes` — 5 to 60, the length of the short test window.
* `window_power_w` — 100 to 3000, the power of the test window.

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

### `read`

Only these keys: `soc`, `battery_temp_c`, `battery_voltage_v`, `battery_current_a`,
`battery_power_w`, `pv_power_w`, `grid_power_w`, `load_power_w`, `active_power_w`,
`pv_energy_total_kwh`, `grid_import_total_kwh`, `grid_export_total_kwh`, and the settings read-back
keys `mode_value`, `power_w`, `soc_min`, `soc_max`, `export_limit_w`, `export_limit_enabled`.
`tou_enabled` is allowed for time-window profiles and must use the address of `write.tou_enable`.
Sign conventions follow the existing profiles (`grid_power_w` positive = import); fix a
vendor's opposite convention with `sign`.

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
