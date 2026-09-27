"""Profil marki: ładowanie, walidacja i wygodny dostęp.

UWAGA dla warstwy HA: `load_profile` czyta plik synchronicznie — w HA
wołać przez `hass.async_add_executor_job`.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .profile_schema import validate_profile

PROFILES_DIR = Path(__file__).resolve().parent.parent / "profiles"
_BUILTIN_ID_RE = re.compile(r"[a-z0-9-]+")


class ProfileError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class ModeDef:
    name: str
    value: int
    direction: str
    ha_option: str


@dataclass(frozen=True)
class ModbusSpec:
    """Dostęp bezpośredni do rejestrów: status ścieżki, funkcja zapisu, parametry łączy."""
    status: str                                         # "draft" | "verified"
    write_function: int                                 # 6 | 16
    max_read_registers: int                             # 1..125
    transport_options: Mapping[str, Mapping[str, int]]  # nazwa → {port, timeout_ms, gap_ms}
    identify_reads: tuple[tuple[int, int], ...]         # (adres, liczba)
    probe_keys: tuple[str, ...]
    echo_only: tuple[str, ...] = ()                     # klucze potwierdzane samym echem (bez odczytu)


@dataclass(frozen=True)
class NvmBudget:
    """Budżet zapisów do pamięci nieulotnej w oknie kroczącym."""
    window_s: float
    per_key: int
    total: int


@dataclass(frozen=True)
class Profile:
    raw: Mapping[str, Any]
    id: str
    status: str
    control_model: str
    unit_id: int
    modes: Mapping[str, ModeDef]
    neutral_mode: str | None
    write_order: tuple[str, ...]
    min_interval_s: float
    max_direction_changes_per_hour: int
    max_state_age_s: float
    temp_min_c: float
    temp_max_c: float
    tou_programs: int
    time_step_min: int
    soc_tolerance_pp: float
    power_tolerance_w: float
    tou_field_order: tuple[str, ...]
    modbus: ModbusSpec
    nvm_budget: NvmBudget | None

    def intent(self, name: str) -> Mapping[str, Any] | None:
        return self.raw["intents"][name]

    def mode_value(self, name: str) -> int:
        return self.modes[name].value

    def mode_by_value(self, value: int) -> ModeDef | None:
        for m in self.modes.values():
            if m.value == value:
                return m
        return None

    def mode_direction(self, name: str) -> str:
        return self.modes[name].direction


def profile_from_dict(raw: dict) -> Profile:
    errors = validate_profile(raw)
    if errors:
        raise ProfileError(errors)
    wp, lim, tou = raw["write_policy"], raw["limits"], raw.get("tou") or {}
    modes = {n: ModeDef(n, m["value"], m["direction"], m["ha_option"])
             for n, m in (raw.get("modes") or {}).items()}
    return Profile(
        raw=MappingProxyType(json.loads(json.dumps(raw))),   # własna, głęboka kopia
        id=raw["id"], status=raw["status"], control_model=raw["control_model"],
        unit_id=raw["unit_id"], modes=MappingProxyType(modes),
        neutral_mode=raw.get("neutral_mode"),
        write_order=tuple(wp["order"]), min_interval_s=float(wp["min_interval_s"]),
        max_direction_changes_per_hour=wp["max_direction_changes_per_hour"],
        max_state_age_s=float(wp["max_state_age_s"]),
        temp_min_c=float(lim["battery_temp_c"]["min"]), temp_max_c=float(lim["battery_temp_c"]["max"]),
        tou_programs=int(tou.get("programs", 0)), time_step_min=int(tou.get("time_step_min", 60)),
        soc_tolerance_pp=float(tou.get("soc_tolerance_pp", 0)),
        power_tolerance_w=float(tou.get("power_tolerance_w", 0)),
        tou_field_order=tuple(tou.get("field_order", ())),
        modbus=_modbus_spec(raw["modbus"]),
        nvm_budget=_nvm_budget(wp.get("nvm_budget")),
    )


def _modbus_spec(m: Mapping[str, Any]) -> ModbusSpec:
    return ModbusSpec(
        status=m["status"], write_function=m["write_function"],
        max_read_registers=m["max_read_registers"],
        transport_options=MappingProxyType({name: MappingProxyType(dict(o))
                                            for name, o in m["transport_options"].items()}),
        identify_reads=tuple((r["addr"], r["count"]) for r in m["identify_reads"]),
        probe_keys=tuple(m["probe_keys"]),
        echo_only=tuple(m.get("echo_only", ())),
    )


def _nvm_budget(b: Mapping[str, Any] | None) -> NvmBudget | None:
    if b is None:
        return None
    return NvmBudget(window_s=float(b["window_h"]) * 3600.0, per_key=b["per_key"], total=b["total"])


def direct_verified(profile: Profile) -> bool:
    """Ścieżka rejestrów dopuszczona do zapisu: i profil, i jego sekcja `modbus` zweryfikowane."""
    return profile.status == "verified" and profile.modbus.status == "verified"


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict:
    # Domyślnie `json` bierze ostatnie wystąpienie klucza — w profilu marki to cicha
    # podmiana nastawy, więc powtórzony klucz jest błędem pliku.
    out: dict = {}
    for k, val in pairs:
        if k in out:
            raise ValueError(f"powtórzony klucz {k!r}")
        out[k] = val
    return out


def load_profile(path: Path) -> Profile:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_keys)
    except (OSError, ValueError) as err:
        raise ProfileError([f"{path}: nie da się wczytać ({err})"]) from err
    profile = profile_from_dict(raw)
    if Path(path).stem != profile.id:
        raise ProfileError([f"{path}: nazwa pliku musi być równa id {profile.id!r}"])
    return profile


def builtin_ids() -> tuple[str, ...]:
    return tuple(sorted(p.stem for p in PROFILES_DIR.glob("*.json")))


def load_builtin(profile_id: str) -> Profile:
    # Id trafia do ścieżki pliku — bez tej kontroli "../x" wyszłoby poza katalog profili.
    if not isinstance(profile_id, str) or not _BUILTIN_ID_RE.fullmatch(profile_id):
        raise ProfileError([f"$.id: niedozwolony identyfikator profilu {profile_id!r}"])
    return load_profile(PROFILES_DIR / f"{profile_id}.json")
