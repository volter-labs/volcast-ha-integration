# Golden fixtures

One directory per profile, named after the profile id with `-` replaced by `_`
(`deye-sg` -> `deye_sg/`).

## Register images

`registers.json` is the image the profile tests decode:

```json
{"source": "...", "profile": "<id>",
 "registers": {"<addr>": <u16>},
 "input_registers": {"<addr>": <u16>}}
```

* `registers`: holding registers (read function 3); keys are decimal addresses, values raw
  16-bit words.
* `input_registers`: optional, input registers (read function 4). This is a separate address
  space, the same address in both maps is two different registers. Omit it for profiles that only
  use holding registers.
* Load with `golden_image("<profile-id>")` in `tests/core/modbus/helpers.py`.

The reference implementation fixtures in `goodwe_et/` use recorded frames instead
(`frames.json`) plus vectors for the control logic.

## Synthetic and recorded

* **Synthetic**: written by hand from vendor documentation, with placeholder serials. The
  directory's `README.md` must say so. Replace it with a recording once a real unit is available.
* **Recorded**: captured from a real inverter and scrubbed before it is committed.

## Scrubbing rules

Enforced by `tests/core/test_golden_files.py`: no real serial numbers (fake serials only, the
frame CRCs stay valid), no UUIDs, and no readable text in recorded responses other than the model
and firmware strings the tests list as known and non-identifying.
