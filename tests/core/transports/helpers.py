"""Pomocnicze atrapy do testów transportów."""
from __future__ import annotations

import asyncio


class FakeClock:
    """Zegar monotoniczny sterowany ręcznie; `sleep` zapisuje żądany czas i przesuwa zegar."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt

    async def sleep(self, dt: float) -> None:
        self.sleeps.append(dt)
        self.now += dt
        await asyncio.sleep(0)
