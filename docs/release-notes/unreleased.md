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
- **Respecting your own time-of-use changes (Deye)** — a change you make to a time-of-use program or to the schedule switch (for example in the inverter vendor app) is now respected after the first change. Volcast stops writing that setting and pauses control for 30 minutes, as it already did after a second change. When control is turned off, a setting is restored from the saved copy only if it still holds the value Volcast last wrote; a setting you changed stays as you set it, also after a Home Assistant restart. Such settings are listed in the `restore_skipped` attribute of the control status sensor, and the events `volcast_control_takeover` and `volcast_control_restore_skipped` are fired. Mode and setpoint inverters such as GoodWe still treat only a second change as a takeover.
- **Deye Sell bit** — the Deye profile now knows the per-program Sell bit (bit 5 of registers 172–177). Selling from the battery stays switched off for Deye until it is confirmed on a live unit, so Volcast does not plan sell windows yet. When Volcast controls the schedule it clears Sell bits on programs outside planned sell windows (also in fallback and stale-plan windows; a sell window is never stretched over other hours); other bits of the register, such as Gen, are left as they are. If you set Sell on a program yourself while Volcast controls the schedule, Volcast does not overwrite it: it turns the schedule off (plain self-consumption) until the bit is cleared again or the plan sells in that program. Turning control off restores the bit from the saved copy unless you changed it.
- **Read-back diagnostics** — register write read-back problems (ERROR or DENIED) are now visible: the control status sensor has a `readback_issues` attribute with the last 5 items (`key`, `expected`, `read`, `result`, `at`), cleared per key after a successful write, and a `volcast_control_readback` event is fired for each one.
- **Faster start after a restart** — after a Home Assistant restart the first control cycle waits up to 30 seconds for the first plan download, so the fallback is not written just before the plan.
- **Missing grid or house load data** — when your inverter is recognised but Volcast gets no grid power or no house load power from it (for example a power meter that is not visible in Home Assistant), a Repairs issue now explains that the energy balance, the live view and the forecast calibration from inverter data are limited, and lets you pick the missing sensors. Channels that your inverter integration does not provide at all (for example house load in the Huawei Solar integration) are not reported, because there is nothing to pick. Until grid and house load power (and battery power, when you have a battery) are available, the integration keeps calibrating the forecast from the PV sensors mapped in the Forecast options, instead of relying on inverter telemetry the server cannot use for calibration.
- **Live data without an inverter profile** — setups with sensors assigned by hand now get live mode too.

## Fixes

- **Price source selection** — the integration's own entities are no longer offered as a buy or sell price source and are rejected when submitted.
- **Time-window verification** — fixed how Volcast verifies that a written time window took effect; the check no longer needs a mode register. This also applies to direct Deye control.
- **Detection of foreign writes** — changes to the time-of-use block made by something other than Volcast are now detected reliably.
- **Orphaned diagnostic entities** — the "submit queue depth" and "last reconciliation" diagnostic sensors no longer stay behind as unavailable when they are not used; they come back when they are needed again.

## Known limitations

- Time-window control (Deye): any single change of a program value that Volcast did not make counts as a takeover, including a change the inverter makes by itself (for example firmware adjusting a written value later). That setting is then kept and not restored when control is turned off; the values are logged at INFO level. If you see a takeover you did not cause, please report it.
- After a restart the 60-minute plan age limit can effectively be 45–60 minutes, because the download time is saved at most every 15 minutes.
- The problems list in the Modbus hub step is shown in English only in this beta.

## Upgrading from an earlier 2.0.0 beta

Update in HACS or in the Home Assistant Marketplace, then restart Home Assistant. Your forecast entities and history are kept. Existing direct and entity-mode setups are not changed.
