# Solis hybrid test registers

**Synthetic until recorded from a live unit.** `registers.json` is a hand-made register image
consistent with the `solis-hybrid` profile and the community sources in the profile's `sources` (wills106 homeassistant-solax-modbus `plugin_solis.py`,
Pho3niX90 solis_modbus, davidrapan ha-solarman and StephanJoubert home_assistant_solarman
`solis_hybrid.yaml`, fboundy ha_solis_modbus, evcc `solis-hybrid*.yaml`). Nothing here was read
from a real inverter. The serial at input 33004-33011 is the fake placeholder `110F12FAKE000000`:
the real prefix `110F` (single-phase Gen5 5 kW 48 V hybrid in the solax_modbus prefix table)
followed by a fake tail.

Addresses are wire addresses and equal the vendor's decimal register numbers (the cited
integrations send them unchanged: register 33139 is sent as 33139). `input_registers` holds the input space
(function 4), where Solis keeps identification and measurements; `registers` holds the holding
space (function 3) with the storage control switch. 32-bit values are stored high word first
(lower address = high word).

Reference state, in register units:

| Value | Wire address | Raw | Decoded by the profile |
|---|---|---|---|
| Serial (model prefix) | 33004-33011 ascii (input) | `110F12FAKE000000` | model `110F12FAKE000000` |
| Total generation | 33029 u32 (input) | 12345 (kWh) | 12345 kWh |
| Battery temperature (BMS) | 33043 i16 (input) | 240 (0.1 C) | 24 C |
| Total DC (PV) power | 33057 u32 (input) | 3200 (W) | PV 3200 W |
| Inverter active power | 33079 i32 (input) | 1700 (W) | active power 1700 W |
| Rated power candidate (undocumented by public sources) | 33100 i32 (input) | 5000 (W) | rated power 5000 W |
| Storage control switch, read-only mirror | 33132 u16 (input) | 1 | not read |
| Battery voltage | 33133 u16 (input) | 512 (0.1 V) | 51.2 V |
| Battery current | 33134 i16 (input) | 293 (0.1 A; > 0 = charging assumed, unverified) | -29.3 A |
| Battery current direction | 33135 u16 (input) | 0 (charging; 1 = discharging) | not read |
| Battery SoC | 33139 u16 (input) | 55 (%) | 55 % |
| House load power | 33147 u16 (input) | 2100 (W) | load 2100 W |
| Battery power | 33149 i32 (input) | 1500 (W; > 0 = charging) | battery -1500 W (charging) |
| Total energy imported | 33169 u32 (input) | 4321 (kWh) | 4321 kWh |
| Total energy exported | 33173 u32 (input) | 6789 (kWh) | 6789 kWh |
| Meter total active power | 33263 i32 (input) | -400 (W; > 0 = export, as evcc reads it) | grid +400 W (import) |
| Energy storage control switch | 43110 u16 (holding) | 1 (bit 0 only: Self-Use, no grid charging) | self_use |

Balance: PV 3200 W + battery -1500 W + grid import 400 W = load 2100 W
(inverter active power 1700 W = load 2100 W - grid import 400 W). Battery 51.2 V x 29.3 A = 1500 W.
