"""Sloty ładowarki (`loads[]`) i metadane planu (`loads_meta`) z koperty planu.

Parser jest LENIWY: plan ładowarki jest dodatkiem do planu baterii, więc brak,
zły kształt albo pojedynczy zły wpis oznacza „brak/pominięty wpis" — nigdy wyjątek
i nigdy unieważnienie planu baterii (inaczej niż fail-closed parser baterii).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

MODES = ("off", "pv_surplus", "forced")
GUARDS = ("bypass", "allow")
UNITS = ("A", "W")

# Czas ISO-8601 ze strefą (jak w `core/slot.py`; tu osobno, by uniknąć importu cyklicznego).
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")


@dataclass(frozen=True)
class LoadSlot:
    device_id: str
    start: datetime
    end: datetime
    mode: str                      # "off" | "pv_surplus" | "forced"
    power_w: float | None
    min_power_w: float | None
    session_id: str | None = None
    battery_guard: str | None = None   # "bypass" | "allow"


@dataclass(frozen=True)
class LoadsMeta:
    session_id: str
    load_key: str
    control_enabled: bool = False
    ready_by: datetime | None = None
    remaining_kwh: float | None = None
    max_power_w: float | None = None
    phases: int = 1
    control_unit: str | None = None    # "A" | "W"
    min: float | None = None
    max: float | None = None
    step: float | None = None
    plan_generated_at: datetime | None = None
    # Zgoda chmury na ładowanie awaryjne z sieci (False przy pauzie użytkownika i „tylko PV”).
    # Tylko jawne `true` otwiera: koperta z `loads_meta` bez tego pola pochodzi z serwera, który
    # już zna pauzy i „tylko PV”, ale nie umie ich przekazać — wtedy reguła awaryjna nie działa.
    fallback_allowed: bool = False


def _ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not _TS_RE.match(raw):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _number(raw: Any) -> float | None:
    # bool to podtyp int — nie przepuszczamy go jako liczby.
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    try:
        f = float(raw)
    except OverflowError:
        return None
    return f if math.isfinite(f) else None


def _text(raw: Any) -> str | None:
    return raw if isinstance(raw, str) and raw else None


_BAD = object()


def _opt_power(raw: Any) -> Any:
    """None → None; liczba ≥ 0 → float; reszta → _BAD (slot odrzucony)."""
    if raw is None:
        return None
    f = _number(raw)
    return _BAD if f is None or f < 0 else f


def _parse_slot(raw: Any) -> LoadSlot | None:
    if not isinstance(raw, dict):
        return None
    device_id = _text(raw.get("device_id"))
    start, end = _ts(raw.get("from")), _ts(raw.get("to"))
    mode = raw.get("mode")
    if device_id is None or start is None or end is None or end <= start or mode not in MODES:
        return None
    power, min_power = _opt_power(raw.get("power_w")), _opt_power(raw.get("min_power_w"))
    if power is _BAD or min_power is _BAD:
        return None
    guard = raw.get("battery_guard")
    return LoadSlot(device_id=device_id, start=start, end=end, mode=mode,
                    power_w=power, min_power_w=min_power,
                    session_id=_text(raw.get("session_id")),
                    battery_guard=guard if guard in GUARDS else None)


def _parse_meta(raw: Any) -> LoadsMeta | None:
    if not isinstance(raw, dict):
        return None
    session_id, load_key = _text(raw.get("session_id")), _text(raw.get("load_key"))
    if session_id is None or load_key is None:
        return None
    phases = _number(raw.get("phases"))
    unit = raw.get("control_unit")
    return LoadsMeta(
        session_id=session_id,
        load_key=load_key,
        control_enabled=raw.get("control_enabled") is True,
        ready_by=_ts(raw.get("ready_by")),
        remaining_kwh=_number(raw.get("remaining_kwh")),
        max_power_w=_number(raw.get("max_power_w")),
        phases=int(phases) if phases is not None and phases >= 1 else 1,
        control_unit=unit if unit in UNITS else None,
        min=_number(raw.get("min")),
        max=_number(raw.get("max")),
        step=_number(raw.get("step")),
        plan_generated_at=_ts(raw.get("plan_generated_at")),
        fallback_allowed=raw.get("fallback_allowed") is True,
    )


def parse_loads(raw: Any) -> tuple[list[LoadSlot], LoadsMeta | None]:
    """Sloty ładowarki i metadane z koperty; brak/śmieci → `([], None)`, nigdy wyjątek."""
    if not isinstance(raw, dict):
        return [], None
    items = raw.get("loads")
    slots = [s for s in map(_parse_slot, items) if s is not None] if isinstance(items, list) else []
    slots.sort(key=lambda s: s.start)
    return slots, _parse_meta(raw.get("loads_meta"))
