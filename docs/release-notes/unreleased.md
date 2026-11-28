# v2.0.0-beta18 — Volcast 2.0 beta (draft, not released)

This is a **pre-release** for testers of the Volcast 2.0 app. It replaces all earlier 2.0.0 betas. The stable line
is unchanged; stay on it unless you were invited to the 2.0 beta.

## What's new

- **Control through an existing Home Assistant Modbus hub** — Deye SG LV three-phase inverters can now be controlled through a Modbus hub that is already configured in Home Assistant, instead of a separate direct connection. Writes go through the hub's `modbus.write_register` service, so no second connection to the inverter is opened. The option is opt-in, appears in the integration options as the "Modbus hub" step, and is offered only when the Home Assistant `modbus` integration is loaded. All safety checks of direct control apply unchanged (device identity, verification, detection of writes made by something else, time-of-use backup and restore).
- **Raw register sensors** — the hub mode reads the inverter from raw register sensors you add to your existing hub. A ready template is in `docs/profiles/deye-sg-ha-modbus.yaml` (51 registers, `unique_id: volcast_reg_<address>`, holding, `uint16`, no scaling). Sensors must belong to the same hub and device address; values restored after a Home Assistant restart are not treated as fresh readings.
- **Read-only check before control** — before control can be enabled, Volcast checks the hub and the sensors and compares them with your existing state-of-charge and battery power sensors. Nothing is written to the inverter during the check. Problems are listed in the hub step.
- **Stale plan safety** — when the last successful plan download is older than 60 minutes, active plan slots (charging from the grid, selling from the battery, forced discharge) are no longer executed; the inverter follows the plan's fallback instead. Passive slots are unchanged. The time of the last download survives a restart; a plan of unknown age counts as stale. The plan sensor shows `plan_fetched_at` and `plan_stale`.
- **Plan sensor attributes** — slots carry `buy_price` and `sell_price`, and the sensor shows the plan `strategy` when the server provides it.
- **Today's energy attributes** — the energy-today sensor shows `actual_so_far_kwh`, `remaining_kwh` and `expected_total_kwh` when the server sends them. The sensor state is unchanged.
- **Live data without an inverter profile** — setups with sensors assigned by hand now get live mode too.

## Fixes

- **Price source selection** — the integration's own entities are no longer offered as a buy or sell price source and are rejected when submitted.
- **Time-window verification** — fixed how Volcast verifies that a written time window took effect; the check no longer needs a mode register. This also applies to direct Deye control.
- **Detection of foreign writes** — changes to the time-of-use block made by something other than Volcast are now detected reliably.
- **Orphaned diagnostic entities** — the "submit queue depth" and "last reconciliation" diagnostic sensors no longer stay behind as unavailable when they are not used; they come back when they are needed again.

## Known limitations

- The problems list in the Modbus hub step is shown in English only in this beta.

## Upgrading from an earlier 2.0.0 beta

Update in HACS or in the Home Assistant Marketplace, then restart Home Assistant. Your forecast entities and history are kept. Existing direct and entity-mode setups are not changed.
