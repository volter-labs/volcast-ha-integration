# Unreleased

## What's New

### Control through an existing Home Assistant Modbus hub

- Deye SG LV three-phase inverters can now be controlled through a Modbus hub that is already configured in Home Assistant, instead of a separate direct connection. The option is opt-in, available in the integration options, and is offered only when the Home Assistant `modbus` integration is loaded.
- A raw-sensor YAML template is provided for the hub's register sensors, so the integration can read the registers it needs without scaling or other transformations.
- Before any control is enabled, the integration runs a read-only check of the hub and the sensors. Nothing is written to the inverter during the check. Problems found are listed in the hub step of the options.
- Known limitation in this beta: the problems list in the hub step is shown in English only.

### Fixes

- Time-window verification: fixed how the integration verifies that a written time window took effect. This also affects direct Deye control.
- Foreign-write detection: fixed detection of writes to the inverter made by something other than the integration.

## Upgrading

Update via HACS and restart Home Assistant. Existing direct and entity-mode setups are not changed.
