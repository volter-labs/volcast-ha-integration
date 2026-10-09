# Huawei SUN2000 test registers

**Synthetic until recorded from a live unit.** `registers.json` is a hand-made holding-register
image (address → 16-bit word) consistent with the `huawei-sun2000` profile and the huawei-solar library
(`registers.py`) and its wlcrs wiki (sources in the profile). Nothing here was read from a real inverter. The model string is
`SUN2000-5KTL-L1` and the serial at 30015–30024 is the fake placeholder `HWFAKESERIAL0000`.

Reference state, in register units (32-bit values big-endian, high word first):

| Value | Register | Raw | Decoded by the profile |
|---|---|---|---|
| Rated power | 30073 u32 | 5000 | 5000 W |
| PV input power | 32064 i32 | 3200 | 3200 W |
| Inverter active power | 32080 i32 | 1700 | 1700 W |
| Meter active power | 37113 i32 | -400 (importing; Huawei: > 0 = feeding the grid) | grid +400 W (import) |
| Battery charge/discharge power | 37765 i32 | +1500 (charging; Huawei: > 0 = charging) | battery -1500 W (charging) |
| Load | 32080 + grid import | — | 2100 W |
| SoC | 37760 u16 ×0.1 | 550 | 55 % |
| Battery temperature | 37022 i16 ×0.1 | 240 | 24 °C |
| Battery bus voltage | 37763 u16 ×0.1 | 4500 | 450 V |
| Storage working mode | 47086 u16 | 2 | maximise self consumption |
