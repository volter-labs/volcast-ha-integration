"""Tryb bazowy po utracie prawa do sterowania.

Tryb z profilu (`baseline.mode`); ogranicznik eksportu oraz progi SoC (dolny i górny)
z migawki zrobionej przed naszym pierwszym zapisem — nigdy „jak ostatnio zapisaliśmy"
(inaczej zakaz eksportu 0 W albo sufit ładowania z planu zostałby na zawsze).
Przywracamy tylko to, co sami przejęliśmy.
"""
from __future__ import annotations

import math
from typing import Iterable, Mapping

from ..params import Params

SNAPSHOT_KEYS = ("mode", "soc_min", "soc_max", "export_limit_w", "export_limit_enabled")


def take_snapshot(readings: Mapping[str, float | str]) -> dict[str, float | str]:
    return {k: readings[k] for k in SNAPSHOT_KEYS if k in readings}


def snapshot_missing(snapshot: Mapping[str, float | str], mapped_keys: Iterable[str]) -> tuple[str, ...]:
    """Klucze migawki z encją, których migawka nie ma — w kolejności `SNAPSHOT_KEYS`.

    Tryb pomijamy: tryb bazowy pochodzi z profilu. Niepełna migawka = klucz, którego
    nigdy nie przywrócimy, więc przed pierwszym zapisem musi być pusta.
    """
    mapped = set(mapped_keys)
    return tuple(k for k in SNAPSHOT_KEYS if k != "mode" and k in mapped and k not in snapshot)


def _f(v) -> float | None:
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return None
    return float(v) if math.isfinite(v) else None


def baseline_params(profile, snapshot: Mapping[str, float | str]) -> Params:
    base = profile.raw.get("baseline") or {}
    mode = base.get("mode")
    if mode is None:
        return Params()          # model okien czasowych — w tej wersji nie zapisujemy programów
    enabled = base.get("export_limit_enabled")
    snap_enabled = _f(snapshot.get("export_limit_enabled"))
    if enabled is None and snap_enabled is not None:
        enabled = snap_enabled >= 0.5
    return Params(mode=mode, soc_min=_f(snapshot.get("soc_min")), soc_max=_f(snapshot.get("soc_max")),
                  export_limit_w=_f(snapshot.get("export_limit_w")), export_limit_enabled=enabled)


def needs_restore(*, owned: bool, consent: bool | None, local_switch: bool,
                  control_mode: str | None, active_mode: str = "entities") -> bool:
    """Powrót do trybu bazowego: tylko przy własności i utracie prawa.

    `active_mode` — sposób sterowania, w którym przejęliśmy falownik (`entities`/`direct`);
    zmiana sposobu przy własności = powrót przez stary sposób.
    """
    if not owned:
        return False
    return consent is False or not local_switch or control_mode != active_mode
