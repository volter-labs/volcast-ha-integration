"""Punkt zbiorczy bloku `signals`: kanał, nadawca „live" i odświeżanie planu.

Blok sygnałów nigdy nie może wywrócić planu ani telemetrii — wyjątki atrap/zależności
są łapane, w logu tylko nazwa klasy (bez treści, bo mogą zawierać dane kanału).
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from ..cloud.signals import parse_signals

_LOGGER = logging.getLogger(__name__)

# najwyżej jedno pobranie planu na to okno (s)
REFRESH_WINDOW_S = 5.0


def _default_task_factory(coro: Coroutine[Any, Any, None], name: str) -> asyncio.Task[None]:
    return asyncio.get_running_loop().create_task(coro, name=name)


class SignalsHub:
    def __init__(
        self,
        *,
        base_url: str,
        channel: Any,
        live: Any,
        refresh: Callable[[], Awaitable[None]],
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        task_factory: Callable[[Coroutine[Any, Any, None], str], asyncio.Task[None]] | None = None,
    ) -> None:
        self._base_url = base_url
        self._channel = channel
        self._live = live
        self._refresh = refresh
        self._mono = monotonic
        self._sleep = sleep
        self._task_factory = task_factory or _default_task_factory
        self._last: float | None = None  # początek ostatniego pobrania
        self._lock = asyncio.Lock()
        self._pending: asyncio.Task[None] | None = None

    async def apply(self, raw: dict | None) -> None:
        sig = parse_signals(raw, base_url=self._base_url)
        cfg = sig.channel if sig else None
        live_for_s = sig.live_for_s if sig else 0
        try:
            await self._channel.async_update(cfg)
        except Exception as err:  # noqa: BLE001 — blok nie może wywrócić planu
            _LOGGER.warning("signals: channel update failed: %s", type(err).__name__)
        try:
            res = self._live.update(live_for_s)
            if inspect.isawaitable(res):
                await res
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("signals: live update failed: %s", type(err).__name__)

    async def request_refresh(self) -> None:
        """Debounce: pierwsze wywołanie od razu, reszta zlana w jedno opóźnione."""
        if self._pending is not None:
            return
        wait = 0.0 if self._last is None else REFRESH_WINDOW_S - (self._mono() - self._last)
        if wait <= 0 and not self._lock.locked():
            await self._run()
            return
        self._pending = self._task_factory(self._delayed(wait), "volcast-signals-refresh")

    async def _delayed(self, wait: float) -> None:
        try:
            if wait > 0:
                await self._sleep(wait)
            # zwalniamy miejsce przed pobraniem — kolejne żądanie może zaplanować następne
            self._pending = None
            await self._run()
        finally:
            if self._pending is asyncio.current_task():
                self._pending = None

    async def _run(self) -> None:
        async with self._lock:
            self._last = self._mono()
            try:
                await self._refresh()
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("signals: refresh failed: %s", type(err).__name__)

    async def async_stop(self) -> None:
        task, self._pending = self._pending, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
