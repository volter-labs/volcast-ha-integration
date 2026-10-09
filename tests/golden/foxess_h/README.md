# FoxESS H1-G2 test registers

**Synthetic until recorded from a live unit.** `registers.json` is a hand-made register image
consistent with the `foxess-h` profile and the public sources in the profile's `sources`
(nathanmarlor/foxess_modbus register definitions and wiki, MIT; evcc `fox-ess-h1.yaml`, MIT).
Nothing here was read from a real inverter. The model text is the placeholder `H1-5.0-E-G2`; the
H1-G2 map has no serial register.

Addresses are wire addresses and equal the decimal register numbers foxess_modbus sends (31024 is
sent as 31024). Every register is a holding register (function 3), so the image has no
`input_registers`. The model text packs two ASCII characters per register, high byte first, padded
with spaces. 32-bit totals are stored high word first (lower address = high word).

Reference state, in register units:

| Value | Wire address | Raw | Decoded by the profile |
|---|---|---|---|
| Model text | 30000-30014 ascii | `H1-5.0-E-G2` + spaces | model `H1-5.0-E-G2` |
| Inverter (AC) power | 31008 i16 | 1700 (W; < 0 = charging from AC) | active power 1700 W |
| Grid CT | 31014 i16 | -400 (W; > 0 = feed-in) | grid 400 W import |
| Load power | 31016 i16 | 2100 (W) | load 2100 W |
| Inverter / ambient temperature | 31018 / 31019 i16 | 350 / 280 (0.1 C) | not read |
| Battery power | 31022 i16 | -1500 (W; > 0 = discharging) | battery -1500 W (charging) |
| Battery temperature | 31023 i16 | 240 (0.1 C) | 24 C |
| Battery SoC | 31024 u16 | 55 (%) | 55 % |
| BMS charge rate (rated-power stand-in) | 31025 i16 | 250 (0.1 A) | 25 A, outside 1-30 kW: rating unknown |
| Solar energy total | 32000-32001 u32 | 123456 (0.1 kWh) | 12345.6 kWh |
| Feed-in energy total | 32009-32010 u32 | 67890 (0.1 kWh) | 6789.0 kWh |
| Grid consumption energy total | 32012-32013 u32 | 43210 (0.1 kWh) | 4321.0 kWh |
| PV1 / PV2 power (low words) | 39280 / 39282 i16 | 2000 / 1200 (W) | PV 3200 W |
| Work mode | 41000 u16 | 0 (Self Use) | mode value 0 |
| Charge periods 1-2 | 41001-41006 | 0 (disabled) | not read |
| Min SoC (off-grid) | 41009 u16 | 10 (%) | not read |
| Max SoC | 41010 u16 | 100 (%) | soc_max 100 % |
| Min SoC (On Grid) | 41011 u16 | 15 (%) | soc_min 15 % |

Power balance: PV 3200 W + battery -1500 W + grid 400 W = load 2100 W, and load 2100 W - grid
400 W = inverter output 1700 W.
