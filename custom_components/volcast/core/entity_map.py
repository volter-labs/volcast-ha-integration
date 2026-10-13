"""Profil → encje HA: rozpoznanie po `platform` + `unique_id` i przekład parametrów
na wywołania usług. Czyste dane — samo wywołanie usług robi warstwa HA.

Encje rozpoznajemy po `unique_id` (stabilne przy zmianie nazw przez użytkownika),
nigdy po `entity_id`. Encja pasująca do regexu więcej niż raz NIE jest mapowana —
zgadywanie adresata zapisu do falownika jest gorsze niż jego brak.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from .params import Params
from .registers import ordered_keys

_UNAVAILABLE = ("unavailable", "unknown", "")
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")

# Jednostki, w których HA może podać stan encji → przelicznik na jednostkę kanoniczną
# klucza (W, kWh, V, A, %). HA potrafi przeliczyć encję na system jednostek
# użytkownika (np. temperatura w °F), więc odczyt musi to odwrócić — inaczej strażnik
# temperatury baterii widzi 82,4 „°C" i trwale blokuje sterowanie.
_LINEAR_UNITS: dict[str, dict[str, float]] = {
    "power": {"W": 1.0, "kW": 1000.0, "MW": 1_000_000.0},
    "energy": {"kWh": 1.0, "Wh": 0.001, "MWh": 1000.0},
    "voltage": {"V": 1.0, "mV": 0.001},
    "current": {"A": 1.0, "mA": 0.001},
    "percent": {"%": 1.0},
}
_TEMPERATURE = {
    "°C": lambda v: v,
    "°F": lambda v: (v - 32.0) * 5.0 / 9.0,
    "K": lambda v: v - 273.15,
}


@dataclass(frozen=True)
class EntityCandidate:
    entity_id: str
    platform: str
    unique_id: str
    unit: str | None = None   # `unit_of_measurement` z rejestru/stanu; None = nieznana


@dataclass(frozen=True)
class ResolveResult:
    mapped: dict[str, str]
    missing: list[str]
    ambiguous: dict[str, list[str]]
    # pasują po unique_id, ale mają jednostkę, której klucz nie przyjmuje — NIE mapowane
    incompatible: dict[str, list[str]] = field(default_factory=dict)
    units: dict[str, str | None] = field(default_factory=dict)   # jednostka zmapowanych encji


@dataclass(frozen=True)
class EntityWrite:
    key: str
    entity_id: str
    domain: str
    service: str
    data: dict


def _integration(profile, domain: str) -> dict:
    for integ in profile.raw["ha"]["integrations"]:
        if integ["domain"] == domain:
            return integ
    raise KeyError(f"profil {profile.id} nie opisuje integracji {domain!r}")


def resolve_entities(profile, integration_domain: str,
                     candidates: Iterable[EntityCandidate]) -> ResolveResult:
    ents = _integration(profile, integration_domain)["entities"]
    pool = [c for c in candidates if c.platform == integration_domain]
    mapped: dict[str, str] = {}
    missing: list[str] = []
    ambiguous: dict[str, list[str]] = {}
    incompatible: dict[str, list[str]] = {}
    units: dict[str, str | None] = {}
    for key, spec in ents.items():
        rx = re.compile(spec["unique_id_regex"])
        hits = [c for c in pool
                if c.entity_id.split(".", 1)[0] == spec["domain"] and rx.search(c.unique_id)]
        ok = sorted((c for c in hits if _unit_factor(key, c.unit) is not None),
                    key=lambda c: c.entity_id)
        if len(ok) == 1:
            mapped[key] = ok[0].entity_id
            units[key] = ok[0].unit
        elif len(ok) > 1:
            ambiguous[key] = [c.entity_id for c in ok]
        elif hits:
            incompatible[key] = sorted(c.entity_id for c in hits)
        else:
            missing.append(key)
    return ResolveResult(mapped, missing, ambiguous, incompatible, units)


def resolve_work_mode_entity(profile, integration_domain: str,
                             candidates: Iterable[EntityCandidate]) -> str | None:
    """Encja select trybu pracy (blok `work_mode` profilu) po `ha.unique_id_regex`; brak bloku, brak
    dopasowania albo niejednoznaczność -> None (stan zostaje `unknown`)."""
    wm = getattr(profile, "work_mode", None)
    if wm is None:
        return None
    rx = re.compile(wm.ha_unique_id_regex)
    hits = sorted(c.entity_id for c in candidates
                  if c.platform == integration_domain and c.entity_id.split(".", 1)[0] == "select"
                  and rx.search(c.unique_id))
    return hits[0] if len(hits) == 1 else None


def entity_writes(params: Params, profile, integration_domain: str, mapped: Mapping[str, str],
                  keys: Iterable[str] | None = None,
                  units: Mapping[str, str | None] | None = None,
                  ) -> tuple[list[EntityWrite], list[str]]:
    """Zapisy (w kolejności profilu) i klucze, dla których nie ma zmapowanej encji.

    `units` (z `ResolveResult.units`) przelicza wartość z jednostki kanonicznej klucza
    na jednostkę encji (np. W → kW). Encja w jednostce, której nie da się dokładnie
    przeliczyć (np. limit eksportu w % zamiast W), ląduje w kluczach bez encji.
    """
    ents = _integration(profile, integration_domain)["entities"]
    wanted = None if keys is None else set(keys)
    writes: list[EntityWrite] = []
    unmapped: list[str] = []
    for key in ordered_keys(params, profile):
        if wanted is not None and key not in wanted:
            continue
        ha_key = key.replace(".", "_") if key.startswith("tou.") else key
        if ha_key not in mapped or ha_key not in ents:
            unmapped.append(key)
            continue
        spec, eid = ents[ha_key], mapped[ha_key]
        domain = spec["domain"]
        if key.startswith("tou."):
            _, idx, field = key.split(".")
            prog = params.tou[int(idx) - 1]
            raw = {"start": prog.start_min, "power_w": prog.power_w, "soc": prog.soc,
                   "grid_charge": prog.grid_charge}[field]
        else:
            raw = getattr(params, key)
        if domain == "select":
            if raw not in profile.modes:
                unmapped.append(key)   # tryb spoza profilu: nie zgadujemy opcji
                continue
            writes.append(EntityWrite(key, eid, domain, "select_option",
                                      {"option": profile.modes[raw].ha_option}))
        elif domain == "switch":
            writes.append(EntityWrite(key, eid, domain, "turn_on" if raw else "turn_off", {}))
        elif domain == "time":
            minutes = int(raw)
            writes.append(EntityWrite(key, eid, domain, "set_value",
                                      {"time": f"{minutes // 60:02d}:{minutes % 60:02d}:00"}))
        else:
            factor = _unit_factor(ha_key, (units or {}).get(ha_key))
            if factor is None:
                unmapped.append(key)
                continue
            value = float(raw)
            if spec.get("transform") == "invert_percent":
                # Encja GoodWe to GŁĘBOKOŚĆ rozładowania: próg 20 % = DoD 80 %.
                value = round(100.0 - value, 1)
            if factor != 1.0:
                value = value / factor
            writes.append(EntityWrite(key, eid, domain, "set_value", {"value": value}))
    return writes, unmapped


def _quantity(key: str) -> str | None:
    """Wielkość fizyczna klucza z jego nazwy (sufiks = jednostka kanoniczna)."""
    if key.endswith("_temp_c"):
        return "temperature"
    if key.endswith("_kwh"):
        return "energy"
    if key.endswith("_w"):
        return "power"
    if key.endswith("_v"):
        return "voltage"
    if key.endswith("_a"):
        return "current"
    if key in ("soc", "soc_min", "soc_max") or key.endswith("_soc"):
        return "percent"
    return None


def _unit_factor(key: str, unit: str | None) -> float | None:
    """Mnożnik jednostka encji → jednostka kanoniczna klucza; None = niezgodna.

    Brak jednostki (None/"") = kanoniczna. Klucz bez wielkości fizycznej (tryb,
    przełącznik, czas) z podaną jednostką jest niezgodny. Temperatura nie jest
    liniowa — dla niej 1.0 oznacza tylko „znana jednostka" (przelicza `_to_canonical`).
    """
    if not unit:
        return 1.0
    quantity = _quantity(key)
    if quantity is None:
        return None
    if quantity == "temperature":
        return 1.0 if unit in _TEMPERATURE else None
    return _LINEAR_UNITS[quantity].get(unit)


def _to_canonical(key: str, value: float, unit: str | None) -> float | None:
    """Przelicza na jednostkę kanoniczną klucza; nieznana/niezgodna jednostka → None.

    Brak jednostki (None/"") = zakładamy jednostkę kanoniczną — tak raportuje
    większość integracji. Jednostka podana, ale obca (np. „W" dla SoC) to błąd
    mapowania: lepiej nie mieć odczytu niż podać strażnikowi złą liczbę.
    """
    factor = _unit_factor(key, unit)
    if factor is None:
        return None
    if unit and _quantity(key) == "temperature":
        out = _TEMPERATURE[unit](value)
    else:
        out = value * factor
    # Skończoność PO przeliczeniu: 1e303 MW to już nieskończoność W.
    return out if math.isfinite(out) else None


def canonical_value(key: str, state: str | None, unit: str | None) -> float | None:
    """Stan encji → liczba w jednostce kanonicznej klucza (sufiks nazwy), bez profilu.

    Ta sama zasada co `entity_value`: nieczytelne, nieskończone albo w niezgodnej
    jednostce → None (brak odczytu, nigdy zero).
    """
    if state is None or state in _UNAVAILABLE:
        return None
    try:
        value = float(state)
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    return _to_canonical(key, value, unit)


def _time_to_minutes(state: str) -> float | None:
    m = _TIME_RE.match(state)
    if m is None:
        return None
    h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    if h > 23 or mi > 59 or s > 59:
        return None
    return float(h * 60 + mi)


def entity_value(key: str, state: str | None, profile, integration_domain: str,
                 *, unit: str | None = None) -> float | str | None:
    """Stan encji HA → wartość w konwencji profilu (jak `Params.flatten`/odczyt rejestrów).

    `unit` to `unit_of_measurement` encji; przeliczamy na jednostkę kanoniczną klucza
    PRZED transformacją profilu. Przełącznik: 1.0/0.0, czas: minuta doby, wybór: nazwa
    trybu z profilu. Cokolwiek nieczytelnego → None (brak odczytu, nie zero).
    """
    spec = _integration(profile, integration_domain)["entities"][key]
    if state is None or state in _UNAVAILABLE:
        return None
    domain = spec["domain"]
    if domain == "select":
        for m in profile.modes.values():
            if m.ha_option == state:
                return m.name
        return None   # opcja spoza profilu = obce przejęcie albo nieznany tryb
    if domain == "switch":
        return {"on": 1.0, "off": 0.0}.get(state)
    if domain == "time":
        return _time_to_minutes(state)
    try:
        value = float(state)
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    value = _to_canonical(key, value, unit)
    if value is None:
        return None
    if spec.get("transform") == "negate":
        return -value
    if spec.get("transform") == "invert_percent":
        return round(100.0 - value, 1)
    return value


# ── Opisy kluczy dla ręcznej mapy encji (czyste dane, bez HA) ──────────────


def _d(purpose: str, unit: str, sign: str, required: bool, examples: list[str]) -> dict:
    return {"purpose": purpose, "unit": unit, "sign": sign, "required": required, "examples": examples}


_NONE = "no sign"

# `purpose`: „plan" = dane wejściowe planu baterii, „control" = sterowanie falownikiem i jego straże.
# `sign`: konwencja znaku po przeliczeniu na jednostkę kanoniczną (Volcast liczy w tej konwencji).
# `examples`: typowe wzorce nazw encji (podpowiedź ogólna, nie lista wspieranych marek).
KEY_DESCRIPTORS: dict[str, dict] = {
    "soc": _d("plan", "%", "0-100, no sign", True,
              ["sensor.<inverter>_battery_state_of_charge", "sensor.<battery>_soc"]),
    "pv_power_w": _d("plan", "W", "positive = PV production", True,
                     ["sensor.<inverter>_pv_power", "sensor.<inverter>_solar_power"]),
    "load_power_w": _d("plan", "W", "positive = house consumption", True,
                       ["sensor.<inverter>_house_consumption", "sensor.<inverter>_load"]),
    "grid_power_w": _d("plan", "W", "positive = import from the grid, negative = export", True,
                       ["sensor.<inverter>_grid_power", "sensor.<meter>_active_power"]),
    "battery_power_w": _d("plan", "W", "positive = discharging, negative = charging", True,
                          ["sensor.<inverter>_battery_power", "sensor.<battery>_power"]),
    "battery_temp_c": _d("control", "°C", "can be negative", False,
                         ["sensor.<inverter>_battery_temperature", "sensor.<battery>_temp"]),
    "battery_voltage_v": _d("control", "V", "no sign", False,
                            ["sensor.<inverter>_battery_voltage", "sensor.<battery>_voltage"]),
    "battery_current_a": _d("control", "A", "positive = discharging, negative = charging", False,
                            ["sensor.<inverter>_battery_current", "sensor.<battery>_current"]),
    "active_power_w": _d("control", "W", "positive = inverter output", False,
                         ["sensor.<inverter>_active_power", "sensor.<inverter>_ac_power"]),
    "pv_energy_total_kwh": _d("plan", "kWh", "lifetime total, only grows", False,
                              ["sensor.<inverter>_total_pv_generation", "sensor.<inverter>_pv_energy_total"]),
    "grid_export_total_kwh": _d("plan", "kWh", "lifetime total, only grows", False,
                                ["sensor.<inverter>_total_energy_export", "sensor.<meter>_export_energy"]),
    "grid_import_total_kwh": _d("plan", "kWh", "lifetime total, only grows", False,
                                ["sensor.<inverter>_total_energy_import", "sensor.<meter>_import_energy"]),
    "load_energy_total_kwh": _d("plan", "kWh", "lifetime total, only grows", False,
                                ["sensor.<inverter>_total_load_energy", "sensor.<inverter>_house_consumption_total"]),
    "load_energy_day_kwh": _d("plan", "kWh", "resets at midnight", False,
                              ["sensor.<inverter>_today_load", "sensor.<inverter>_daily_house_consumption"]),
    "battery_charge_total_kwh": _d("plan", "kWh", "lifetime total, only grows", False,
                                   ["sensor.<inverter>_total_battery_charge", "sensor.<battery>_charge_energy_total"]),
    "battery_discharge_total_kwh": _d("plan", "kWh", "lifetime total, only grows", False,
                                      ["sensor.<inverter>_total_battery_discharge",
                                       "sensor.<battery>_discharge_energy_total"]),
    "bms_charge_current_max_a": _d("control", "A", "no sign", False,
                                   ["sensor.<battery>_max_charge_current", "number.<inverter>_bms_charge_limit"]),
    "bms_discharge_current_max_a": _d("control", "A", "no sign", False,
                                      ["sensor.<battery>_max_discharge_current",
                                       "number.<inverter>_bms_discharge_limit"]),
    "battery_capacity_kwh": _d("plan", "kWh", "no sign", False,
                               ["sensor.<battery>_capacity", "sensor.<inverter>_battery_capacity"]),
    # Klucze zapisu / sterowania.
    "mode": _d("control", "option", _NONE, False,
               ["select.<inverter>_ems_mode", "select.<inverter>_work_mode"]),
    "mode_value": _d("control", "option", _NONE, False,
                     ["sensor.<inverter>_ems_mode", "sensor.<inverter>_work_mode"]),
    "power_w": _d("control", "W", "magnitude of the charge/discharge limit, no sign", False,
                  ["number.<inverter>_ems_power_limit", "number.<inverter>_battery_power_setpoint"]),
    "soc_min": _d("control", "%", "0-100, no sign", False,
                  ["number.<inverter>_battery_min_soc", "number.<inverter>_depth_of_discharge"]),
    "soc_max": _d("control", "%", "0-100, no sign", False,
                  ["number.<inverter>_battery_max_soc", "number.<inverter>_charge_cutoff_soc"]),
    "export_limit_enabled": _d("control", "on/off", _NONE, False,
                               ["switch.<inverter>_export_limit", "switch.<inverter>_grid_export_limit"]),
    "export_limit_w": _d("control", "W", "magnitude of the grid export limit, no sign", False,
                         ["number.<inverter>_export_limit", "number.<inverter>_grid_export_limit"]),
}


_WRITE_KEYS = ("mode", "power_w", "soc_min", "soc_max", "export_limit_w", "export_limit_enabled")


def field_group(key: str) -> str:
    """Grupa pola w formularzu: `plan` (wymagane do planu), `control` (nastawy falownika), `optional`."""
    desc = KEY_DESCRIPTORS.get(key)
    if desc is not None and desc["required"]:
        return "plan"
    return "control" if key in _WRITE_KEYS else "optional"


_GROUP_LABELS = {"plan": "Needed for the plan.", "control": "Needed for control.", "optional": "Optional."}


def describe_key(key: str) -> str:
    """Tekst `data_description` pola mapy (angielski, źródło dla strings.json — test pilnuje zgodności)."""
    d = KEY_DESCRIPTORS[key]
    return (f"{_GROUP_LABELS[field_group(key)]} Unit: {d['unit']}. Sign: {d['sign']}. "
            f"Typical names: {', '.join(d['examples'])}.")


def preview_entity(key: str, state: str | None, unit: str | None) -> tuple[str, float | None]:
    """Podgląd odczytu jednej encji przed zapisem mapy: (status, wartość kanoniczna).

    Statusy: `ok` (wartość albo None dla kluczy nienumerycznych), `unavailable` (brak/`unknown`),
    `unit_mismatch` (jednostka encji niezgodna z kluczem), `unreadable` (nie liczba).
    Znak nie jest sprawdzany — z pojedynczej wartości nie da się go wiarygodnie ocenić.
    """
    if state is None or state in _UNAVAILABLE:
        return "unavailable", None
    if _quantity(key) is None:
        return "ok", None
    if _unit_factor(key, unit) is None:
        return "unit_mismatch", None
    try:
        float(state)
    except ValueError:
        return "unreadable", None
    value = canonical_value(key, state, unit)
    return ("ok", value) if value is not None else ("unreadable", None)
