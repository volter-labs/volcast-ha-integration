"""Czekanie na potwierdzenie parowania (odpytywanie sesji w tle kreatora).

HA nie musi być dostępny z internetu: to integracja pyta chmurę, czy właściciel
potwierdził sesję w aplikacji/na stronie. Terminalne statusy wracają od razu; błędy
sieci — z narastającym odstępem, do limitu. `consumed` w trakcie czekania też jest
terminalny: klucz poszedł w odpowiedzi, która do nas nie dotarła, więc dalsze
odpytywanie nic nie da (nowe parowanie odda ten sam klucz konta)."""
from __future__ import annotations

import asyncio
import time

from .cloud.client import PollResult

_TERMINAL = ("confirmed", "consumed", "expired", "gone", "disabled")
_MAX_BACKOFF_S = 30.0


class PairingPoller:
    def __init__(self, client, session, *, interval_s: float = 3.0, deadline_s: float = 600.0,
                 max_errors: int = 20, sleep=asyncio.sleep, clock=time.monotonic) -> None:
        self._client, self._session = client, session
        self._interval, self._deadline, self._max_errors = interval_s, deadline_s, max_errors
        self._sleep, self._clock = sleep, clock

    async def async_wait(self) -> PollResult:
        end = self._clock() + self._deadline
        errors = 0
        while self._clock() < end:
            result = await self._client.async_poll(self._session)
            if result.status in _TERMINAL:
                return result
            if result.status == "error":
                errors += 1
                if errors >= self._max_errors:
                    return result
                await self._sleep(min(_MAX_BACKOFF_S, self._interval * 2 ** min(errors, 4)))
                continue
            errors = 0
            await self._sleep(self._interval)
        return PollResult("expired")
