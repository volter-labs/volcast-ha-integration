# Sungrow SH test registers

**Synthetic until recorded from a live unit.** `registers.json` is a hand-made register image
consistent with the `sungrow-sh` profile and the community register maps cited in its
`sources` (mkaiser Sungrow-SHx Home Assistant package, SunGather, evcc). Nothing here was read from
a real inverter. The device type code is SH10RT (`0x0E03`) and the serial at 4989-4998 is the fake
placeholder `SGFAKESERIAL0000`.

Addresses are wire addresses: the register number minus 1 as in the community register maps (device type code 5000 is key
`4999`; numbering convention community-reported). `input_registers` holds the input space (function 4), `registers` the holding space
(function 3). 32-bit values are stored low word first.

Reference state, in register units:

| Value | Wire address | Raw | Decoded by the profile |
|---|---|---|---|
| Device type code | 4999 u16 (input) | 3587 (0x0E03, SH10RT) | model "3587" |
| Nominal output power | 5000 u16 (input) | 100 (0.1 kW) | 10000 W |
| Total DC (PV) power | 5016 u32 (input) | 3200 | 3200 W |
| Battery power | 5213 i32 (input) | -1500 (charging; Sungrow: < 0 = charging) | battery -1500 W (charging) |
| Power flow status | 13000 u16 (input) | 0x2B: PV, charging, load, importing | not read |
| Total PV generation | 13002 u32 (input) | 123456 (0.1 kWh) | 12345.6 kWh |
| Load power | 13007 i32 (input) | 2100 | 2100 W |
| Export power | 13009 i32 (input) | -400 (importing; Sungrow: > 0 = exporting) | grid +400 W (import) |
| Battery voltage | 13019 u16 (input) | 4000 (0.1 V) | 400 V |
| Battery power, unsigned (old register) | 13021 u16 (input) | 1500 | not read |
| SoC | 13022 u16 (input) | 550 (0.1 %) | 55 % |
| Battery temperature | 13024 i16 (input) | 240 (0.1 C) | 24 C |
| Total import energy | 13036 u32 (input) | 43210 (0.1 kWh) | 4321 kWh |
| Total export energy | 13045 u32 (input) | 67890 (0.1 kWh) | 6789 kWh |
| EMS mode | 13049 u16 (holding) | 0 | self-consumption |
| Charge/discharge command | 13050 u16 (holding) | 0xCC (stop, the default) | not read |
| Charge/discharge power | 13051 u16 (holding) | 0 | not read |

Balance: PV 3200 W + battery -1500 W + grid import 400 W = load 2100 W.
