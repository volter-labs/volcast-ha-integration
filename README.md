# Volcast Solar Forecast

[![HACS Validation](https://github.com/volter-labs/volcast-ha-integration/actions/workflows/hacs.yaml/badge.svg)](https://github.com/volter-labs/volcast-ha-integration/actions/workflows/hacs.yaml)
[![hassfest](https://github.com/volter-labs/volcast-ha-integration/actions/workflows/hassfest.yaml/badge.svg)](https://github.com/volter-labs/volcast-ha-integration/actions/workflows/hassfest.yaml)

Home Assistant integration for [Volcast](https://volcast.app) — high-accuracy solar PV production forecasts powered by multi-model weather ensemble and Kalman filter calibration.

## Features

- **Energy Dashboard integration** — appears as a solar forecast source in the HA Energy Dashboard
- **7-day forecast** — daily energy (kWh) and peak power (kW)
- **Hourly & 5-min data** — `detailedHourly` and `detailedForecast` attributes on every daily sensor
- **Live power estimate** — interpolated current power output (W)
- **Peak production alert** — binary sensor for automations (configurable threshold)
- **UI-based setup** — no YAML needed, just enter your API key

## Sensors

| Entity | Type | Description |
|--------|------|-------------|
| `sensor.volcast_energy_forecast_today` | Energy (kWh) | Today's total forecasted production |
| `sensor.volcast_energy_forecast_tomorrow` | Energy (kWh) | Tomorrow's total forecasted production |
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
4. Done — sensors will appear automatically

### Energy Dashboard

1. Go to **Settings > Dashboards > Energy**
2. Under **Solar panels**, click **Add solar forecast**
3. Select **Volcast Solar Forecast**
4. Your forecast will appear on the Energy Dashboard

## Configuration Options

After setup, click **Configure** on the integration to adjust:

| Option | Default | Range | Description |
|--------|---------|-------|-------------|
| Update interval | 60 min | 15–1440 | How often to poll the API |
| Peak threshold | 80% | 50–100 | Threshold for peak production binary sensor |

## Sensor Attributes

Each daily energy sensor (`energy_today`, `energy_tomorrow`, days 3–7) exposes rich attributes for advanced automations:

| Attribute | Format | Description |
|-----------|--------|-------------|
| `hours` | `[{"hour": 10, "power_kw": 3.5, "energy_kwh": 3.2}, ...]` | Hourly breakdown (24 entries) |
| `detailedHourly` | `[{"period_start": "ISO8601", "power_kw": 3.5, "energy_kwh": 3.2}, ...]` | Hourly with ISO timestamps (Solcast-compatible) |
| `detailedForecast` | `[{"period_start": "ISO8601", "power_w": 3500, "energy_wh": 292}, ...]` | 5-minute granularity (Premium, API v2) |
| `peak_power_kw` | `float` | Day's peak power |
| `confidence` | `float` | Forecast confidence |
| `sunshine_hours` | `float` | Expected sunshine hours |
| `cloud_cover_pct` | `float` | Cloud cover percentage |

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

## How It Works

Volcast uses a multi-model weather ensemble (ECMWF, GFS, ICON, and regional models) combined with a physics-based PV simulation model. The forecast is calibrated against your actual production data using a Kalman filter, improving accuracy over time.

The integration polls the Volcast cloud API at a configurable interval (default: 60 minutes). Data is served from cache when available, ensuring the values match exactly what you see in the Volcast mobile app.

## Support

- **Issues**: [GitHub Issues](https://github.com/volter-labs/volcast-ha-integration/issues)
- **App support**: In-app chat (Settings > Help)

## License

MIT — see [LICENSE](LICENSE)
