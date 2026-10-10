# Fronius GEN24 Plus test registers

**Synthetic until recorded from a live unit.** `registers.json` is a hand-made register image
consistent with the `fronius-gen24` profile and the public sources in the profile's `sources`
(the Fronius GEN24 Modbus TCP & RTU manual on manuals.fronius.com; SunSpec model definitions,
Apache-2.0; evcc `fronius-gen24.yaml`, MIT; public community configurations). Nothing here was
read from a real inverter. The model text `Symo GEN24 10.0 Plus` is a public product name; the
serial `FAKE0001FR` is a placeholder.

The image is the SunSpec model type **'int + SF'** on unit id 1. Addresses are wire addresses: the
vendor numbers registers from 1 and sends one less, so 'SunS' at 40000 is register 40001 and
StorCtl_Mod at 40348 is register 40349. Every register is a holding register (function 3), so the
image has no `input_registers`. Texts pack two ASCII characters per register, high byte first,
padded with zero bytes. Scale factors are signed 16-bit values (0xFFFE = -2). Model bodies that the
profile does not read (121, 122, 123) carry only their ID and length, the rest is zero.

Model chain: common 1 at 40002, inverter 103 at 40069 (length 50), nameplate 120 at 40121 (26),
basic settings 121 at 40149 (30), extended measurements 122 at 40181 (44), immediate controls 123
at 40227 (24), multiple MPPT 160 at 40253 (88 = four modules), basic storage controls 124 at
40343 (24), end block at 40369.

Reference state, in register units:

| Value | Wire address | Raw | Decoded by the profile |
|---|---|---|---|
| SunSpec marker | 40000-40001 | `SunS` | not read |
| Manufacturer / model / version | 40004 / 40020 / 40044 ascii | `Fronius` / `Symo GEN24 10.0 Plus` / `1.36.5-1` | model `Symo GEN24 10.0 Plus` |
| Serial | 40052-40067 ascii | `FAKE0001FR` | fingerprint only |
| Inverter model id / length | 40069 / 40070 | 103 / 50 | not read |
| AC power, SF | 40083 i16, 40084 | 1700, 0 | active power 1700 W (fixed SF 0) |
| Nameplate DERTyp / WRtg / WRtg_SF | 40123 / 40124 / 40125 | 82 (PV + storage) / 10000 / 0 | rating 10000 W |
| MPPT DCA_SF / DCV_SF / DCW_SF / DCWH_SF | 40255-40258 | -2 / -2 / 0 / 0 | DCW_SF not read (fixed SF 0) |
| MPPT module count | 40261 | 4 | not read |
| Module 1 `String 1` DC power | 40274 u16 | 2000 | PV = 2000 + 1200 = 3200 W |
| Module 2 `String 2` DC power | 40294 u16 | 1200 | (part of PV) |
| Module 3 `StCha 3` DC power (storage charging) | 40314 u16 | 1500 | battery -1500 W (charging) |
| Module 4 `StDisCha 4` DC power (storage discharging) | 40334 u16 | 0 | (part of battery power) |
| WChaMax, WChaMax_SF | 40345, 40361 | 5000 W, 0 | not read |
| StorCtl_Mod | 40348 | 0 (no limits) | mode value 0 |
| MinRsvPct, SF | 40350, 40364 | 500, -2 (5.00 %) | not read |
| ChaState, SF | 40351, 40365 | 5500, -2 (55.00 %) | SoC 55 % (fixed SF -2) |
| ChaSt | 40354 | 4 (charging) | not read |
| OutWRte / InWRte, InOutWRte_SF | 40355 / 40356, 40368 | 10000 / 10000, -2 (100 %) | not read |
| ChaGriSet | 40360 | 1 (grid) | not read |

Not in the image: the grid meter (unit id 200, model 203) and a battery temperature (no register on
unit 1). The reference grid import of 400 W and house load of 2100 W are therefore not decoded on
the direct path; they balance as PV 3200 W + battery -1500 W + grid 400 W = load 2100 W, and the
inverter output 1700 W = load 2100 W - grid 400 W.
