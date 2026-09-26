"""Możliwości wykonawcy w trybie encji = deklaracja profilu ∩ klucze z encją.

Sterowanie wymaga encji dla KAŻDEGO klucza zapisu profilu (`write_policy.order`).
Tryb zapisany bez swoich parametrów warunkujących (blokada eksportu, sufit
ładowania) wykonałby polecenie, którego nikt nie wydał — dlatego przy choćby
jednym brakującym kluczu wszystkie możliwości są fałszywe.
"""
from __future__ import annotations

from typing import Iterable

_INTENT_CAPS = {"force_charge_from_grid": "charge_grid", "sell_from_battery": "sell",
                "force_discharge": "discharge_forced", "standby": "standby"}
_KEY_CAPS = {"set_power_w": ("power_w",), "limit_export": ("export_limit_w", "export_limit_enabled"),
             "set_soc_floor": ("soc_min",), "set_soc_ceiling": ("soc_max",)}


def missing_write_keys(profile, mapped_keys: Iterable[str]) -> tuple[str, ...]:
    """Klucze zapisu profilu bez encji, w kolejności profilu."""
    mapped = set(mapped_keys)
    order = (profile.raw.get("write_policy") or {}).get("order") or ()
    return tuple(k for k in order if k not in mapped)


def capabilities_for(profile, mapped_keys: Iterable[str]) -> dict[str, bool]:
    mapped = set(mapped_keys)
    complete = not missing_write_keys(profile, mapped)
    declared = profile.raw.get("capabilities") or {}
    intents = profile.raw.get("intents") or {}
    out: dict[str, bool] = {}
    for cap, intent in _INTENT_CAPS.items():
        spec = intents.get(intent)
        need = {"mode"}
        if isinstance(spec, dict) and spec.get("power") in ("slot", "zero"):
            need.add("power_w")          # standby pisze jawne 0 W — bez encji mocy nie ustoi
        out[cap] = complete and bool(declared.get(cap)) and isinstance(spec, dict) and need <= mapped
    for cap, keys in _KEY_CAPS.items():
        out[cap] = complete and bool(declared.get(cap)) and set(keys) <= mapped
    return out
