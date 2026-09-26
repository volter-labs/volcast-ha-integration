"""Obca zmiana nastawy: ktoś (użytkownik, automatyzacja) zmienił to, co my zapisaliśmy.

Odświeżenie stanu przez integrację falownika nie ma aktora (`user_id`/`parent_id`
kontekstu) — to rozjazd do uzgodnienia, nie przejęcie. Różnica mniejsza niż kwant
rejestru (1) nie jest zmianą.
"""
from __future__ import annotations

FOREIGN_PAUSE_S = 1800.0
_QUANTUM = 1.0


def is_foreign_change(*, ours: bool, has_actor: bool, new_value: float | str | None,
                      last_written: float | str | None) -> bool:
    if ours or not has_actor or new_value is None or last_written is None:
        return False
    if isinstance(new_value, str) or isinstance(last_written, str):
        return new_value != last_written
    return abs(float(new_value) - float(last_written)) >= _QUANTUM
