# GoodWe ET golden vectors

Generated from the reference executor implementation (C) — do not edit by hand.
Regenerate: build `tools/golden/export_vectors.c` in the firmware repo, then run
`tools/golden/import_box_fixtures.py`. `frames.json` holds Modbus frames recorded on a
GW8KN-ET (serial number replaced, CRC recomputed); `plan_live.json` is a real
`get-schedule` document with its id replaced.

The import script refuses to write when the decoded frame bytes or the JSON text
contain anything that looks like a GoodWe serial number or a UUID.

## Entry added by hand: `work_mode_47000`

The `work_mode_47000` entry in `frames.json` is NOT produced by the generator: it was read live from a GW8KN-ET
with `tools/live/read_registers.py` (three identical reads, serial-free frame) and appended manually. Regenerating
`frames.json` from the firmware repo drops it; re-add it afterwards.

- Register 47000, one word, value `0` = General mode (work mode as in the goodwe library, `et.py`:
  `Integer("work_mode", 47000, ...)`; OperationMode GENERAL=0, ECO=3, PEAK_SHAVING=4, ...). Measured on a GW8KN-ET.
- Register 35187 is a different register (runtime state), not the configured work mode.
