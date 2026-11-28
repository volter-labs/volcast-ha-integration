"""Obca zmiana nastawy: ktoś (użytkownik, automatyzacja) zmienił nastawę, której wartość
była zgodna z planem — nasz ostatni zapis albo odczyt, przy którym plan nie wymagał zapisu.

Odświeżenie stanu przez integrację falownika nie ma aktora (`user_id`/`parent_id`
kontekstu) — to rozjazd do uzgodnienia, nie przejęcie. Różnica mniejsza niż kwant
rejestru (1) nie jest zmianą.

Model okien czasowych (`control_model == "time_window"`): już PIERWSZA obca zmiana pola programu TOU
(`tou.<n>.<pole>`) albo włącznika harmonogramu (`tou_enabled`) jest przejęciem tego klucza — nie
czekamy na drugi rozjazd (`conflict.DRIFT_LIMIT`), bo pojedyncza ręczna zmiana w aplikacji producenta
to świadoma decyzja właściciela, a jej nadpisanie w następnym cyklu ją kasuje. Pozostałe modele i klucze
zostają przy progu dwóch rozjazdów.
"""
from __future__ import annotations

FOREIGN_PAUSE_S = 1800.0
_QUANTUM = 1.0
_TOU_PREFIX = "tou."
_TOU_ENABLED = "tou_enabled"                     # = `tou_cycle.EN_KEY` (bez importu — cykl modułów)


def first_change_takes_over(control_model: str | None, key: str) -> bool:
    """Czy pierwszy rozjazd klucza jest od razu przejęciem (pola programów TOU w modelu okien czasowych)."""
    return control_model == "time_window" and isinstance(key, str) \
        and (key.startswith(_TOU_PREFIX) or key == _TOU_ENABLED)


def is_foreign_change(*, ours: bool, has_actor: bool, new_value: float | str | None,
                      expected: float | str | None) -> bool:
    if ours or not has_actor or new_value is None or expected is None:
        return False
    if isinstance(new_value, str) or isinstance(expected, str):
        return new_value != expected
    return abs(float(new_value) - float(expected)) >= _QUANTUM
