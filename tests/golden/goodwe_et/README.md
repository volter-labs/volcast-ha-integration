# GoodWe ET golden vectors

Generated from the reference executor implementation (C) — do not edit by hand.
Regenerate: build `tools/golden/export_vectors.c` in the firmware repo, then run
`tools/golden/import_box_fixtures.py`. `frames.json` holds Modbus frames recorded on a
GW8KN-ET (serial number replaced, CRC recomputed); `plan_live.json` is a real
`get-schedule` document with its id replaced.

The import script refuses to write when the decoded frame bytes or the JSON text
contain anything that looks like a GoodWe serial number or a UUID.
