"""Odczyt potwierdzonych ładowarek EV do bloku `loads` telemetrii.

Czyta encje ról zapisanych w opcjach (`ev_chargers`) i przekazuje je dalej bez interpretacji:
stan encji statusu idzie jako tekst, tak jak go podaje HA. Jedyna obróbka to przeliczenie
jednostek (moc do W, energia do kWh, nastawa do A albo W). Nieznana jednostka nie jest zgadywana.
"""
from __future__ import annotations

import math
from typing import Any

_UNAVAILABLE = ("unavailable", "unknown")
_MAX_LOADS = 4
_POWER_TO_W = {"w": 1.0, "kw": 1e3, "mw": 1e6}
_ENERGY_TO_KWH = {"wh": 1e-3, "kwh": 1.0, "mwh": 1e3}
_SETPOINT_UNITS = {"a": ("A", 1.0), "w": ("W", 1.0), "kw": ("W", 1e3)}


def _num(v) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _unit(state) -> str | None:
    u = state.attributes.get("unit_of_measurement")
    return u.strip().lower() if isinstance(u, str) else None


class LoadsReader:
    def __init__(self, hass, entry) -> None:
        self._hass, self._entry = hass, entry

    def read(self) -> list[dict] | None:
        """Wpisy bloku `loads`; None, gdy nie ma potwierdzonej ładowarki."""
        saved = self._entry.options.get("ev_chargers")
        if not isinstance(saved, list):
            return None
        out: list[dict] = []
        seen: set[str] = set()
        for c in saved:
            if not isinstance(c, dict):
                continue
            key = c.get("device_id")
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(self._entry_for(str(key), c))
            if len(out) >= _MAX_LOADS:
                break
        return out or None

    def _state(self, roles: dict, role: str):
        eid = roles.get(role)
        return self._hass.states.get(eid) if isinstance(eid, str) and eid else None

    def _entry_for(self, key: str, charger: dict) -> dict:
        roles = charger.get("roles") if isinstance(charger.get("roles"), dict) else {}
        status = self._state(roles, "status")
        options = status.attributes.get("options") if status is not None else None
        entry: dict[str, Any] = {
            "key": key, "kind": "ev_charger", "label": charger.get("label"),
            "control_class": "regulated",
            "status_raw": status.state if status is not None else None,
            "status_options": [str(o) for o in options] if isinstance(options, (list, tuple)) else None,
        }
        power = self._scaled(self._state(roles, "power"), _POWER_TO_W)
        if power is not None:
            entry["power_w"] = max(0.0, round(power, 3))
        energy = self._scaled(self._state(roles, "energy"), _ENERGY_TO_KWH)
        entry["energy_kwh"] = round(energy, 6) if energy is not None else None
        entry["setpoint"] = self._setpoint(self._state(roles, "setpoint"))
        entry["source"] = {"executor": "ha", "device_ref": roles.get("setpoint") or key}
        return entry

    @staticmethod
    def _scaled(state, factors: dict[str, float]) -> float | None:
        if state is None or state.state in _UNAVAILABLE:
            return None
        unit = _unit(state)
        value = _num(state.state)
        if unit not in factors or value is None:
            return None
        return value * factors[unit]

    @staticmethod
    def _setpoint(state) -> dict | None:
        if state is None:
            return None
        spec = _SETPOINT_UNITS.get(_unit(state))
        if spec is None:
            return None
        unit, factor = spec

        def scaled(v):
            n = _num(v)
            return round(n * factor, 3) if n is not None else None
        return {"unit": unit, "value": scaled(state.state),
                "min": scaled(state.attributes.get("min")), "max": scaled(state.attributes.get("max")),
                "step": scaled(state.attributes.get("step"))}
