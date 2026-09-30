"""Możliwości wykonawcy w trybie encji = deklaracja profilu ∩ klucze z encją.

Sterowanie wymaga encji trybu (`REQUIRED_WRITE_KEYS`). Pozostała nastawa bez encji
(brak mapowania, encja wyłączona albo niedostępna) jest nieobsługiwana: wypada jej
możliwość — chmura nie planuje z nią — i intencje, które jej potrzebują (moc), a reszta
sterowania działa dalej. Plan, który mimo to niesie taką nastawę, dostaje ją odrzuconą
w cyklu (moc — cykl wstrzymany, bo tryb na starej mocy to inna komenda).
"""
from __future__ import annotations

from typing import Iterable

from ..profile_schema import SLOT_POWER_KINDS

_INTENT_CAPS = {"force_charge_from_grid": "charge_grid", "sell_from_battery": "sell",
                "force_discharge": "discharge_forced", "standby": "standby"}
_KEY_CAPS = {"set_power_w": ("power_w",), "limit_export": ("export_limit_w", "export_limit_enabled"),
             "set_soc_floor": ("soc_min",), "set_soc_ceiling": ("soc_max",)}


REQUIRED_WRITE_KEYS = ("mode",)


def missing_write_keys(profile, mapped_keys: Iterable[str]) -> tuple[str, ...]:
    """Klucze zapisu profilu bez encji, w kolejności profilu."""
    mapped = set(mapped_keys)
    order = (profile.raw.get("write_policy") or {}).get("order") or ()
    return tuple(k for k in order if k not in mapped)


def entity_mode_ready(choice, mapped_keys: Iterable[str]) -> bool:
    """Czy tryb encji jest dostępny: profil z integracją HA i encja trybu.

    Jedna reguła dla opcji integracji i dla wyboru zdalnego w onboardingu.
    """
    if choice is None or not getattr(choice, "integration_domain", None):
        return False
    return not required_missing(choice.profile, mapped_keys or ())


def required_missing(profile, mapped_keys: Iterable[str]) -> tuple[str, ...]:
    """Klucze wymagane do sterowania (tryb), które nie mają encji."""
    missing = missing_write_keys(profile, mapped_keys)
    return tuple(k for k in missing if k in REQUIRED_WRITE_KEYS)


def entity_mode_options(choice) -> dict[str, str]:
    """Opcje trybu encji — te same trzy klucze z opcji integracji i z wyboru zdalnego.

    Jedna postać zapisu: inaczej ponowny wybór tego samego w opcjach wyglądałby na zmianę
    sterowania (profil/integracja z pustego na wartość) i oddawał falownik bez potrzeby.
    """
    return {"control_mode": "entities", "profile_id": choice.profile.id,
            "inverter_domain": choice.integration_domain}


def capabilities_for(profile, mapped_keys: Iterable[str]) -> dict[str, bool]:
    mapped = set(mapped_keys)
    complete = not required_missing(profile, mapped)
    declared = profile.raw.get("capabilities") or {}
    intents = profile.raw.get("intents") or {}
    out: dict[str, bool] = {}
    for cap, intent in _INTENT_CAPS.items():
        spec = intents.get(intent)
        need = {"mode"}
        if isinstance(spec, dict) and spec.get("power") in (*SLOT_POWER_KINDS, "zero"):
            need.add("power_w")          # standby pisze jawne 0 W — bez encji mocy nie ustoi
        out[cap] = complete and bool(declared.get(cap)) and isinstance(spec, dict) and need <= mapped
    for cap, keys in _KEY_CAPS.items():
        out[cap] = complete and bool(declared.get(cap)) and set(keys) <= mapped
    return out
