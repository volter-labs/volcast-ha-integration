# Volcast Solar Forecast

[![HACS Validation](https://github.com/volter-labs/volcast-ha-integration/actions/workflows/hacs.yaml/badge.svg)](https://github.com/volter-labs/volcast-ha-integration/actions/workflows/hacs.yaml)
[![hassfest](https://github.com/volter-labs/volcast-ha-integration/actions/workflows/hassfest.yaml/badge.svg)](https://github.com/volter-labs/volcast-ha-integration/actions/workflows/hassfest.yaml)

Home Assistant integration for [Volcast](https://volcast.app) — solar PV production forecasts powered by multi-model weather ensemble, Kalman filter calibration, and real-time nowcasting.

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
Sending it again gives the same value.

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

## Prerequisites

1. **Volcast app** — download from [App Store](https://apps.apple.com/app/id6758962799) or [Google Play](https://play.google.com/store/apps/details?id=com.volcast.app)
2. **Premium subscription** — required for API access
3. **API key** — generate it in the app: Settings > **API Key** card (shown as "Premium required" without a subscription)

Requires Home Assistant 2024.1.0 or newer.

## Installation

### HACS (recommended)

1. Open HACS in your Home Assistant
2. Search for **Volcast Solar Forecast** and open it
3. Click **Download**
4. Restart Home Assistant

### Beta line (2.0)

A newer 2.0 line is in beta. It is installed as a pre-release: in HACS enable pre-releases for this repository, and in Home Assistant 2026.11 and newer use the Marketplace. See the [GitHub Releases](https://github.com/volter-labs/volcast-ha-integration/releases) page for the available versions and release notes. This README describes the stable line.

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
| `detailedForecast` | `[{"period_start": "ISO8601", "power_w": 3500, "energy_wh": 292}, ...]` | 5-minute granularity, today and tomorrow only |
| `peak_power_kw` | `float` | Day's peak power |
| `confidence` | `high` / `medium` / `low` | Forecast confidence level |
| `sunshine_hours` | `int` | Number of forecast hours with non-zero power |
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

## Support

- **Issues**: [GitHub Issues](https://github.com/volter-labs/volcast-ha-integration/issues)
- **App support**: In-app chat (Settings > Help)
- **Website**: [volcast.app](https://volcast.app)

## License

MIT — see [LICENSE](LICENSE)
