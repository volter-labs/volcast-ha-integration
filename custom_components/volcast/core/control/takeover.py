"""Obca zmiana nastawy: ktoś (użytkownik, automatyzacja) zmienił nastawę, której wartość
była zgodna z planem — nasz ostatni zapis albo odczyt, przy którym plan nie wymagał zapisu.

Odświeżenie stanu przez integrację falownika nie ma aktora (`user_id`/`parent_id`
kontekstu) — to rozjazd do uzgodnienia, nie przejęcie. Różnica mniejsza niż kwant
rejestru (1) nie jest zmianą.
"""
from __future__ import annotations

FOREIGN_PAUSE_S = 1800.0
_QUANTUM = 1.0


def is_foreign_change(*, ours: bool, has_actor: bool, new_value: float | str | None,
                      expected: float | str | None) -> bool:
    if ours or not has_actor or new_value is None or expected is None:
        return False
    if isinstance(new_value, str) or isinstance(expected, str):
        return new_value != expected
    return abs(float(new_value) - float(expected)) >= _QUANTUM
