# SOFAR HYD 5-20KTL-3PH test registers

**Synthetic until recorded from a live unit.** `registers.json` is a hand-made register image
consistent with the `sofar-hyd` profile and the sources cited in the profile's `sources` (the
vendor G3 Modbus protocol, three-phase edition; davidrapan ha-solarman and StephanJoubert
home_assistant_solarman `sofar_g3hyd.yaml`; wills106 homeassistant-solax-modbus
`plugin_sofar.py`; evcc `sofarsolar-g3.yaml`). Nothing here was read from a real inverter. The
serial at holding 0x0445-0x044C is the fake placeholder `SP1ES110FAKE0000`: the real prefix
`SP1` (HYD xxKTL-3PH in the community prefix table) followed by a fake tail.

Addresses are wire addresses, equal to the vendor's hexadecimal register addresses (0x0608 is
key `1544`). All registers are holding registers (function 3), so the image has no
`input_registers`. 32-bit values are stored high word first (lower address = high word).

Reference state, in register units:

| Value | Wire address | Raw | Decoded by the profile |
|---|---|---|---|
| Serial (model prefix) | 1093-1100 = 0x0445-0x044C ascii | `SP1ES110FAKE0000` | model `SP1ES110FAKE0000` |
| Inverter output power | 1157 = 0x0485 i16 | 170 (0.01 kW) | active power 1700 W |
| PCC (grid) power | 1160 = 0x0488 i16 | -40 (0.01 kW; Sofar: > 0 = selling) | grid +400 W (import) |
| System load power | 1199 = 0x04AF u16 | 210 (0.01 kW) | load 2100 W |
| Total PV power | 1476 = 0x05C4 u16 | 32 (0.1 kW) | PV 3200 W |
| Battery 1 voltage | 1540 = 0x0604 u16 | 4000 (0.1 V) | 400 V |
| Battery 1 current | 1541 = 0x0605 i16 | 375 (0.01 A; > 0 = charging) | -3.75 A |
| Battery 1 power | 1542 = 0x0606 i16 | 150 (0.01 kW; Sofar: > 0 = charging) | battery -1500 W (charging) |
| Battery 1 temperature | 1543 = 0x0607 i16 | 24 (C) | 24 C |
| Battery 1 SoC | 1544 = 0x0608 u16 | 55 (%) | 55 % |
| PV generation total | 1670-1671 = 0x0686 u32 | 123456 (0.1 kWh) | 12345.6 kWh |
| Energy bought total | 1678-1679 = 0x068E u32 | 43210 (0.1 kWh) | 4321 kWh |
| Energy sold total | 1682-1683 = 0x0692 u32 | 67890 (0.1 kWh) | 6789 kWh |
| Rated power | 1773 = 0x06ED u16 | 100 (0.1 kW) | 10000 W |
| Energy Storage Mode | 4368 = 0x1110 u16 | 0 | Self Use |

Balance: PV 3200 W + battery -1500 W + grid import 400 W = load 2100 W
(inverter output 1700 W = load 2100 W - grid import 400 W).
