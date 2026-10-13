"""Reguła awaryjna ładowarki: bez świeżego planu z chmury, a z terminem blisko — ładuj pełną mocą.

`should_force` = plan starszy niż `FALLBACK_PLAN_AGE_S` ∧ teraz ≥ `ready_by` − czas potrzebny na
`remaining_kwh` przy `max_power_w` − `FALLBACK_MARGIN_S` ∧ zostało coś do naładowania. Reguła może
tylko WŁĄCZYĆ ładowanie (nigdy nie zatrzymuje) i działa wyłącznie w bramkach wykonawcy (zgoda,
przełącznik, nie „tylko plan”, sterowanie ładowarką włączone) — tu jest tylko warunek czasu.

Wiek planu podaje wołający (czas od ostatniej odpowiedzi chmury, nie od wygenerowania planu).
Każda wartość spoza dziedziny (brak, NaN, nieskończoność, ≤ 0, data bez strefy, nieczytelny
status) = False — nigdy wyjątek ani dzielenie przez zero.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

FALLBACK_PLAN_AGE_S = 90 * 60
FALLBACK_MARGIN_S = 30 * 60
# Górna granica czasu potrzebnego (absurdalne metadane nie przepełniają daty; okno i tak już trwa).
_MAX_NEED_S = 30 * 24 * 3600
_UNREADABLE = ("unavailable", "unknown")


def _positive(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) and f > 0 else None


def _aware(d: Any) -> bool:
    return isinstance(d, datetime) and d.tzinfo is not None and d.utcoffset() is not None


def should_force(now: datetime, meta, plan_age_s: Any, status: Any) -> bool:
    """Czy wymusić ładowanie pełną mocą (plan stary, termin blisko, cel nieosiągnięty)."""
    if meta is None or getattr(meta, "control_enabled", None) is not True:
        return False
    if not isinstance(status, str) or not status or status in _UNREADABLE:
        return False
    age = _positive(plan_age_s)
    if age is None or age <= FALLBACK_PLAN_AGE_S:
        return False
    remaining = _positive(getattr(meta, "remaining_kwh", None))
    power = _positive(getattr(meta, "max_power_w", None))
    ready_by = getattr(meta, "ready_by", None)
    if remaining is None or power is None or not _aware(ready_by) or not _aware(now):
        return False
    need_s = min(remaining * 1000.0 / power * 3600.0, _MAX_NEED_S)
    return now >= ready_by - timedelta(seconds=need_s + FALLBACK_MARGIN_S)
