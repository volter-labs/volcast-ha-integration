# Volcast Solar Forecast

[![HACS Validation](https://github.com/volter-labs/volcast-ha-integration/actions/workflows/hacs.yaml/badge.svg)](https://github.com/volter-labs/volcast-ha-integration/actions/workflows/hacs.yaml)
[![hassfest](https://github.com/volter-labs/volcast-ha-integration/actions/workflows/hassfest.yaml/badge.svg)](https://github.com/volter-labs/volcast-ha-integration/actions/workflows/hassfest.yaml)

Home Assistant integration for [Volcast](https://volcast.app) — solar PV production forecasts powered by multi-model weather ensemble, Kalman filter calibration, and real-time nowcasting.

## Beta: Installation Discovery (v2.0.0b1)

This version adds read-only installation discovery. Existing forecast setups keep working exactly as in 1.7.2; after updating, they also get the 'Installation discovery' sensor and 'Run discovery' button, and discovery runs automatically after each Home Assistant start.

### What Discovery Does

Discovery runs automatically once after Home Assistant starts and again after each integration reload. It scans:

- Your installed inverter and price integrations (device and entity registries)
- Entity names, units, and options from those integrations
- The inverter's host address from its settings (no passwords, nothing else)
- How many days of long-term statistics your energy sensors have
- Sends one UDP broadcast on port 48899 to find Wi-Fi data loggers on your local network

Discovery adds two entities to the Volcast device:
- **Installation discovery** sensor — a summary of what was found
- **Run discovery** button — manually re-scan anytime

### Privacy & Data

For an entry that is not connected to a Volcast account, the full discovery result stays in Home Assistant only. It is visible in your diagnostics file, where serial numbers, MAC addresses and e-mails are masked (IP addresses are kept). **Nothing is sent to Volcast.** (A paired entry does send some of this data to your account — see "Connect your account (beta)" below.)

The diagnostics file still lists entity names and local IP addresses, so review it before sharing it with anyone.

### New: Discovery-Only Setup

A new setup option "**Discovery only (no account)**" is for people without a Volcast account. It enables discovery in Home Assistant with no forecast, no cloud connection, and no Volcast API key required. It is available only when Volcast is not set up yet, because forecast setups already include discovery.

### Sharing Your Setup

To share your installation details with support:

1. Go to **Settings** → **Devices & Services**
2. Click **Volcast**
3. Tap the **⋮** (three dots) menu → **Download diagnostics**

### Going Back to v1.7.2

If you'd prefer to skip this beta:

1. **If you enabled inverter control:** turn off the Volcast control switch (or set **Options → Inverter control → Off**) and confirm the inverter is back in its normal mode. Do this first — 1.7.2 has no control logic and a Home Assistant restart never undoes a write on its own, so a value Volcast wrote would otherwise stay until you change it by hand.
2. **Delete** any "Discovery only (no account)" Volcast entry — v1.7.2 cannot load it
3. In HACS, click **Volcast Solar Forecast** → **⋮** → **Redownload**
4. Select version **1.7.2**
5. Restart Home Assistant

## Beta: Account Pairing & Inverter Control (v2.0.0b2)

### Connect your account (beta)

Choose "**Connect to your Volcast account**" during setup, or convert an existing discovery-only or API-key entry to it later. Home Assistant opens a pairing window; confirm it in the Volcast app or on the Volcast website, and the setup finishes by itself — no code to copy.

During pairing, Home Assistant shares with Volcast: the inverter's make and model, its rated power, and the list of settings Volcast can control on it (no serial number and no local address — those stay in Home Assistant), and the price entity id if a usable price sensor was found. Once paired, the integration also sends:

- Live readings roughly once a minute
- Up to 60 days of past consumption history, once
- A control summary — what Volcast is doing or would do, any pause, and how many times a setting was changed outside Volcast (never entity ids or serial numbers)
- The inverter profile id, battery capacity (if known), which settings Volcast can control, and whether the local control switch is on

It also adds a plan card and a "Volcast" panel with the current and upcoming plan slots, and lets you pick a price sensor of your own in **Options → Energy prices**.

### Inverter control (beta)

Volcast can write plan-driven mode and power settings to your inverter **through your existing inverter integration's entities** (for example a select for mode and a number for power). A direct local connection to the inverter is described in "Direct connection (beta)" below.

**In this pre-release, control is enabled per inverter brand.** Writing is enabled only once a brand's profile has been verified against real hardware. Control through the **GoodWe** integration (the HACS "GoodWe Inverter (experimental)" integration) is verified; other brands are not yet. Until a brand is verified, the status sensor always shows what Volcast *would* write, and nothing actually reaches the inverter.

Once a brand is verified, nothing is written unless all of these are also true at once:

1. "**Through the inverter integration (entities)**" is selected in **Options → Inverter control**
2. You've given consent in the Volcast app
3. The Volcast **control switch** entity is turned on in Home Assistant

Entity-mode control and account pairing are both beta features.

**Verification ladder and plan-only mode.** Before the first write to an inverter, Volcast verifies the device step by step: a read-only trial that only counts the writes it would make, a control write confirmed by read-back, and, where the profile allows it, a short test window. A profile already marked verified starts at the control write and skips the trial. A stop returns the inverter to the settings it had before Volcast took control, and the verification sensor shows the current step. If another controller writes the same settings, Volcast asks you to choose who controls the inverter; choosing your own controller keeps Volcast in plan-only mode, where it computes the plan but does not write.

### Safety behaviour

- **Daily limit on setting changes** — Plan writes made through the inverter integration entities now count against a daily write limit per setting and in total over a rolling 24 hours. When the limit is reached, further plan writes are held. Returning the inverter to its own settings is never blocked by the limit.
- **You change a setting yourself while control is on** — Volcast pauses control for 30 minutes, keeps the value you set, and raises a repair notification. A clock-triggered schedule you already run on your inverter is treated as ordinary drift instead — Volcast does not read it as "someone took over" — so it does **not** pause control. Instead, Volcast writes the planned value back on its next control cycle, about a minute later. If your own automation is meant to control these settings, turn the Volcast control switch off first, or the two will keep overwriting each other.
- **Consent is revoked, the switch is turned off, or the entry is disabled/removed** — Volcast returns the inverter to how it was **before Volcast took control**: its baseline mode, export limit and SoC limits — not to the last value Volcast wrote. The one exception: if you had already taken the mode over yourself, Volcast leaves the mode alone and keeps only the last power setpoint it wrote, instead of restoring the older value there too.
- **Before installing an older version, or removing the integration's files in HACS without first deleting the entry:** turn off the Volcast control switch and confirm the inverter is back in its normal mode first — see "Going Back to v1.7.2" above. Deleting the entry itself does restore (previous bullet); an older version or a files-only removal has no control logic to do that for you.
- **Repeated write failures** — a repair notification tells you control has stopped working and what to check.
- **Known limitation:** a 30-minute pause does not survive a Home Assistant restart — after a restart, control resumes immediately even if a pause was in effect before.

### Diagnostics privacy

The downloadable diagnostics file masks serial numbers, MAC addresses and e-mail addresses (IP addresses are kept, since they help with troubleshooting). Once an entry is paired, its readings and control summary also go to your Volcast account, as listed above — diagnostics is no longer the only place they're visible.

## Beta: Direct Connection

### Direct connection (beta)

Volcast can connect to a supported inverter directly on your local network — **GoodWe** (ET/EH/BT/BH, over UDP or Modbus TCP), **Deye** three-phase hybrids (SUN-xK-SG, through a Solarman data logger or Modbus TCP), or either behind an RS485 ↔ TCP gateway.

- **Search**: **Options → Installation details → Search for the inverter on the local network** only reads from devices that answer (identification and settings registers) and never writes; pick your inverter from the list or enter its local IP address.
- **Sensors**: state of charge, temperatures, voltages, power flows, lifetime energy counters (Energy dashboard), the inverter mode and diagnostic settings, refreshed every 10 s by default.
- **Read-only test connection**: for inverters whose direct control is not verified yet, **Installation details → Read-only test connection** shows the sensors and what Volcast *would* write, and never writes.

**Direct mode is offered once the inverter is identified, but every device first goes through a verification ladder before Volcast writes to it.** The ladder starts with a read-only trial: Volcast reads the inverter and counts what it would have written. Only after you allow control does it make a control write (the current setting is written again and read back) and run a short test window. Brands marked verified skip the trial and start at the control write.

- **GoodWe** (ET/EH/BT/BH) — verified since v2.0.0b6 on a live GW8KN-ET over the Wi-Fi module (UDP). Writes are also enabled over Modbus TCP and over an RS485 ↔ TCP gateway (Modbus RTU framing); these use the same registers but have not been tried on hardware yet. Other ET/EH/BT/BH models share the register map but have not been tried yet; the search detects registers a model does not have, and Volcast does not use them. Choose **Options → Inverter control → Directly over the local network**.
- **Deye** — still draft: only the read-only test connection is available, and nothing is written.
- **Draft profiles** — Huawei SUN2000 (L1/M1, through an SDongle over Modbus TCP), Sungrow SH (WiNet-S, Modbus TCP), SolaX X1/X3-Hybrid G3/G4 (Modbus TCP or RTU), Sofar HYD 5-20KTL-3PH (Solarman logger or RTU), Solis hybrid S5/S6-EH1P and RHI (Solarman stick or RTU), FoxESS H1-G2/AC1-G2/P1 (RS485 gateway; readings through `foxess_modbus` only), SolarEdge StorEdge and Home Hub (Modbus TCP, port 1502) and Fronius GEN24 (Modbus TCP). All eight are **draft**: only the read-only test connection is available, nothing is written, and control for any of them requires a live verification on the inverter first. Their `ha` entries are only hints for mapping the readings of an existing integration: entity mode is not offered for them until that integration entry is verified. When several profiles describe the same integration (`solarman`, `solax_modbus`) and the device's manufacturer and model text does not decide between them, no profile is selected and the discovery sensor lists the candidates. Profile ids: `huawei-sun2000`, `sungrow-sh`, `solax-x-hybrid`, `sofar-hyd`, `solis-hybrid`, `foxess-h`, `solaredge-storedge`, `fronius-gen24`.

Nothing is written unless direct control is selected, consent is given in the Volcast app, the control switch is on, the inverter's identity at the saved address is confirmed and no other client uses the inverter.

**The GoodWe Wi-Fi module must have Volcast as its only local client.** Other Home Assistant integrations, the manufacturer's app on the local network or other energy controllers talking to the same module cause timeouts and mismatched replies.

Safety rules for the direct connection ("neutral mode" = the inverter's normal self-use mode, which is not the same as returning it to its settings from before Volcast):

- Every write is read back; only values the inverter actually holds are recorded.
- A daily limit on setting changes protects the inverter's memory; a forced charge or discharge returns to the inverter's normal mode when the limit is hit, and returning to baseline is never blocked by it.
- Withdrawing consent, turning the switch off, or disabling/removing the entry returns the inverter to its settings from before Volcast (confirmed by reading back). Changing the control method in the options first returns the inverter through the current method — if the inverter cannot be reached, the change is refused.
- Stopping Home Assistant, or unloading or reloading the entry, tries to set the neutral mode if the inverter is in a forced mode set by Volcast (in both direct and entity mode); control resumes afterwards. This is best effort within a 10-second budget, not a guarantee: if the inverter does not answer in time, it keeps its last mode and an error is logged.
- A control cycle that cannot run the plan safely (no plan, a stale or missing reading, an error, another client on the connection) sets the neutral mode if the inverter is in a forced mode set by Volcast. An action that needs a setting the inverter does not have or that cannot be read runs in the neutral mode instead; without a usable mode register direct control does not run and a repair notification is raised.
- In a sell slot the export setpoint is recalculated from the inverter's own PV and house-load reading; without a usable reading the slot runs in the neutral mode.
- A setting you change twice within 30 minutes pauses control for 30 minutes and stays as you set it until the plan for it changes. During a pause, a discharge set by Volcast is switched to the neutral mode once the battery reaches the plan's reserve.
- Another integration configured with the inverter's local address blocks the direct connection (Volcast checks again every few minutes; cloud-only integrations do not block it).
- If a different device answers at the saved address, reading and control stop until you search again.
- **These safeguards only work while Home Assistant is running.** If Home Assistant crashes or hangs, or the host loses power or network, while the inverter is in a forced mode (charging from the grid or selling), the inverter stays in that mode until Home Assistant is back.

Stored in Home Assistant only: the inverter's local address, port, unit ID, Solarman logger serial, a salted device fingerprint and the search results. Sent to Volcast: the readings, the profile id, the connection type and counters, which settings can be controlled and the rated power — **never** the address, serial numbers, the fingerprint or the salt. Raw frames appear in the diagnostics file only while the read-only test connection is on, with serial numbers and the logger serial zeroed.

Known limitations: single-phase Deye inverters are not identified; Solarman logger behaviour on real hardware and the Deye write details are not verified yet; on some GoodWe models the upper state-of-charge limit cannot be read back and is not set directly. No serial port (use an RS485 ↔ TCP gateway), local IP addresses only, no background scanning. Brands that answer on input registers (function code 4) are supported by the Modbus core. The SunSpec scale factors in the draft profiles (Fronius, SolarEdge) are fixed assumptions and may be wrong for some inverters. Some brands (FoxESS) have no serial or rating register, so they cannot be identified on the direct path; until the `foxess_modbus` integration entry is verified they get readings only, no control.

**Going back to v2.0.0b2:** if you used direct control, turn off the Volcast control switch and wait until the status sensor shows the inverter was returned to its previous settings — b2 cannot do that for a direct connection. See `docs/release-notes/v2.0.0-beta3.md` for details.

## Features

- **Energy Dashboard integration** — appears as a solar forecast source in the HA Energy Dashboard
- **7-day forecast** — daily energy (kWh) and peak power (kW) for up to 7 days
- **Hourly & 5-min data** — `detailedHourly` and `detailedForecast` attributes on every daily sensor
- **Live power estimate** — interpolated current power output (W)
- **Production tracking** — sends your actual inverter data to Volcast for forecast calibration
- **Nowcasting** — adjusts today's remaining forecast based on actual production so far
- **Curtailment detection** — uses battery SoC to detect inverter curtailment; affected hours are excluded from calibration so a capped system doesn't skew the forecast
- **Persistent retry queue** — production submissions that fail (network issues, API downtime) are queued locally and retried automatically
- **Peak production alert** — binary sensor for automations (configurable threshold)
- **UI-based setup** — no YAML needed, just enter your API key and select your sensors
- **Key sanity check at setup** — a pasted key that is not a real Volcast key (for example the shortened preview shown in the app) is rejected instantly with a clear hint, before any network call

### Resilience & Reconciliation

The integration now ships three layered resilience mechanisms:

- **Forecast retry** — short network blips no longer flap entities to `unavailable`. The integration retries failed forecast fetches with 5/15/45s backoff inside one update cycle.
- **Submit retry** — hourly production submits use the same 5/15/45 retry pattern before queuing. Persistent retry queue still kicks in for longer outages.
- **Daily reconciliation** — the integration reads hourly statistics from HA Recorder and fills any gaps in your Volcast cloud history. The scheduled run at 00:30 local each day backfills the **previous day**. Additionally, on **HA startup** it reconciles **both yesterday and today**, so a gap left by an HA restart or update during the day self-heals immediately instead of waiting for 00:30. Requires `energy_entity` configured with `state_class: total_increasing`.

New diagnostic entities (under integration → Diagnostic):
- `sensor.volcast_submit_queue_depth` — readings waiting to be retried (0 = healthy).
- `sensor.volcast_last_reconciliation` — timestamp + status of last daily backfill.
- `binary_sensor.volcast_integration_healthy` — single signal you can wire into automations.

Power-only users (no `energy_entity` configured): retry mechanisms apply, but daily reconciliation is **not** active. Planned for a future release.

### Manual sync (force sync)

If Home Assistant was offline (system update, reboot) and an hour of production
is missing in Volcast, the integration self-heals on the next HA start and daily
at 00:30. To force it immediately without waiting:

- Press the **Sync production now** button on the Volcast device page, or
- Call the `volcast.sync_production` action — optionally with a `date` (within
  the last ~24–38 hours) to reconcile one specific day. Without a date it
  reconciles yesterday and today.

Both are safe to repeat: hours already delivered (and the current in-progress
hour) are skipped, so nothing is double-counted.

**Limitation:** if HA was fully down during an hour, HA Recorder may have no
statistics row for that hour. The energy is not lost — it lands in the next
hour's delta — but no sync can split it back out. Daily totals stay correct.

_(The button and service require an energy sensor configured — power-only
users have nothing to reconcile.)_

## How It Works

### Forecast Model

Volcast uses a physics-based PV simulation model fed by a multi-model weather ensemble (ECMWF IFS, GFS, and regional models like ICON, UKMO, JMA depending on your location). The models are blended with horizon-dependent weighting — regional models dominate for short-range forecasts, global models take over for longer horizons.

The integration polls the Volcast cloud API at a configurable interval (default: 60 minutes). Data is served from a server-side cache that refreshes every 2 hours, so values match exactly what you see in the Volcast mobile app.

### Production Tracking & Calibration

When you connect your inverter's energy or power sensor, the integration sends **hourly production summaries** to Volcast. This data drives two mechanisms:

**Kalman filter calibration** — Volcast maintains a per-user bias estimate that adjusts forecasts based on how your actual production compares to predictions. The filter uses hourly cloud cover to apply different corrections for clear vs. cloudy conditions. This means the forecast learns your system's real-world characteristics (shading, soiling, inverter efficiency) over time. Calibration requires at least 5 days of data to activate and is applied to future days only — today's forecast stays unbiased.

**Nowcasting** — After receiving at least 2 hourly readings for today, Volcast computes an actual-to-forecast ratio and adjusts the remaining hours of today's forecast. The adjustment decays exponentially for hours further from the last reading, so it has the strongest effect on the next few hours. This helps when conditions differ from the morning forecast — for example, unexpected cloud cover or clearer skies than predicted. Nowcast resets daily.

**Curtailment detection** — When you connect a battery state-of-charge sensor, the integration detects hours when the inverter caps production (battery full + clear sky + low output vs. forecast). Curtailed hours are marked so calibration ignores them — otherwise the Kalman filter would learn a downward bias from artificially low production. Battery sensors are optional; without them, curtailment is not detected but calibration still works.

All three mechanisms are optional — the forecast works without production tracking, it just won't improve over time.

### How production data is collected

The integration tracks your inverter sensor via state change events and accumulates data in hourly buckets:

- **Energy sensor** (preferred): Computes the delta between the first and last reading each hour. Handles counter resets and carries over the last reading to the next hour to avoid gaps.
- **Power sensor** (fallback): If the energy sensor is unavailable or resets, uses trapezoidal integration of power readings to estimate hourly energy.

Data is submitted to Volcast once per hour (at ~5 minutes past each hour). A quality score and peak power reading are included with each submission.

## Sensors

| Entity | Type | Description |
|--------|------|-------------|
| `sensor.volcast_energy_forecast_today` | Energy (kWh) | Today's total forecasted production |
| `sensor.volcast_energy_forecast_tomorrow` | Energy (kWh) | Tomorrow's total forecasted production |
| `sensor.volcast_energy_forecast_day_3` – `day_7` | Energy (kWh) | Days 3–7 forecasted production |
| `sensor.volcast_power_now` | Power (W) | Current estimated power output |
| `binary_sensor.volcast_peak_production` | Binary | ON when power > threshold % of today's peak |
| `sensor.volcast_api_status` | Diagnostic | API connection status |

## Prerequisites

1. **Volcast app** — download from [App Store](https://apps.apple.com/app/volcast/id6740044441) or [Google Play](https://play.google.com/store/apps/details?id=pl.volcast.app)
2. **Premium subscription** — required for API access
3. **API key** — generate in the app: Settings > API Access

## Installation

### HACS (recommended)

1. Open HACS in your Home Assistant
2. Click the 3 dots menu > **Custom repositories**
3. Add `https://github.com/volter-labs/volcast-ha-integration` as an **Integration**
4. Click **Install**
5. Restart Home Assistant

### Manual

1. Download the `custom_components/volcast` folder from this repo
2. Copy it to your HA `config/custom_components/` directory
3. Restart Home Assistant

## Setup

1. Go to **Settings > Devices & Services > Add Integration**
2. Search for **Volcast**
3. Enter your API key (`vk_...`)
4. **(Optional)** Connect your inverter sensors for production tracking:
   - **Today's PV generation (kWh)** — a sensor showing today's total solar production that resets daily (e.g. "Today's PV Generation", "Daily Yield" from GoodWe, Fronius, SolarEdge, Huawei, SMA, Enphase)
   - **Current PV power (W)** — a sensor showing real-time power output, used as fallback when the energy sensor is unavailable
5. **(Optional)** Connect your battery sensors for curtailment detection:
   - **Battery state of charge (%)** — enables detection of inverter curtailment when the battery is full
   - **Battery charge power (W)** — power flowing to/from the battery, improves curtailment accuracy (v2 detection)
6. Done — sensors will appear automatically

All production and battery sensors are optional. You can add or change them later in the integration options.

### Energy Dashboard

1. Go to **Settings > Dashboards > Energy**
2. Under **Solar panels**, click **Add solar forecast**
3. Select **Volcast Solar Forecast**
4. Your forecast will appear on the Energy Dashboard

The integration retains a rolling forecast history (last 14 days), so when you
navigate back to previous days the Energy Dashboard still shows what was
forecast for those days — not just today and the future. The history is
persisted across restarts.

## Configuration Options

After setup, click **Configure** on the integration to adjust:

| Option | Default | Range | Description |
|--------|---------|-------|-------------|
| Update interval | 60 min | 15–1440 | How often to poll the API |
| Peak threshold | 80% | 50–100 | Threshold for peak production binary sensor |
| PV energy sensor | — | — | Today's generation sensor (kWh, resets daily) |
| PV power sensor | — | — | Current power sensor (W, fallback) |
| Battery SoC sensor | — | — | State of charge (%) — enables curtailment detection |
| Battery charge power sensor | — | — | Battery power flow (W) — improves curtailment accuracy |

## Sensor Attributes

Each daily energy sensor (`energy_today`, `energy_tomorrow`, days 3–7) exposes rich attributes for advanced automations:

| Attribute | Format | Description |
|-----------|--------|-------------|
| `hours` | `[{"hour": 10, "power_kw": 3.5, "energy_kwh": 3.2}, ...]` | Hourly breakdown (24 entries) |
| `detailedHourly` | `[{"period_start": "ISO8601", "power_kw": 3.5, "energy_kwh": 3.2}, ...]` | Hourly with ISO timestamps (Solcast-compatible) |
| `detailedForecast` | `[{"period_start": "ISO8601", "power_w": 3500, "energy_wh": 292}, ...]` | 5-minute granularity |
| `peak_power_kw` | `float` | Day's peak power |
| `confidence` | `high` / `medium` / `low` | Forecast confidence level |
| `sunshine_hours` | `float` | Expected sunshine hours |
| `cloud_cover_pct` | `float` | Average cloud cover percentage |

The `energy_today` sensor additionally includes a `forecast` attribute with a 7-day daily summary.

### Accessing hourly data in templates

```yaml
# Today's hourly forecast as a list
{{ state_attr('sensor.volcast_energy_forecast_today', 'detailedHourly') }}

# Power at hour 12
{{ state_attr('sensor.volcast_energy_forecast_today', 'hours')
   | selectattr('hour', 'eq', 12) | first | attr('power_kw') }}
```

### Accessing hourly data in AppDaemon / Python

```python
state = self.get_state("sensor.volcast_energy_forecast_today", attribute="detailedHourly")
for entry in state:
    print(f"{entry['period_start']}: {entry['power_kw']} kW")
```

## Automation Examples

### Notify when tomorrow's forecast is high

```yaml
automation:
  - alias: "High solar forecast tomorrow"
    trigger:
      - platform: numeric_state
        entity_id: sensor.volcast_energy_forecast_tomorrow
        above: 20
    action:
      - service: notify.mobile_app
        data:
          title: "Solar forecast"
          message: "Tomorrow: {{ states('sensor.volcast_energy_forecast_tomorrow') }} kWh expected"
```

### Start EV charging during peak production

```yaml
automation:
  - alias: "Charge EV during peak solar"
    trigger:
      - platform: state
        entity_id: binary_sensor.volcast_peak_production
        to: "on"
    action:
      - service: switch.turn_on
        target:
          entity_id: switch.ev_charger
```

### Run appliances when nowcast shows surplus

```yaml
automation:
  - alias: "Run washing machine during high production"
    trigger:
      - platform: numeric_state
        entity_id: sensor.volcast_power_now
        above: 3000
        for: "00:10:00"
    action:
      - service: switch.turn_on
        target:
          entity_id: switch.washing_machine
```

## Accuracy & Limitations

- **Forecast horizon**: Accuracy is highest for today and tomorrow. Days 5–7 are less reliable, especially in variable weather.
- **Calibration ramp-up**: The Kalman filter needs ~5 days of production data before calibration activates. During this period, forecasts use the uncalibrated model.
- **Nowcast availability**: Requires at least 2 hourly readings with meaningful production (>0.01 kWh) and forecast (>0.5 kWh). Early morning hours or heavily overcast days may not produce enough data.
- **Sensor compatibility**: Works with any inverter that exposes an energy or power entity in HA. Tested with GoodWe, Fronius, SolarEdge, Huawei, SMA, and Enphase.

## Support

- **Issues**: [GitHub Issues](https://github.com/volter-labs/volcast-ha-integration/issues)
- **App support**: In-app chat (Settings > Help)
- **Website**: [volcast.app](https://volcast.app)

## License

MIT — see [LICENSE](LICENSE)
