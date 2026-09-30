"""Punkt zbiorczy bloku `signals`: kanał, nadawca „live" i odświeżanie planu.

Blok sygnałów nigdy nie może wywrócić planu ani telemetrii — wyjątki atrap/zależności
są łapane, w logu tylko nazwa klasy (bez treści, bo mogą zawierać dane kanału).

Pobranie planu (z cyklem wykonawcy) biegnie ZAWSZE we własnym zadaniu huba, nigdy w zadaniu
wołającego: wołającym jest kanał (join, ping), a pobranie może przez `apply` przełączyć albo
zatrzymać ten kanał — w jego zadaniu anulowałoby samo siebie w pół zgody/planu/zapisu. Z tego
samego powodu zatrzymanie huba anuluje tylko pobrania czekające (okno, blokada), a na pobranie
w toku czeka z limitem — nie ucina zapisu wykonawcy (jak `executor.async_stop`).
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from ..cloud.signals import parse_signals
from ..const import STOP_WRITE_TIMEOUT_S

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
        stop_timeout_s: float = STOP_WRITE_TIMEOUT_S,
    ) -> None:
        self._base_url = base_url
        self._channel = channel
        self._live = live
        self._refresh = refresh
        self._mono = monotonic
        self._sleep = sleep
        self._task_factory = task_factory or _default_task_factory
        self._stop_timeout_s = stop_timeout_s
        self._last: float | None = None  # początek ostatniego pobrania
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        # Zaplanowane pobranie, które jeszcze nie ruszyło (czeka na okno). Znacznik stawiany PRZED
        # fabryką (gorliwy start), uchwyt — po niej (zadanie anulowane przed startem nie zdejmie znacznika).
        self._waiting = False
        self._waiting_task: asyncio.Task[None] | None = None
        self._refreshing: asyncio.Task[None] | None = None
        self._stopped = False

    async def apply(self, raw: dict | None) -> None:
        if self._stopped:
            return          # po zatrzymaniu spóźniona odpowiedź nie wskrzesza kanału ani nadawania
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
        """Debounce: pierwsze pobranie od razu, reszta zlana w jedno opóźnione — zawsze w zadaniu huba."""
        if self._stopped:
            return
        if self._waiting and (self._waiting_task is None or not self._waiting_task.done()):
            return
        wait = 0.0 if self._last is None else max(0.0, REFRESH_WINDOW_S - (self._mono() - self._last))
        self._waiting, self._waiting_task = True, None
        task = self._task_factory(self._delayed(wait), "volcast-signals-refresh")
        if self._waiting:
            self._waiting_task = task
        if not task.done():
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _delayed(self, wait: float) -> None:
        try:
            if wait > 0:
                await self._sleep(wait)
        finally:
            # zwalniamy miejsce przed pobraniem — kolejne żądanie może zaplanować następne
            self._waiting, self._waiting_task = False, None
        await self._run()

    async def _run(self) -> None:
        async with self._lock:
            if self._stopped:
                return
            self._last = self._mono()
            self._refreshing = asyncio.current_task()
            try:
                await self._refresh()
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("signals: refresh failed: %s", type(err).__name__)
            finally:
                self._refreshing = None

    def freeze(self) -> None:
        """Synchronicznie: koniec nowych pobrań i `apply`; czekające anulowane, pobranie w toku zostaje."""
        self._stopped = True
        current = asyncio.current_task()
        for task in list(self._tasks):
            if task is not self._refreshing and task is not current and not task.done():
                task.cancel()

    async def async_stop(self) -> None:
        """Anuluje czekające pobrania; na pobranie w toku czeka najwyżej `stop_timeout_s`, nie ucina go."""
        self.freeze()
        current = asyncio.current_task()
        tasks = {t for t in self._tasks if t is not current and not t.done()}
        if not tasks:
            return
        _done, pending = await asyncio.wait(tasks, timeout=self._stop_timeout_s)
        if pending:
            _LOGGER.warning("signals: refresh still running at stop — not waiting any longer")
