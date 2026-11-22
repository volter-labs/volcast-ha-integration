# Volcast Solar Forecast

[![HACS Validation](https://github.com/volter-labs/volcast-ha-integration/actions/workflows/hacs.yaml/badge.svg)](https://github.com/volter-labs/volcast-ha-integration/actions/workflows/hacs.yaml)
[![hassfest](https://github.com/volter-labs/volcast-ha-integration/actions/workflows/hassfest.yaml/badge.svg)](https://github.com/volter-labs/volcast-ha-integration/actions/workflows/hassfest.yaml)

Home Assistant integration for [Volcast](https://volcast.app) — solar PV production forecasts powered by multi-model weather ensemble, Kalman filter calibration, and real-time nowcasting.

## Two lines: stable and 2.0

The integration comes in two lines. They share the forecast; the 2.0 line adds the rest.

| | Stable (1.7.x) | 2.0 (beta) |
|---|---|---|
| For | Solar forecast in Home Assistant | Everything in the stable line, plus the full Volcast app: live home energy, a price-aware battery plan, EV charger, and optional inverter control |
| Connects with | API key | Your Volcast account (pairing), or an API key |
| Install | Default HACS catalog | Pre-release (see [Install the 2.0 beta](#install-the-20-beta-and-go-back-to-stable)) |

**Who 2.0 is for.** Volcast account holders who want live data and the battery plan from their Home Assistant, and who are ready to try beta software. The stable line is the safe default; stay on it unless you want these features. This README describes the 2.0 line as it is today; the forecast sections apply to both lines.

## Requirements

- Home Assistant 2024.4.0 or newer.
- The Volcast app: [App Store](https://apps.apple.com/app/id6758962799) or [Google Play](https://play.google.com/store/apps/details?id=com.volcast.app).
- A subscription tier that matches what you use:

| Tier | What it covers |
|---|---|
| Forecast | 7-day forecast, calibration, alerts, and the API key for Home Assistant (the **API Key** card in the app, on the Home Assistant screen in Settings) |
| Plan | Everything in Forecast, plus live energy, prices and the daily battery plan |
| Automate | Everything in Plan, plus inverter control |

The app shows what your tier includes. Control also needs your consent in the app (see [Inverter control](#inverter-control)).

## Install the 2.0 beta and go back to stable

2.0 versions are pre-releases, so they are not offered by default. Below, "the latest 2.0.0 beta" is the newest pre-release and "the stable version (1.7.x)" is the newest non-beta release. All versions and release notes are on the [GitHub Releases](https://github.com/volter-labs/volcast-ha-integration/releases) page.

**Make a Home Assistant backup first.** Both HACS and the Marketplace advise restoring a backup instead of downgrading.

### Home Assistant Marketplace (Home Assistant 2026.11 and newer)

1. **Settings → Marketplace**, open **Volcast Solar Forecast** (add the repository link `https://github.com/volter-labs/volcast-ha-integration` with **Add from link** if it is not listed).
2. Menu **⋮ → Include pre-releases**, confirm.
3. An update to the latest 2.0.0 beta appears (also under **Settings → Updates**). Update it.
4. **Restart Home Assistant yourself.** Home Assistant does not ask for a restart here.

To go back: menu **⋮ → Install another version**, pick the stable version (1.7.x), update, then restart Home Assistant. To stop being offered betas, use **⋮ → Exclude pre-releases**.

### HACS

1. Open **Volcast Solar Forecast** in HACS, menu **⋮ → Redownload**.
2. Expand **Need a different version?** and pick the release: the latest 2.0.0 beta (marked pre-release), or the stable version (1.7.x) to go back. Click **Download**.
3. Restart Home Assistant (HACS raises a "Restart required" repair you can confirm).

### Before going back to stable

1. If you enabled inverter control, turn off the Volcast control switch (or set **Options → Inverter control → Off**) and confirm the inverter is back in its normal mode. The stable version has no control logic, and a Home Assistant restart never undoes a write on its own.
2. Delete any "Discovery only (no account)" Volcast entry; the stable version cannot load it.

After going back with a paired account, the connection to your account stays, but the 2.0 entities (plan, status, control, discovery) are unavailable and live data in the app stops. Updating to the beta again needs no new pairing; live data returns within a few minutes. Turning off the control switch returns the inverter to its previous settings and, where the Volcast cloud mirrors consent for your plan, also withdraws your control consent on the account (otherwise it only switches control off in Home Assistant).

## Connect your account (pairing)

1. **Settings → Devices & services → Add integration**, search for **Volcast**.
2. In **Set up Volcast** choose **Connect to your Volcast account (recommended)**. Leave **Advanced** closed and submit.
3. When Home Assistant shows an external-site step, click **Open website**. The link opens the Volcast app (on a computer, open it on your phone).
4. In the app, check the Home Assistant name and **Connect**, then confirm a second time with Face ID or fingerprint (on a device without biometrics, **Confirm once more**).
5. The Home Assistant window finishes by itself, usually within a minute, and ends on **Name and assign** (choose areas for the two devices or **Skip and finish**).

The pairing request expires after 10 minutes; start again if it runs out. The **Control** switch starts off, and nothing controls your inverter until you also agree in the app.

Other setup modes: **Connect with API key** gives the forecast and production tracking only, as in the stable line (needs the Forecast tier); there is no live data, plan or control for such an entry until you convert it to a paired one; **Discovery only (no account)** runs a read-only scan of your installation without any account (see [Privacy and diagnostics](#privacy-and-diagnostics)). An API-key or discovery-only entry can be converted to a paired one later.

During pairing, Home Assistant shares the inverter's make and model, rated power, the list of settings Volcast could control, and the price entity id if a usable price sensor was found. Serial numbers and local addresses stay in Home Assistant. Once paired, the integration also sends:

- live readings roughly once a minute,
- up to 60 days of past consumption history, once,
- a control summary: what Volcast is doing or would do, any pause, and how many times a setting was changed outside Volcast (never entity ids or serial numbers),
- the inverter profile id, battery capacity (if known), which settings Volcast can control, and whether the control switch is on.

It adds a plan card and a "Volcast" panel with the current and upcoming plan slots.

## Live data and sensors

Live data means the app's Home, Energy and Plan screens fill with readings from your inverter.

**Recognised inverter integrations work by themselves.** Volcast matches your installed integration to an inverter profile and reads state of charge and PV, battery, grid and house-load power from its entities.

**If Volcast does not recognise an integration**, discovery reports "No inverter integration found" and the app shows "Waiting for data from the installation…". Then:

- A repair appears: **Volcast needs to know which sensors to use** (only for a paired entry without a recognised profile or a direct connection, and with no PV power sensor chosen yet). Its **Choose your inverter sensors** step shows a proposal built from your Home Assistant sensors.
- The same proposal is in **Configure → Volcast Options → Installation details**. Review each sensor and **Submit**.

Nothing is saved until you submit, and the inverter settings are never changed by this step. PV power is required; the other sensors improve the plan.

**Signs.** Volcast expects battery power to be **positive while discharging** and grid power to be **positive while importing**. If a sensor is the other way round, turn on **Battery sensor shows discharging as negative** or **Grid sensor shows export as positive** in the same form. The battery option applies only to a battery sensor you chose there.

**Sign check.** The form compares your sensors over the last 24 hours of recorder statistics (current readings if there is no history) and says whether a sign looks wrong. It works only when a house load sensor is assigned, because it checks that PV + grid + battery adds up to the house consumption; without one there is no recommendation, for the grid or the battery. A house load sensor that Home Assistant calculates from other sensors (a template, a sum or a group helper) is not used for the check, because it would always add up. It is a recommendation: you decide.

**For automations**, the read-only action `volcast.propose_sensor_assignment` returns the same proposal and sign check and changes nothing. `config_entry_id` is optional when only one entry is paired. As the actions of an automation or script:

```yaml
actions:
  - action: volcast.propose_sensor_assignment
    data: {}
    response_variable: proposal
  - action: persistent_notification.create
    data:
      title: "Volcast sensor proposal"
      message: "Applicable: {{ proposal.applicable }}, flag changes: {{ proposal.flag_changes }}"
```

`applicable` is false when an inverter integration is recognised or a direct connection is set up, because then there is nothing to propose.

## Supported inverters

Profiles are data files that describe how to read, and where verified, control an inverter. They can be delivered to the integration without an update, so this list can grow between releases.

| Group | Inverters | What works |
|---|---|---|
| Verified on real hardware | GoodWe ET/EH/BT/BH (the GoodWe integration, or a direct connection) | Reading and control |
| Prepared, read-only until first tests | Deye SUN-xK-SG, FoxESS H1-G2/AC1-G2/P1, Huawei SUN2000, Sungrow SH, SolaX X1/X3-Hybrid, Sofar HYD, Solis hybrid, SolarEdge StorEdge, Fronius GEN24 | Reading and a trial that never writes; no control |
| Any other inverter visible in Home Assistant | Anything with sensors for power and state of charge | Reading, after you assign the sensors |

Control is enabled for a brand after its tests, and only when you ask for it (see below). Profiles that arrive after the release are read-only drafts until verified. The prepared brands use the integrations `foxess_modbus`, `fronius`, `huawei_solar`, `solarman`, `solax_modbus`, `solaredge_modbus` or a Modbus connection; their profiles only map readings from those integrations.

## Inverter control

Volcast can write the plan's mode and power settings to your inverter. This is off until you turn it on, and it works only for verified brands. For every other inverter the status sensor shows what Volcast **would** write, and nothing reaches the inverter.

Choose the method in **Configure → Inverter control**:

- **Through the inverter integration (entities)**: Volcast writes the mode and power entities of your inverter integration (for example a select for mode and a number for power).
- **Directly over the local network**: Volcast talks to the inverter itself (GoodWe over UDP, Modbus TCP or an RS485 gateway). Search under **Installation details → Search for the inverter on the local network** (read-only). The GoodWe Wi-Fi module must have Volcast as its only local client.
- **Off — forecast and monitoring only**.
- **Choose inverter entities manually** maps the entities yourself, with a check screen before saving.

Nothing is written unless all of these are true: a method other than Off is chosen, you gave consent in the Volcast app, the **Control** switch is on in Home Assistant, and the brand is verified. For direct control, the inverter's identity at the saved address must also be confirmed and no other client may use it.

A new device first goes through a verification ladder: a read-only trial that counts what it would write, a control write confirmed by read-back, and a short test window. Verified profiles start at the control write. If another controller writes the same settings, Volcast asks who controls the inverter; choosing your own controller keeps Volcast in plan-only mode.

### Safety behaviour

- **Daily limit** on setting changes per setting and in total (rolling 24 hours) protects the inverter's memory. When it is reached, plan writes are held; returning the inverter to its own settings is never blocked.
- **You change a setting yourself while control is on**: control pauses for 30 minutes, keeps your value and raises a repair. A clock-triggered schedule you run on the inverter counts as drift and is written back about a minute later; if your own automation should control these settings, turn the Volcast switch off. The pause does not survive a Home Assistant restart.
- **Consent withdrawn, switch off, or entry disabled or removed**: the inverter returns to how it was before Volcast took control (baseline mode, export limit and state-of-charge limits), confirmed by read-back for direct control. If you had taken the mode over yourself, the mode is left alone.
- **Every write is read back**; only values the inverter actually holds are recorded.
- **Stopping, unloading or reloading** tries to set the inverter's normal mode if it is in a forced mode set by Volcast, within 10 seconds (best effort). A control cycle that cannot run the plan safely does the same.
- **Repeated write failures**, a different device at the saved address, or another client on the connection stop control and raise a repair.
- **These safeguards only work while Home Assistant runs.** If Home Assistant or the host fails while the inverter is in a forced mode (grid charge or selling), it stays in that mode until Home Assistant is back.

Known limitations: single-phase Deye inverters are not identified; no serial port (use an RS485 to TCP gateway); local IP addresses only; on some GoodWe models the upper state-of-charge limit cannot be read back and is not set directly.

Direct-connection data: the address, port, unit ID, logger serial and a salted device fingerprint stay in Home Assistant; readings, profile id, connection type and rated power go to Volcast, never the address, serials or fingerprint.

## EV charger

Under **Configure → EV charger**, pick the chargers found by discovery and check the entity for each role (status, charging current or power limit, start/stop, power, energy).

- **Controlled charger**: it follows the charging slots of the Volcast plan. Volcast sets the current (a `number` entity) and starts or stops charging (a `select` or `switch` entity) of that charger only, under the same conditions as inverter control (consent, switch on, not in plan-only mode). Turning control off leaves the charger as it is.
- **Monitor-only charger**: a charger exposed as sensors only (status, power, energy) can be added as monitor-only. Its sessions are reported to Volcast; it is never controlled.

A controlled charger pauses (no commands, also no start) until a plan generated after the pause arrives when you or an automation stop charging during a charging slot or start it during an off slot, or when the charger stops by itself (its own app or button, end of charging, an error, unplugging, a car that reached its limit). A stop while the plan still needs energy is reported to the cloud, which recalculates within about half a minute. If the cloud does not answer for a long time, charging at full power starts when the ready time gets close, only when the cloud allowed it for the current plan. Devices of a read-only integration, or ones with a battery state-of-charge sensor, are not offered as chargers.

## Energy prices

Paired accounts on the Plan or Automate tier get two sensors, **Buy price** and **Sell price**: the current price per kWh in your market's currency, exactly as the Volcast plan and app use it (energy, variable distribution charges and VAT; fixed fees are not included). Each has today's and tomorrow's prices as an attribute list; tomorrow appears once prices are published. They are unavailable for accounts whose prices come from Home Assistant itself, and when the cloud reports no prices.

If your prices come from Home Assistant, choose them in **Configure → Energy prices** (purchase and sale price sensors that already provide a complete series) or **Price source from an integration**.

## Read-only integrations

Some integrations are described by a sensors-only profile. Volcast reads state of charge and PV, load, grid and battery power from them and never writes to them; control is not offered for such an integration. Profiles for these integrations arrive through the profile feed without a plugin update. If one becomes your chosen profile, a repair notice appears; confirm it and reload Volcast (or restart Home Assistant) to start reading. In entity mode, if the source reports identical load and grid values for 30 minutes, Volcast stops sending inverter readings until a value changes.

## Forecast

The sections from here to "Accuracy & Limitations" describe the forecast and are the same on both lines.

### Features

- **Energy Dashboard integration** — appears as a solar forecast source in the HA Energy Dashboard
- **7-day forecast** — daily energy (kWh) and peak power (kW) for up to 7 days
- **Hourly & 5-min data** — `detailedHourly` and `detailedForecast` attributes on every daily sensor
- **Live power estimate** — interpolated current power output (W)
- **Production tracking** — sends your actual production to Volcast for forecast calibration (from the inverter readings when they include PV power, otherwise from the PV sensors you choose in the Forecast options)
- **Nowcasting** — adjusts today's remaining forecast based on actual production so far
- **Curtailment detection** — uses battery SoC to detect inverter curtailment; affected hours are excluded from calibration so a capped system doesn't skew the forecast
- **Persistent retry queue** — production submissions that fail (network issues, API downtime) are queued locally and retried automatically
- **Peak production alert** — binary sensor for automations (configurable threshold)
- **UI-based setup** — no YAML needed: connect your Volcast account (or paste an API key) and select your sensors
- **Key sanity check at setup** — a pasted key that is not a real Volcast key (for example the shortened preview shown in the app) is rejected instantly with a clear hint, before any network call

### Resilience & Reconciliation

The integration has three layered resilience mechanisms:

- **Forecast retry** — short network blips no longer flap entities to `unavailable`. The integration retries failed forecast fetches with 5/15/45s backoff inside one update cycle.
- **Submit retry** — hourly production submits use the same 5/15/45 retry pattern before queuing. Persistent retry queue still kicks in for longer outages.
- **Daily reconciliation** — the integration reads hourly statistics from HA Recorder and fills any gaps in your Volcast cloud history. The scheduled run at 00:30 local each day backfills the **previous day**. Additionally, on **HA startup** it reconciles **both yesterday and today**, so a gap left by an HA restart or update during the day self-heals immediately instead of waiting for 00:30. Requires `energy_entity` configured with `state_class: total_increasing`.

Diagnostic entities (under integration → Diagnostic):
- `sensor.volcast_submit_queue_depth` — readings waiting to be retried (0 = healthy).
- `sensor.volcast_last_reconciliation` — timestamp + status of last daily backfill.
- `binary_sensor.volcast_integration_healthy` — single signal you can wire into automations.

Power-only users (no `energy_entity` configured): retry mechanisms apply, but daily reconciliation is **not** active.

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

**After a data gap:** if Home Assistant or the inverter integration was
unavailable for a while (a restart, an update, an offline inverter), the
integration remembers the last known counter value. When readings return, it
reports the whole period the reading covers (up to 36 hours) and Volcast
spreads that energy over the hours of the gap, instead of attributing it to a
single hour. Hours already covered by such a report are not re-sent by the
nightly reconciliation. Once a day is closed, the integration also sends that
day's total calculated from Home Assistant statistics — only when the
statistics for the day are complete — so the day can be corrected after a gap.
Sending it again gives the same value. On the 2.0 line this recovery is available from beta 15.

_(The button and service require an energy sensor configured — power-only
users have nothing to reconcile.)_

## How It Works

### Forecast Model

Volcast uses a physics-based PV simulation model fed by a multi-model weather ensemble. The models depend on your location:

- **Most regions**: a regional model (for example ICON, AROME, UKMO, JMA, HRRR for the contiguous USA) blended with ECMWF IFS. The regional model dominates the first 48 hours; later, ECMWF IFS takes over, and beyond the regional model's range it is used alone.
- **No regional model available**: ECMWF IFS blended with GFS.
- **Netherlands / Benelux**: five models (KNMI, DMI, ICON-D2, AROME France, ECMWF IFS) averaged with equal weights over the hours each one covers.

The integration polls the Volcast cloud API at a configurable interval (default: 60 minutes). Data is served from a server-side cache that is refreshed about once an hour, the same cache the Volcast mobile app reads.

### Production Tracking & Calibration

When you connect your inverter's energy or power sensor, the integration sends **hourly production summaries** to Volcast. This data drives two mechanisms:

**Kalman filter calibration** — Volcast maintains a per-user bias estimate that adjusts forecasts based on how your actual production compares to predictions. The filter uses hourly cloud cover to apply different corrections for clear vs. cloudy conditions. This means the forecast learns your system's real-world characteristics (shading, soiling, inverter efficiency) over time. The correction is applied to future days and to the remaining hours of today (hours after the current one); hours that have already passed keep the forecast issued earlier.

**Nowcasting** — Once at least one recent hourly reading for today shows production (above 0.01 kWh) in an hour with a forecast above 0.5 kWh, Volcast computes an actual-to-forecast ratio from up to the last two such hours and adjusts the remaining hours of today's forecast. The adjustment halves with every hour after the last reading, so it has the strongest effect on the next few hours. This helps when conditions differ from the morning forecast — for example, unexpected cloud cover or clearer skies than predicted. Nowcast resets daily.

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

## Forecast setup

The forecast works as soon as the entry is set up. To improve it with your own production data, open **Configure → Volcast Options → Forecast** (all sensors are optional and can be changed later):

- **Today's PV generation (kWh)**: a sensor with your solar production, a daily counter or a lifetime total (for example "Today's PV Generation" or "Daily Yield"). Preferred source for calibration.
- **Current PV power (W)**: real-time power output, used as a fallback when the energy sensor is unavailable.
- **Battery state of charge (%)**: enables detection of inverter curtailment when the battery is full.
- **Battery charge power (W)**: power to or from the battery; improves curtailment accuracy.

While inverter readings include PV power (a recognised integration or a direct connection), calibration uses them and the PV sensors above are not used; you can leave or clear them.

### Configuration options

After setup, **Configure** opens **Volcast Options** with the menu **Forecast**, **Inverter control**, **Installation details**, **Energy prices**, **EV charger** and **Price source from an integration**. The **Forecast** page has:

| Option | Default | Range | Description |
|--------|---------|-------|-------------|
| Update interval | 60 min | 15–1440 | How often to poll the API |
| Peak threshold | 80% | 50–100 | Threshold for peak production binary sensor |
| PV energy sensor | — | — | Today's generation sensor (kWh) |
| PV power sensor | — | — | Current power sensor (W, fallback) |
| Battery SoC sensor | — | — | State of charge (%), enables curtailment detection |
| Battery charge power sensor | — | — | Battery power flow (W), improves curtailment accuracy |

## Energy Dashboard

1. Go to **Settings > Dashboards > Energy**
2. Under **Solar panels**, click **Add solar forecast**
3. Select **Volcast Solar Forecast**
4. Your forecast will appear on the Energy Dashboard

The integration retains a rolling forecast history (last 14 days), so when you
navigate back to previous days the Energy Dashboard still shows what was
forecast for those days — not just today and the future. The history is
persisted across restarts.

## Sensor Attributes

Each daily energy sensor (`energy_today`, `energy_tomorrow`, days 3–7) exposes rich attributes for advanced automations:

| Attribute | Format | Description |
|-----------|--------|-------------|
| `hours` | `[{"hour": 10, "power_kw": 3.5, "energy_kwh": 3.2}, ...]` | Hourly breakdown (24 entries) |
| `detailedHourly` | `[{"period_start": "ISO8601", "power_kw": 3.5, "energy_kwh": 3.2}, ...]` | Hourly with ISO timestamps (Solcast-compatible) |
| `detailedForecast` | `[{"period_start": "ISO8601", "power_w": 3500, "energy_wh": 292}, ...]` | 5-minute granularity, today and tomorrow only |
| `peak_power_kw` | `float` | Day's peak power |
| `confidence` | `high` / `medium` / `low` | Forecast confidence level |
| `sunshine_hours` | `int` | Number of forecast hours with non-zero power |
| `cloud_cover_pct` | `float` | Average cloud cover percentage |

The `energy_today` sensor additionally includes a `forecast` attribute with a 7-day daily summary.

### Accessing hourly data in templates

```jinja
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

The examples use the automation syntax of Home Assistant 2024.10 and newer. On older versions, rename `triggers:` to `trigger:`, `- trigger: <type>` to `- platform: <type>`, `actions:` to `action:` and `- action: <service>` to `- service: <service>`.

### Notify when tomorrow's forecast is high

```yaml
automation:
  - alias: "High solar forecast tomorrow"
    triggers:
      - trigger: numeric_state
        entity_id: sensor.volcast_energy_forecast_tomorrow
        above: 20
    actions:
      - action: notify.mobile_app
        data:
          title: "Solar forecast"
          message: "Tomorrow: {{ states('sensor.volcast_energy_forecast_tomorrow') }} kWh expected"
```

### Start EV charging during peak production

```yaml
automation:
  - alias: "Charge EV during peak solar"
    triggers:
      - trigger: state
        entity_id: binary_sensor.volcast_peak_production
        to: "on"
    actions:
      - action: switch.turn_on
        target:
          entity_id: switch.ev_charger
```

### Run appliances when nowcast shows surplus

```yaml
automation:
  - alias: "Run washing machine during high production"
    triggers:
      - trigger: numeric_state
        entity_id: sensor.volcast_power_now
        above: 3000
        for: "00:10:00"
    actions:
      - action: switch.turn_on
        target:
          entity_id: switch.washing_machine
```

## Accuracy & Limitations

- **Forecast horizon**: Accuracy is highest for today and tomorrow. Days 5–7 are less reliable, especially in variable weather.
- **Calibration ramp-up**: Calibration learns from your production data, so forecasts start uncalibrated and improve as data accumulates.
- **Nowcast availability**: Requires a recent hourly reading with meaningful production (>0.01 kWh) in an hour with a forecast above 0.5 kWh. Early morning hours or heavily overcast days may not produce enough data.
- **Sensor compatibility**: Works with any inverter that exposes an energy or power entity in HA.

## Privacy and diagnostics

**Discovery** runs once after each Home Assistant start and after each reload. It reads your installed inverter and price integrations (device and entity registries, entity names, units and options), the inverter's host address from its settings (no passwords), how many days of long-term statistics your energy sensors have, and sends one UDP broadcast on port 48899 to find Wi-Fi data loggers on your local network. It adds an **Installation discovery** sensor and a **Run discovery** button to the Volcast device.

For an entry that is not paired with an account, the discovery result stays in Home Assistant and nothing is sent to Volcast. A paired entry sends the data listed under [Connect your account](#connect-your-account-pairing).

The downloadable diagnostics file masks serial numbers, MAC addresses and e-mail addresses (IP addresses and entity names are kept, since they help with troubleshooting), so review it before sharing. To get it: **Settings → Devices & services → Volcast → ⋮ → Download diagnostics**.

## Support

- **Issues**: [GitHub Issues](https://github.com/volter-labs/volcast-ha-integration/issues)
- **Release notes**: [GitHub Releases](https://github.com/volter-labs/volcast-ha-integration/releases)
- **App support**: In-app chat (Settings > Help)
- **Website**: [volcast.app](https://volcast.app)

## License

MIT — see [LICENSE](LICENSE)
