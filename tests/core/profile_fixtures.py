"""Minimalne POPRAWNE profile do testów. Każdy test mutuje własną kopię."""
import copy

_INT = ("charge_grid", "charge_pv", "discharge_forced", "sell", "self_consume", "standby")
_CAPS = {"force_charge_from_grid": True, "sell_from_battery": True, "force_discharge": True,
         "standby": True, "set_power_w": True, "limit_export": True, "set_soc_floor": True,
         "set_soc_ceiling": True}

_MS = {
    "schema_version": 1, "id": "test-ms", "label": "Test MS", "status": "verified",
    "control_model": "mode_setpoint", "sources": [{"what": "test", "ref": "test"}],
    "unit_id": 247, "transports": ["goodwe_udp"],
    "identify": {"model_register": {"addr": 35011, "type": "ascii", "len": 5},
                 "model_regex": ["^GW"], "registers": {"rated_power_w": {"addr": 35001, "type": "u16"}}},
    "read": {"soc": {"addr": 37007, "type": "u16"},
             "active_power_w": {"addr": 35140, "type": "i16"},
             "pv_power_w": {"sum": [{"addr": 35105, "type": "u32", "undef": 4294967295}]},
             "load_power_w": {"sum": [{"ref": "pv_power_w"}, {"ref": "active_power_w", "sign": -1}]}},
    "write": {"mode": {"addr": 47511, "type": "u16", "encode": "mode"},
              "power_w": {"addr": 47512, "type": "u16", "encode": "watts"},
              "soc_min": {"addr": 45356, "type": "u16", "encode": "percent"},
              "export_limit_w": {"addr": 47510, "type": "u16", "encode": "watts"},
              "export_limit_enabled": {"addr": 47509, "type": "u16", "encode": "bool"}},
    "modes": {"auto": {"value": 1, "direction": "neutral", "ha_option": "auto"},
              "charge_battery": {"value": 11, "direction": "charge", "ha_option": "charge_battery"},
              "sell_power": {"value": 10, "direction": "discharge", "ha_option": "sell_power"},
              "discharge_battery": {"value": 12, "direction": "discharge", "ha_option": "discharge_battery"},
              "battery_standby": {"value": 8, "direction": "idle", "ha_option": "battery_standby"}},
    "neutral_mode": "auto",
    "intents": {"charge_grid": {"mode": "charge_battery", "power": "slot"},
                "charge_pv": {"mode": "auto", "power": "none"},
                "discharge_forced": {"mode": "discharge_battery", "power": "slot"},
                "sell": {"mode": "sell_power", "power": "slot"},
                "self_consume": {"mode": "auto", "power": "none"},
                "standby": {"mode": "battery_standby", "power": "zero"}},
    "baseline": {"mode": "auto", "export_limit_enabled": False},
    "write_policy": {"order": ["soc_min", "power_w", "export_limit_w", "export_limit_enabled", "mode"],
                     "min_interval_s": 60, "nvm": True, "max_direction_changes_per_hour": 4,
                     "max_state_age_s": 300},
    "limits": {"battery_temp_c": {"min": -10, "max": 55}, "rated_power_register": "rated_power_w"},
    "ha": {"integrations": [{"domain": "goodwe", "ems": True, "status": "draft", "entities": {
        "soc": {"domain": "sensor", "unique_id_regex": "^goodwe-battery_soc-"},
        "soc_min": {"domain": "number", "unique_id_regex": "^goodwe-battery_discharge_depth-",
                    "transform": "invert_percent"}}}]},
    "capabilities": {**_CAPS, "time_windows": 0},
}

_TW = {
    "schema_version": 1, "id": "test-tw", "label": "Test TW", "status": "draft",
    "control_model": "time_window", "sources": [{"what": "test", "ref": "test"}],
    "unit_id": 1, "transports": ["solarman_v5"],
    "identify": {"model_register": {"addr": 3, "type": "ascii", "len": 5},
                 "model_regex": ["."], "registers": {"rated_power_w": {"addr": 16, "type": "u32", "scale": 0.1}}},
    "read": {"soc": {"addr": 184, "type": "u16"}},
    "write": {"tou_program": {"count": 6, "start": {"addr": 148, "encode": "hhmm"},
                              "power_w": {"addr": 154, "encode": "watts"},
                              "soc": {"addr": 166, "encode": "percent"},
                              "grid_charge": {"addr": 172, "bit": 0}}},
    "intents": {"charge_grid": {"grid_charge": True, "soc": "target_or_max", "power": "slot"},
                "charge_pv": {"grid_charge": False, "soc": "reserve", "power": "max"},
                "discharge_forced": None, "sell": None,
                "self_consume": {"grid_charge": False, "soc": "reserve", "power": "max"},
                "standby": {"grid_charge": False, "soc": "hold", "power": "max"}},
    "tou": {"programs": 6, "time_step_min": 5, "soc_tolerance_pp": 5, "power_tolerance_w": 500,
            "field_order": ["soc", "power_w", "grid_charge", "start"]},
    "baseline": {"intent": "self_consume"},
    "write_policy": {"order": ["tou"], "min_interval_s": 300, "nvm": True,
                     "max_direction_changes_per_hour": 0, "max_state_age_s": 300},
    "limits": {"battery_temp_c": {"min": -10, "max": 55}, "rated_power_register": "rated_power_w"},
    "ha": {"integrations": []},
    "capabilities": {**_CAPS, "sell_from_battery": False, "force_discharge": False,
                     "time_windows": 6},
}


def ms_profile() -> dict:
    return copy.deepcopy(_MS)


def tw_profile() -> dict:
    return copy.deepcopy(_TW)
