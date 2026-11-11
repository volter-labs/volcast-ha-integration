"""Tryb bazowy po utracie prawa do sterowania.

Tryb z profilu (`baseline.mode`); ogranicznik eksportu oraz progi SoC (dolny i górny)
z migawki zrobionej przed naszym pierwszym zapisem — nigdy „jak ostatnio zapisaliśmy"
(inaczej zakaz eksportu 0 W albo sufit ładowania z planu zostałby na zawsze).
Przywracamy tylko to, co sami przejęliśmy.

Profil z mocą wg kierunku i przełącznikiem zdalnego sterowania: migawka obejmuje też obie nastawy
mocy i stan przełącznika (klucze istnieją tylko w takich profilach — odczyt i mapa encji innych
profili ich nie mają, więc migawka GoodWe i reszty jest taka jak dotąd). Przełącznik po oddaniu:
`baseline.enable` z profilu, bez niego — stan z migawki.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Iterable, Mapping

from ..params import Params

SNAPSHOT_KEYS = ("mode", "soc_min", "soc_max", "export_limit_w", "export_limit_enabled",
                 "charge_power_w", "discharge_power_w", "enable")
# Nastawy mocy wg kierunku (`write_policy.power_by_direction`).
SET_POINT_KEYS = ("charge_power_w", "discharge_power_w")


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
    write = profile.raw.get("write") or {}
    switch = None
    if "enable" in write:
        switch = base.get("enable")
        snap_switch = _f(snapshot.get("enable"))
        if switch is None and snap_switch is not None:
            switch = snap_switch >= 0.5
    set_points = {k: _f(snapshot.get(k)) for k in SET_POINT_KEYS if k in write}
    return Params(mode=mode, soc_min=_f(snapshot.get("soc_min")), soc_max=_f(snapshot.get("soc_max")),
                  export_limit_w=_f(snapshot.get("export_limit_w")), export_limit_enabled=enabled,
                  enable=switch, **set_points)


def needs_restore(*, owned: bool, consent: bool | None, local_switch: bool,
                  control_mode: str | None, active_mode: str = "entities") -> bool:
    """Powrót do trybu bazowego: tylko przy własności i utracie prawa.

    `active_mode` — sposób sterowania, w którym przejęliśmy falownik (`entities`/`direct`);
    zmiana sposobu przy własności = powrót przez stary sposób.
    """
    if not owned:
        return False
    return consent is False or not local_switch or control_mode != active_mode


def plan_uncovered_since(schedule, now_utc: datetime, started_utc: datetime) -> datetime | None:
    """Od kiedy żaden slot planu nie pokrywa „teraz”; None — slot pokrywa.

    Koniec ostatniego slotu, który już minął (zapisany plan przeżywa restart, więc i ten czas); bez
    takiego slotu (brak planu, plan dopiero w przyszłości) — start wykonawcy."""
    slots = tuple(getattr(schedule, "slots", None) or ())
    if any(s.covers(now_utc) for s in slots):
        return None
    ended = [s.end for s in slots if s.end <= now_utc]
    return max(ended) if ended else started_utc


def release_without_plan(profile, schedule, now_utc: datetime, started_utc: datetime) -> bool:
    """`write_policy.release_without_plan_s`: brak slotu pokrywającego „teraz” dłużej niż próg = oddanie.
    Profil bez pola — nigdy (zapasowy slot planu steruje jak dotąd)."""
    limit = getattr(profile, "release_without_plan_s", None)
    if limit is None:
        return False
    since = plan_uncovered_since(schedule, now_utc, started_utc)
    return since is not None and (now_utc - since).total_seconds() > limit
