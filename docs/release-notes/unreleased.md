# Unreleased

## What's New

### Control through an existing Home Assistant Modbus hub

- Deye SG LV three-phase inverters can now be controlled through a Modbus hub that is already configured in Home Assistant, instead of a separate direct connection. The option is opt-in, available in the integration options, and is offered only when the Home Assistant `modbus` integration is loaded.
- A raw-sensor YAML template is provided for the hub's register sensors, so the integration can read the registers it needs without scaling or other transformations.
- Before any control is enabled, the integration runs a read-only check of the hub and the sensors. Nothing is written to the inverter during the check. Problems found are listed in the hub step of the options.
- Known limitation in this beta: the problems list in the hub step is shown in English only.

### Diagnostics

- Register write read-back problems (ERROR or DENIED) are now visible: the control Status entity has a `readback_issues` attribute with the last 5 items (`key`, `expected`, `read`, `result`, `at`), cleared per key after a successful write, and a `volcast_control_readback` event is fired for each one.

### Fixes

- Missing grid or house load data: when your inverter is recognised but Volcast gets no grid power or no house load power from it (for example a power meter that is not visible in Home Assistant), a Repairs issue now explains that the energy balance, the live view and the forecast calibration from inverter data are limited, and lets you pick the missing sensors. Channels that your inverter integration does not provide at all (for example house load in the Huawei Solar integration) are not reported, because there is nothing to pick. Until grid and house load power (and battery power, when you have a battery) are available, the integration keeps calibrating the forecast from the PV sensors mapped in the Forecast options, instead of relying on inverter telemetry the server cannot use for calibration.
- Time-window verification: fixed how the integration verifies that a written time window took effect. This also affects direct Deye control.
- Foreign-write detection: fixed detection of writes to the inverter made by something other than the integration.
- Time-window control (Deye): a change you make to a time-of-use program or to the schedule switch (for example in the inverter vendor app) is now respected after the first change. Volcast stops writing that setting and pauses control for 30 minutes, as it already did after a second change. When control is turned off, the integration restores a setting from its saved copy only if the setting still holds the value Volcast last wrote. A setting you changed stays as you set it, also after a Home Assistant restart. Settings left this way are listed in the `restore_skipped` attribute of the control status sensor. Two Home Assistant events are fired: `volcast_control_takeover` and `volcast_control_restore_skipped`. Mode and setpoint inverters such as GoodWe still treat only a second change as a takeover.
- Caution for time-window control (Deye): any single change of a program value that Volcast did not make now counts as a takeover. This includes a change the inverter makes by itself, for example firmware adjusting a written value some time after the write. The setting is then kept as it is and is not restored when control is turned off. The values involved are logged at INFO level. If you see a takeover you did not cause, please report it.
- Time-window control (Deye): the profile now knows the per-program Sell bit (bit 5 of registers 172–177). Selling from the battery stays switched off for Deye until it is confirmed on a live unit, so Volcast does not plan sell windows yet. When Volcast takes control, it clears Sell bits left on programs outside planned sell windows (also in fallback and stale-plan windows, and a sell window is never stretched over other hours); other bits of the register (for example Gen) are left as they are. If you set Sell on a program yourself while Volcast controls the schedule, Volcast does not overwrite it: it turns the schedule off (plain self-consumption) and keeps it off until the bit is cleared again or the plan sells in that program. Turning control off restores the bit from the saved copy unless you changed it.

## Upgrading

Update via HACS and restart Home Assistant. Existing direct and entity-mode setups are not changed.
