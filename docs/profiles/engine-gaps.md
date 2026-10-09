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

## Automatic verification ladder

What profiles cannot yet say for the per-device verification (identify, read, dry-run, re-write
of the current value, short forced window):

| Missing | Brand(s) | What the core would need |
|---|---|---|
| A safe forced test window | huawei-sun2000 (no forced intent fits the schema) | A profile-declared low-power test command and duration, with how to end it (Huawei: 47100 = 0 stops a forcible charge/discharge). |
| Credentials for the write step | huawei-sun2000 | A way for the ladder to ask for and keep the installer login before the first write. |
