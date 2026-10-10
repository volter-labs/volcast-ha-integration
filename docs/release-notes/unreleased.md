# Unreleased — next pre-release (v2.0.0b7)

This is a **pre-release** building on v2.0.0b6. Nothing in it changes how the GoodWe direct control works, and the new brands below are read-only.

## What's New

### Reading input registers

- The Modbus core can now read input registers (function code 4), not only holding registers. Brands that report their measurements on input registers can be read directly.
- GoodWe and Deye are unchanged.

### Eight draft brand profiles

Each profile is marked **draft**. They are available for the read-only test connection (**Installation details → Read-only test connection**), and their `ha` entries are only hints for mapping the readings of an existing integration: the integration's control through Home Assistant entities (entity mode) is not offered for any of them until that integration entry is verified, and the cloud is told that no setting can be controlled. When several profiles describe the same integration (`solarman`, `solax_modbus`), the device's manufacturer and model text decides between them; if it does not, no profile is selected and the discovery sensor lists the candidates in `profile_candidates`:

- Huawei SUN2000 (L1/M1, through an SDongle over Modbus TCP) — `huawei-sun2000`
- Sungrow SH (WiNet-S, Modbus TCP) — `sungrow-sh`
- SolaX X1/X3-Hybrid G3/G4 (Modbus TCP or RTU) — `solax-x-hybrid`
- Sofar HYD 5-20KTL-3PH (Solarman logger or RTU) — `sofar-hyd`
- Solis hybrid S5/S6-EH1P and RHI (Solarman stick or RTU) — `solis-hybrid`
- FoxESS H1-G2/AC1-G2/P1 (RS485 gateway; readings through `foxess_modbus` only) — `foxess-h`
- SolarEdge StorEdge and Home Hub (Modbus TCP, port 1502) — `solaredge-storedge`
- Fronius GEN24 (Modbus TCP) — `fronius-gen24`

### Documentation for contributors

- `docs/profiles/authoring.md` — a guide for writing a brand profile.
- `docs/profiles/engine-gaps.md` — the vendor control features that the profile schema and the control engines cannot express yet, with the brands they affect and what the core would need.

## Direct control stays disabled for the new brands

Nothing is written to the inverters of the eight brands above. Control for any of them needs a live verification on the inverter first, and until then only the read-only test connection is available.

## Known Limitations

- SunSpec scale factors are fixed assumptions in the draft Fronius and SolarEdge profiles; an inverter that reports under another scale factor shows values that are off by a factor of 10 or more.
- Some brands (FoxESS) have no serial or rating register, so they cannot be identified on the direct path; until the `foxess_modbus` integration entry is verified they get readings only, no control.
- The known limitations of v2.0.0b6 still apply.

## Upgrading

Update via HACS and restart Home Assistant. No writes start with this version.
