# Unreleased — next pre-release

## What's new

- **Read-only integrations (sensors only)** — some integrations are described by a profile that uses sensors only. Volcast reads state of charge and PV, load, grid and battery power from them and never writes to them: no numbers, selects or switches are used, and control is not offered for such an integration.
- **New read-only integrations from the profile feed** — a profile for this kind of integration arrives through the profile feed, so no plugin update is needed. When it arrives, the existing "test profile available" repair notice appears; reading starts after the integration is reloaded.
- **Frozen readings are not sent** — in entity mode, if the source keeps reporting identical load and grid values for 30 minutes, Volcast stops sending inverter readings until a value changes. Diagnostics shows this state. Known limitation: while inverter readings are unavailable or frozen and there are no prices to send, EV charger data is not sent either.
- **Monitor-only EV chargers** — chargers exposed as sensors only (status, power, energy, no control entities) can be added as monitor-only chargers. Their sessions are reported; the charger is never controlled.
- **EV charger detection** — devices of an inverter integration are no longer offered as EV chargers when they belong to a read-only integration or have a battery state-of-charge sensor.
