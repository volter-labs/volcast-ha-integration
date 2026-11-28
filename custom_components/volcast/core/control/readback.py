"""Rozbieżności odczytu zwrotnego zapisów rejestrów: ostatnie pozycje do encji Status i zdarzenia HA.

Czysty moduł (bez zależności od HA): zdarzenie wystawia wołający przez `fire`. Pozycje niosą
wyłącznie nazwę klucza rejestru, wartości liczbowe, wynik i czas — bez encji ani numerów seryjnych."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Protocol

MAX_ISSUES = 5
EVENT = "volcast_control_readback"


class ReadbackSink(Protocol):
    def issue(self, key: str, expected: int | None, read: int | None, result: str) -> None: ...

    def ok(self, key: str) -> None: ...


class ReadbackLog:
    def __init__(self, fire: Callable[[dict[str, Any]], None] | None = None, *,
                 now: Callable[[], datetime] | None = None) -> None:
        self._fire = fire
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._items: list[dict[str, Any]] = []

    def issue(self, key: str, expected: int | None, read: int | None, result: str) -> None:
        item = {"key": key, "expected": expected, "read": read, "result": result,
                "at": self._now().isoformat()}
        self._items.append(item)
        del self._items[:-MAX_ISSUES]
        if self._fire is not None:
            try:
                self._fire(dict(item))
            except Exception:  # noqa: BLE001 — widoczność nie może zepsuć zapisu
                pass

    def ok(self, key: str) -> None:
        """Udany zapis klucza kasuje jego pozycje."""
        self._items = [i for i in self._items if i["key"] != key]

    @property
    def items(self) -> list[dict[str, Any]]:
        return [dict(i) for i in self._items]
