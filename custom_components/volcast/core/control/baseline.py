"""Tryb bazowy po utracie prawa do sterowania.

Tryb z profilu (`baseline.mode`); ogranicznik eksportu oraz progi SoC (dolny i górny)
z migawki zrobionej przed naszym pierwszym zapisem — nigdy „jak ostatnio zapisaliśmy"
(inaczej zakaz eksportu 0 W albo sufit ładowania z planu zostałby na zawsze).
Przywracamy tylko to, co sami przejęliśmy.
"""
from __future__ import annotations

from typing import Mapping

from ..params import Params

SNAPSHOT_KEYS = ("mode", "soc_min", "soc_max", "export_limit_w", "export_limit_enabled")


def take_snapshot(readings: Mapping[str, float | str]) -> dict[str, float | str]:
    return {k: readings[k] for k in SNAPSHOT_KEYS if k in readings}


def _f(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


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
                  control_mode: str | None) -> bool:
    if not owned:
        return False
    return consent is False or not local_switch or control_mode != "entities"
