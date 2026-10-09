# SolaX X1/X3-Hybrid test registers

**Synthetic until recorded from a live unit.** `registers.json` is a hand-made register image
consistent with the `solax-x-hybrid` profile and the community register maps cited in the
profile's `sources` (wills106 homeassistant-solax-modbus `plugin_solax.py`, evcc `solax.yaml`).
Nothing here was read from a real inverter. The serial at holding 0x0000-0x0006 is the fake
placeholder `H34A10FAKE0000`: the real prefix `H34` (X3-Hybrid G4, 10 kW per the community
prefix table) followed by a fake tail.

Addresses are wire addresses, equal to the vendor's hexadecimal register addresses (0x001C is
key `28`). `input_registers` holds the input space (function 4), `registers` the holding space
(function 3). 32-bit values are stored low word first. SolaX writes settings at other addresses
than it reads them back; the write register 0x001F is deliberately absent from the image.

Reference state, in register units:

| Value | Wire address | Raw | Decoded by the profile |
|---|---|---|---|
| Serial (model prefix) | 0-6 ascii (holding) | `H34A10FAKE0000` | model `H34A10FAKE0000` |
| Charger Use Mode (read-back) | 139 = 0x008B u16 (holding) | 0 | Self Use |
| Manual Mode (read-back) | 140 = 0x008C u16 (holding) | 0 | not read |
| Rated output power | 186 = 0x00BA u16 (holding) | 10000 | 10000 W |
| Inverter power | 2 = 0x0002 i16 (input) | 1700 | active power 1700 W |
| PV1 / PV2 power | 10, 11 = 0x000A, 0x000B u16 (input) | 1700, 1500 | PV 3200 W |
| Battery voltage | 20 = 0x0014 i16 (input) | 3000 (0.1 V) | 300 V |
| Battery current | 21 = 0x0015 i16 (input) | 50 (0.1 A, charging; sign assumed) | -5 A |
| Battery power | 22 = 0x0016 i16 (input) | 1500 (SolaX: > 0 = charging) | battery -1500 W (charging) |
| Battery temperature | 24 = 0x0018 i16 (input) | 24 (C) | 24 C |
| Battery SoC | 28 = 0x001C u16 (input) | 55 (%) | 55 % |
| Measured (feed-in) power | 70 = 0x0046 i32 (input) | -400 (SolaX: > 0 = exporting) | grid +400 W (import) |
| Grid export total | 72 = 0x0048 u32 (input) | 678900 (0.01 kWh) | 6789 kWh |
| Grid import total | 74 = 0x004A u32 (input) | 432100 (0.01 kWh) | 4321 kWh |
| Total solar energy | 148 = 0x0094 u32 (input) | 123456 (0.1 kWh) | 12345.6 kWh |

Balance: PV 3200 W + battery -1500 W + grid import 400 W = load 2100 W
(load = inverter power 1700 W + grid import 400 W).
