# SolarEdge Home Hub / StorEdge test registers

**Synthetic until recorded from a live unit.** `registers.json` is a hand-made register image
consistent with the `solaredge-storedge` profile and the public sources in the profile's `sources`
(WillCodeForCats/solaredge-modbus-multi, Apache-2.0; evcc `solaredge-hybrid.yaml`, MIT; SunSpec
model definitions, Apache-2.0). Nothing here was read from a real inverter. The model text is the
public part number `SE10K-RWB48BFN4`; the serial `FAKE0001SE` is a placeholder.

Addresses are wire addresses: the SunSpec block starts at 40000 (tables numbered from 1 print
40001), and the storage and battery registers are their hexadecimal numbers (0xE004 = 57348,
0xE174 = 57716). Every register is a holding register (function 3), so the image has no
`input_registers`. Texts pack two ASCII characters per register, high byte first, padded with
zero bytes. SunSpec 32-bit values are high word first; storage and battery float32 values are
**low word first** (the lower address holds the low word).

Reference state, in register units:

| Value | Wire address | Raw | Decoded by the profile |
|---|---|---|---|
| SunSpec marker | 40000-40001 | `SunS` | not read |
| Manufacturer / model / version | 40004 / 40020 / 40044 ascii | `SolarEdge` / `SE10K-RWB48BFN4` / `0004.0020.0036` | model `SE10K-RWB48BFN4` |
| Serial | 40052-40067 ascii | `FAKE0001SE` | fingerprint only |
| Inverter model id (rated-power stand-in) | 40069 u16 | 103 | outside 1-30 kW: rating unknown |
| AC power, SF | 40083 i16, 40084 i16 | 17000, -1 | active power 1700 W (fixed scale 0.1) |
| DC power, SF | 40100 i16, 40101 i16 | 17000, -1 | DC 1700 W, PV = 1700 + 1500 charging = 3200 W |
| Meter 1 power, SF | 40206 i16, 40210 i16 | -4000, -1 (> 0 = export) | grid 400 W import |
| Meter 1 exported / imported, SF | 40226 / 40234 u32, 40242 | 6789000 / 4321000 Wh, 0 | 6789.0 / 4321.0 kWh |
| Export control mode / limit mode / site limit | 0xE000 / 0xE001 / 0xE002 f32 | 0 / 0 / 0.0 | not read |
| Storage control mode | 0xE004 u16 | 1 (Maximize Self Consumption) | mode value 1 |
| AC charge policy / limit, backup reserve | 0xE005, 0xE006 f32, 0xE008 f32 | 0, 0.0, 10.0 % | not read |
| Default mode / command timeout / command mode | 0xE00A, 0xE00B u32, 0xE00D | 7, 0, 7 | not read |
| Remote charge / discharge limit | 0xE00E / 0xE010 f32 | 0.0 / 0.0 | not read |
| Battery 1 average / max temperature | 0xE16C / 0xE16E f32 | 24.0 / 25.0 C | 24 C |
| Battery 1 voltage / current | 0xE170 / 0xE172 f32 | 52.0 V / 28.846 A | 52 V / current not read |
| Battery 1 power | 0xE174 f32 | 1500.0 (> 0 = charging) | battery -1500 W (charging) |
| Battery 1 state of energy | 0xE184 f32 | 55.0 % | 55 % |
| Battery 1 status | 0xE186 u32 | 3 (charging) | not read |

Power balance: PV 3200 W + battery -1500 W + grid 400 W = load 2100 W, and load 2100 W - grid
400 W = inverter output 1700 W.
