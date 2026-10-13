"""Tryb pracy falownika (General / inny) — tylko odczyt, do ostrzeżeń.

Moduł czysty (bez importów HA). Stan to `"general"` | `"other:<kod>"` | `"unknown"`; `<kod>` to opcja selecta
po normalizacji (kontrakt serwera: `^(general|unknown|other:[a-z0-9_]{1,24})$`). Odczyt bezpośredni
z rejestru (`classify_direct`) i odczyt z encji karmią ten sam `WorkModeTracker`."""
from __future__ import annotations

import re
from typing import Callable, Iterable

UNKNOWN = "unknown"
GENERAL = "general"
_CODE_MAX = 24
_UNUSABLE = frozenset({"unavailable", "unknown"})


def normalize_option(option) -> str | None:
    """Małe litery, każdy znak spoza [a-z0-9_] -> `_`, ucięte do 24 znaków; pusty wynik -> None."""
    if not isinstance(option, str):
        return None
    code = re.sub(r"[^a-z0-9_]", "_", option.lower())[:_CODE_MAX]
    return code or None


def classify_entity(option, neutral_options: Iterable[str]) -> str | None:
    """`general` | `other:<znormalizowana opcja>` | None (opcja bezużyteczna / odczyt nieudany: stan bez zmian)."""
    if isinstance(option, str) and option.strip().lower() in _UNUSABLE:
        return None
    code = normalize_option(option)
    if code is None:
        return None
    neutral = {n for n in (normalize_option(o) for o in neutral_options) if n}
    return GENERAL if code in neutral else f"other:{code}"


def classify_direct(value, neutral_values: Iterable[int]) -> str | None:
    """Odczyt rejestru: `general` | `other:<liczba dziesiętna>` | None (odczyt nieudany / nie liczba: stan bez zmian)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return GENERAL if value in tuple(neutral_values) else f"other:{value}"


class WorkModeTracker:
    """Ostatni udany odczyt; `unknown` tylko do pierwszego udanego."""

    def __init__(self) -> None:
        self._state = UNKNOWN
        # wołane po każdej zmianie stanu (oba źródła odczytu karmią ten sam tracker)
        self.on_change: Callable[[str], None] | None = None

    @property
    def state(self) -> str:
        return self._state

    def update(self, value: str | None) -> bool:
        """Nowy stan (None = odczyt nieudany, stan bez zmian). Zwraca, czy stan się zmienił."""
        if value is None or value == self._state:
            return False
        self._state = value
        if self.on_change is not None:
            self.on_change(value)
        return True


class WorkModeWatcher:
    """Karmi `WorkModeTracker` surowym stanem encji select (NIE przez `entity_value`)."""

    def __init__(self, entity_id: str, neutral_options: Iterable[str], *,
                 on_change: Callable[[str], None] | None = None,
                 tracker: WorkModeTracker | None = None) -> None:
        self.entity_id = entity_id
        self.tracker = tracker or WorkModeTracker()
        self._neutral = tuple(neutral_options)
        self._on_change = on_change

    def prime(self, state) -> None:
        """Stan encji z chwili startu (może być None, gdy encja jeszcze nie istnieje)."""
        self._feed(getattr(state, "state", None))

    def on_event(self, event) -> None:
        self._feed(getattr(event.data.get("new_state"), "state", None))

    def _feed(self, raw) -> None:
        if self.tracker.update(classify_entity(raw, self._neutral)) and self._on_change is not None:
            self._on_change(self.tracker.state)
