# Volcast Solar Forecast

[![HACS Validation](https://github.com/volter-labs/volcast-ha-integration/actions/workflows/hacs.yaml/badge.svg)](https://github.com/volter-labs/volcast-ha-integration/actions/workflows/hacs.yaml)
[![hassfest](https://github.com/volter-labs/volcast-ha-integration/actions/workflows/hassfest.yaml/badge.svg)](https://github.com/volter-labs/volcast-ha-integration/actions/workflows/hassfest.yaml)

Home Assistant integration for [Volcast](https://volcast.app) — high-accuracy solar PV production forecasts powered by multi-model weather ensemble and Kalman filter calibration.

## Features

- **Energy Dashboard integration** — appears as a solar forecast source in the HA Energy Dashboard
- **7-day forecast** — daily energy (kWh) and peak power (kW)
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

## Direct API Usage (without Home Assistant)

You can call the Volcast forecast API directly using curl or any HTTP client — no Home Assistant or HACS required.

**Important:** The API is hosted at `https://jzihchpmkhawegqcfbeo.supabase.co`, **not** at `volcast.app` (which is the marketing website). Calling `volcast.app/api/...` will return a 404 error.

### Endpoint

```
GET https://jzihchpmkhawegqcfbeo.supabase.co/functions/v1/get-forecast-api?key=YOUR_API_KEY
```

### Example

```bash
curl "https://jzihchpmkhawegqcfbeo.supabase.co/functions/v1/get-forecast-api?key=vk_your_api_key"
```

### Response

The API returns JSON with the following structure:

```json
{
  "state": 12.5,
  "attributes": {
    "forecast": [
      {
        "date": "2025-07-15",
        "energy_kwh": 12.5,
        "peak_power_kw": 3.2,
        "confidence": 0.85,
        "sunshine_hours": 8.5,
        "cloud_cover_pct": 25
      }
    ],
    "hourly": {
      "2025-07-15": [
        { "hour": 8, "power_kw": 1.2, "energy_kwh": 0.9 }
      ]
    },
    "detailed": {
      "2025-07-15": [
        { "time": "08:00", "power_w": 1200, "energy_wh": 100 }
      ]
    },
    "system_capacity_kwp": 5.0,
    "location": "Warsaw, Poland",
    "generated_at": "2025-07-15T06:00:00Z",
    "cache_age_minutes": 5,
    "api_version": 2
  }
}
```

| Field | Description |
|-------|-------------|
| `state` | Today's total forecasted energy (kWh) |
| `attributes.forecast` | 7-day daily forecast |
| `attributes.hourly` | Hourly breakdown per day |
| `attributes.detailed` | 5-minute resolution data (today + tomorrow, API v2) |

### Common Errors

| HTTP Status | Meaning |
|-------------|---------|
| 401 | Invalid API key |
| 403 | Premium subscription required |
| 429 | Rate limit exceeded — retry later |
| 503 | Forecast not yet available — cache being populated |

## How It Works

Volcast uses a multi-model weather ensemble (ECMWF, GFS, ICON, and regional models) combined with a physics-based PV simulation model. The forecast is calibrated against your actual production data using a Kalman filter, improving accuracy over time.

The integration polls the Volcast cloud API at a configurable interval (default: 60 minutes). Data is served from cache when available, ensuring the values match exactly what you see in the Volcast mobile app.

## Support

- **Issues**: [GitHub Issues](https://github.com/volter-labs/volcast-ha-integration/issues)
- **App support**: In-app chat (Settings > Help)

## License

MIT — see [LICENSE](LICENSE)
