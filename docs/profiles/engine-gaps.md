# Engine gaps

Vendor control features that the profile schema and the control engines cannot express today.
Profiles do not work around them: the intent is marked unsupported and the capability stays
`false` (see `authoring.md`, section 7). Each row names what the core would need.

## Core

| Gap | Brand(s) | What the core would need |
|---|---|---|
| `mode_setpoint` requires all six intents | all `mode_setpoint` profiles | Optional intents with an engine fallback to the neutral mode; until then an unsupported intent is written as neutral mode + power `none` with its capability `false`. |
| Capabilities are global, not per path | huawei-sun2000, solax-x-hybrid | Per-path capabilities: a setting the Home Assistant integration can write (e.g. a SoC limit through a number entity in %) may be impossible on the direct register path. |
| One `unit_id` per profile | huawei-sun2000 | A unit id per transport or connection path (the same inverter answers on different ids depending on how it is reached). |
| No post-connect quiet period | huawei-sun2000 | A per-transport delay between opening the connection and the first request (`gap_ms` only spaces consecutive requests). |
| Entity mode is matched only by an integration domain in `INVERTER_DOMAINS` | sungrow-sh (community Modbus YAML package) | Selecting YAML `modbus`/`template` packages, which have no config entry and no device (a manual pick of the package, then matching by unique id); a `mode` key whose select comes from the `template` platform while the sensors come from `modbus` (today one entry = one platform). |
| No absolute-value transform for registers some firmware reports negative | huawei-sun2000 (grid export total 37119) | An `abs` (or unsigned-from-signed) option on read specs; until then such registers are not read. |
| A setting is read back at the address it was written | solax-x-hybrid (write table and read table differ: Charger Use Mode written at 0x001F, read at 0x008B) | A per-key read-back address (`write.<key>.readback`), used by the writer before and after the write, by the probe and by `verify_blocks`; until then such a key is `echo_only` and the schema-required verify block over the write address reads an unrelated register. |

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

## SolaX (`solax-x-hybrid`)

Addresses are wire addresses and equal the vendor's hexadecimal register addresses (no offset).
Settings have separate write and read-back addresses (see the core row above).

| Gap | Registers | What the core would need |
|---|---|---|
| Read-back at a different address | Charger Use Mode written at 0x001F, read at 0x008B; Manual Mode (G4) written at 0x0020, read at 0x008C; self-use minimum SoC (G4) written at 0x0061, read at 0x0093; export limit written at 0x0042, read at 0x00B6 | The per-key read-back address from the core row; without it every setting is echo-only and the automatic ladder cannot confirm or restore it. |
| Prerequisite (conditional) register before the mode write | forced charge/discharge/stop on G4 = 0x0020 Manual Mode (0 stop, 1 force charge, 2 force discharge) written first, then 0x001F = 3 (Manual); evcc also wakes the battery with 0x0056 = 1 before a forced charge | A mode made of two registers written in order, each with its own read-back address. Writing 0x001F = 3 alone is not a safe standby: it runs whatever 0x0020 holds. |
| Non-persistent remote control with a lifetime | remote-control commands (modes 1-7) are one FC16 write over 0x007C-0x008A, modes 8/9 over 0x00A0-0x00A7, carrying a 32-bit active-power target, a duration in seconds and a timeout; G4 and later only; not stored in EEPROM, they lapse unless repeated | Multi-register (FC16) set-point writes with 32-bit fields and a profile-declared keep-alive (re-send interval and fallback when it stops). This is the better long-term route than EEPROM settings for frequent control. |
| Generation-specific write maps | 0x0020 is Manual Mode on G4 but battery minimum capacity on G3; 0x0061 (self-use minimum SoC) and 0x00E0 (charge upper SoC) are documented for G4 only; Charger Use Mode values 1 and 3 mean Force Time Use / Feedin Priority on G3 and Feedin Priority / Manual on G4 | Write keys and mode tables selected per identified variant (serial prefix), not one map per profile. |
| Scaled export-limit write on some models | export limit 0x0042 is in W on X1 G4 but in 10 W units on X3 G4 (same register) | A scale on write encodings, chosen per variant. |
| Settings unlock before writes | 0x0000 = 2014 (unlock; 6868 advanced) before settings can be changed, possibly again after a power cycle; lock state readable at input 0x0054 | A session step that writes and checks an unlock value before the first settings write. |
| Rated power register only on G4 | rated output power at holding 0x00BA (W) is documented for G4; G3 has no documented register | A per-variant identify register, or no rated power for G3. |
| Settings live in EEPROM | community docs warn of about 100000 write cycles for settings | Covered by `nvm` and `nvm_budget`; listed so the remote-control route above is preferred once supported. |

## Sofar (`sofar-hyd`)

Addresses are wire addresses and equal the vendor's hexadecimal register addresses (no offset).
All registers are holding registers. Community docs say most writable settings are stored in
EEPROM.

