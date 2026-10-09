# Engine gaps

Vendor control features that the profile schema and the control engines cannot express today.
Profiles do not work around them: the intent is marked unsupported and the capability stays
`false` (see `authoring.md`, section 7). Each row names what the core would need.

## Core

| Gap | Brand(s) | What the core would need |
|---|---|---|
| `mode_setpoint` requires all six intents | all `mode_setpoint` profiles | Optional intents with an engine fallback to the neutral mode; until then an unsupported intent is written as neutral mode + power `none` with its capability `false`. |
| Capabilities are global, not per path | huawei-sun2000 | Per-path capabilities: a setting the Home Assistant integration can write (e.g. a SoC limit through a number entity in %) may be impossible on the direct register path. |
| One `unit_id` per profile | huawei-sun2000 | A unit id per transport or connection path (the same inverter answers on different ids depending on how it is reached). |
| No post-connect quiet period | huawei-sun2000 | A per-transport delay between opening the connection and the first request (`gap_ms` only spaces consecutive requests). |
| Entity mode is matched only by an integration domain in `INVERTER_DOMAINS` | sungrow-sh (community Modbus YAML package) | Selecting YAML `modbus`/`template` packages, which have no config entry and no device (a manual pick of the package, then matching by unique id); a `mode` key whose select comes from the `template` platform while the sensors come from `modbus` (today one entry = one platform). |
| No absolute-value transform for registers some firmware reports negative | huawei-sun2000 (grid export total 37119) | An `abs` (or unsigned-from-signed) option on read specs; until then such registers are not read. |

## Huawei (`huawei-sun2000`)

| Gap | Registers | What the core would need |
|---|---|---|
| Prerequisite (conditional) registers before the mode write | 47246 setting mode (0 time / 1 SoC), then 47083 duration in minutes or 47101 target SoC, then 47100 forcible charge/discharge (0 stop / 1 charge / 2 discharge) | A write sequence where the mode write depends on prerequisite registers written first, with their own read-back. |
| Two power registers, chosen by direction | 47247 forcible charge power, 47249 forcible discharge power | A power set-point whose register depends on the mode direction. |
| 32-bit writes | 47247 / 47249 (u32 W) | `u32`/`i32` write encodings (two registers, word order). |
| Scaled SoC writes | 47081 charge cut-off, 47082 discharge cut-off, 47101 target SoC (gain 10, 0.1 %) | A scale on write encodings (`percent` × 10). |
| Export limit needs 32-bit writes | 47415 active power control mode plus 47416 maximum feed-in power (i32 W) | `u32`/`i32` write encodings and a companion mode register for the limit. |
| Installer login before writes over TCP | vendor login (challenge/response) on most TCP connections; re-login after reconnect | An authenticated session step in the transport, with stored installer credentials. |
| Post-connect quiet period on the SDongle | about 1 s after connect | See the core row above. |
| Unit id differs per path | SDongle and RS485: 1; inverter access point / Local O&M (port 6607 on newer firmware): 0 | See the core row above. |

## Sungrow (`sungrow-sh`)

Addresses are wire addresses (vendor register number minus 1).

| Gap | Registers | What the core would need |
|---|---|---|
| Prerequisite (conditional) register before the command write | 13049 EMS mode must be 2 (compulsory) before 13050 charge/discharge command (0xAA charge / 0xBB discharge / 0xCC stop) takes effect; leaving forced control needs 0xCC then EMS mode 0 | A mode made of two registers written in order, each with its own read-back. Writing 13049 = 2 alone is not a safe standby: it starts whatever command 13050 holds. |
| Power set-point valid only in the forced mode, unit varies by model | 13051 charge/discharge power (W, up to the battery converter rating at 5627; per the community map an older vendor sheet gave % for some models) | The prerequisite-register support above, plus a per-model unit check before a power write. |
| Scaled SoC writes | 13057 max SoC (50.0-100.0 %), 13058 min SoC (0.0-50.0 %), both 0.1 % | A scale on write encodings (`percent` x 10). |
| Export limit needs an enable register with vendor codes | 13086 feed-in limitation (0xAA enable / 0x55 disable) before 13073 feed-in limitation value (W) applies | An enable encoding with configurable on/off values (the `bool` encoding writes 1/0). |
| Charge/discharge power caps in 0.01 kW | 33046 max charge power, 33047 max discharge power (used by community integrations to hold the battery) | A scale on write encodings. |
| External EMS mode needs a heartbeat | 13049 = 3 (external EMS) or 4 (VPP) falls back to self-consumption unless 13079 heartbeat is re-written within its timeout | A periodic keep-alive write declared by the profile. |
| Forced-charging periods are split hour/minute registers | 33207 enable (0xAA/0x55), 33208 weekday/everyday, then per period start hour, start minute, end hour, end minute, target SoC (33209-33218) | A time-window table with start/end slots in separate hour and minute registers (the `time_window` model has one `hhmm` start per program). |
| Battery power sign from a state register on older firmware | 13021 battery power is unsigned; the direction is bit 1 (charging) / bit 2 (discharging) of the power flow status 13000. The profile reads the signed 5213-5214 instead, which older firmware may lack | A read spec whose sign comes from a bit of another register. |
| "Not available" markers decode as numbers | 0xFFFF (U16) and 0x7FFFFFFF (S32) mean "no data" (e.g. load and export power without a meter); `undef` maps to 0 | An `unavailable` marker on read specs that yields no value instead of 0. |
| Settings registers polled every cycle | the vendor asks not to read or write RW registers frequently through WiNet-S; `mode_value` re-reads 13049 each cycle | A per-key read interval (settings read-back slower than measurements). |
| No post-connect quiet period | the community package waits several seconds after connecting through WiNet-S | See the core row above. |

## Automatic verification ladder

What profiles cannot yet say for the per-device verification (identify, read, dry-run, re-write
of the current value, short forced window):

| Missing | Brand(s) | What the core would need |
|---|---|---|
| A safe forced test window | huawei-sun2000 (no forced intent fits the schema) | A profile-declared low-power test command and duration, with how to end it (Huawei: 47100 = 0 stops a forcible charge/discharge). |
| Credentials for the write step | huawei-sun2000 | A way for the ladder to ask for and keep the installer login before the first write. |
| A safe forced test window | sungrow-sh (forced control needs two registers) | A profile-declared low-power test command and duration with its exit sequence (Sungrow: 13050 = 0xCC, then 13049 = 0). |