| Gap | Registers | What the core would need |
|---|---|---|
| Forced charge/discharge needs a mode plus an atomic block of 32-bit set-points | Passive mode = 0x1110 = 3, then desired grid power, minimum and maximum battery power as three I32 values at 0x1187-0x118C, which the device accepts only as one FC16 block of 6 registers starting at 0x1187 | Signed 32-bit write encodings, a multi-register write block committed in one request, and a mode made of two writes in order (mode register, then the block). Writing 0x1110 = 3 alone is not a safe standby: it runs whatever the set-points hold. |
| Passive-mode watchdog | timeout 0x1184 and timeout action 0x1185 (force standby or return to the previous mode), written as a pair from 0x1184 | A profile-declared keep-alive (re-send interval) and fallback, written before the first passive command; this is the better route for frequent control than stored settings. |
| Time slots with power and a commit register | Timing mode: program 0x1111, charge/discharge control 0x1112, start/end times at 0x1113-0x1116, U32 power at 0x1117 and 0x1119, set-program register 0x111F; the 15 registers 0x1111-0x111F are written as a whole. Time-of-use: 16 registers 0x1120-0x112F written as a whole, with target SoC 0x1124 and power 0x1125 | Start/end slot writes, unsigned 32-bit power writes, a program selector written first and a commit register written last. |
| Export limit is a pair with a scaled power | feed-in limitation mode 0x1023 and maximum feed-in power 0x1024 in 100 W units, written together as a block of 2 from 0x1023 | A scale on write encodings and two-register atomic writes. |
| SoC floor is a depth of discharge behind a selector and a commit register | depth of discharge 0x104D in the battery configuration block 0x1044-0x105A, selected per battery by 0x1044 and applied through BatConfig_Control 0x1053; the community sources do not document how the value maps to a SoC floor | A percent write with a declared mapping (possibly inverted) plus the selector/commit sequence of a configuration block. |
| Non-standard reply to FC16 writes | evcc ignores a malformed reply ("response data size 18 does not match count 4") to its FC16 writes at 0x1110 and 0x1187 | Tolerating a malformed write confirmation when the read-back of the written register shows the new value, per profile and per transport. Until then such a write is reported as failed; the profile keeps function 16 as the reference integrations do. |
| Several battery packs | packs 1-8 at a stride of 7 registers from 0x0604 (power 0x0606, SoC 0x0608, ...); only pack 1 is read | A pack count read from the inverter and aggregates over the present packs (sum of power, capacity-weighted SoC); `sum` alone would add absent packs and cannot weight the SoC. |
| One entity map per integration domain | two integrations register the domain `solarman` with opposite sign conventions: davidrapan/ha-solarman inverts battery power 0x0606 and grid power 0x0488 to the usual signs, StephanJoubert/home_assistant_solarman keeps the vendor signs | Entity specs (and transforms) per integration variant within a domain, e.g. chosen by the unique id pattern that matched; until then the signed keys match only the davidrapan entities and the other integration needs the manual pick. |

## Automatic verification ladder

What profiles cannot yet say for the per-device verification (identify, read, dry-run, re-write
of the current value, short forced window):

| Missing | Brand(s) | What the core would need |
|---|---|---|
| A safe forced test window | huawei-sun2000 (no forced intent fits the schema) | A profile-declared low-power test command and duration, with how to end it (Huawei: 47100 = 0 stops a forcible charge/discharge). |
| Credentials for the write step | huawei-sun2000 | A way for the ladder to ask for and keep the installer login before the first write. |
| A safe forced test window | sungrow-sh (forced control needs two registers) | A profile-declared low-power test command and duration with its exit sequence (Sungrow: 13050 = 0xCC, then 13049 = 0). |
| A re-write probe confirmed by read-back | solax-x-hybrid (read-back of 0x001F lives at 0x008B) | The per-key read-back address; until then the ladder's single reversible write is echo-only and its result must be checked by hand. |
| A safe forced test window | solax-x-hybrid (forced control needs 0x0020 then 0x001F, or remote-control FC16 writes) | A profile-declared low-power test command and duration with its exit (SolaX: 0x001F = 0 Self Use; remote control lapses when not repeated). |
| Unlock before the write step | solax-x-hybrid | A way for the ladder to write the settings unlock value (0x0000 = 2014) and verify the lock state before the first write. |
| A safe forced test window | sofar-hyd (forced control needs 0x1110 = 3 plus the I32 block at 0x1187) | A profile-declared low-power test command and duration with its exit (Sofar: 0x1110 = 0 Self Use) and the passive watchdog 0x1184/0x1185 set to "return to the previous mode" as a fallback. |
| A probe that survives a malformed write reply | sofar-hyd (FC16 replies at 0x1110 reported as non-standard by evcc) | The ladder's single reversible write judged by the read-back of 0x1110, not only by the write confirmation. |
