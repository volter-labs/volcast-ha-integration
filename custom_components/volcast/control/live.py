"""Nadawanie na żywo: co 3 s lekki odczyt `persist:false` do `device-telemetry`, póki trwa okno.

Okno ustawia `update(live_for_s)` (czas względny z ostatniego bloku `signals`, sufit
`LIVE_MAX_S`; 0 gasi). Każda odpowiedź 200 niesie świeży `signals` — idzie do `on_signals`
(hub → `update`), więc chmura sama przedłuża albo zamyka okno. 409 (okno zamknięte w
chmurze) i 401 kończą pętlę od razu; brak odpowiedzi i 5xx — następny tik. Pętla jest
sekwencyjna (najwyżej jedno żądanie w locie), a żaden wyjątek poza anulowaniem jej nie
kończy. W logu tylko status albo nazwa klasy wyjątku.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from ..cloud.signals import LIVE_MAX_S

_LOGGER = logging.getLogger(__name__)

# odstęp odczytów na żywo (s)
LIVE_INTERVAL_S = 3.0
# statusy kończące okno bez czekania na termin: zamknięte w chmurze, zły klucz
_STOP_STATUSES = (401, 409)


def _default_task_factory(coro: Coroutine[Any, Any, None], name: str) -> asyncio.Task[None]:
    return asyncio.get_running_loop().create_task(coro, name=name)


def _current_task() -> asyncio.Task | None:
    try:
        return asyncio.current_task()
    except RuntimeError:          # wywołanie poza pętlą zdarzeń
        return None


class LiveSender:
    def __init__(
        self,
        *,
        cloud: Any,
        telemetry: Any,
        on_signals: Callable[[dict | None], Awaitable[None]],
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        task_factory: Callable[[Coroutine[Any, Any, None], str], asyncio.Task[None]] | None = None,
        interval_s: float = LIVE_INTERVAL_S,
    ) -> None:
        self._cloud = cloud
        self._telemetry = telemetry
        self._on_signals = on_signals
        self._mono = monotonic
        self._sleep = sleep
        self._task_factory = task_factory or _default_task_factory
        self._interval = interval_s
        self._deadline = 0.0
        self._task: asyncio.Task[None] | None = None
        # Fabryka może wystartować pętlę gorliwie: pierwszy tik (200 → hub → `update`) biegnie,
        # zanim uchwyt trafi do `_task`. Znacznik ustawiony PRZED fabryką mówi wtedy „pętla już jest”.
        self._starting = False

    @property
    def running(self) -> bool:
        return self._starting or (self._task is not None and not self._task.done())

    def update(self, live_for_s: int) -> None:
        """Ustawia koniec okna na teraz + min(live_for_s, sufit); ≤ 0 gasi. Nie blokuje."""
        n = live_for_s if isinstance(live_for_s, int) and not isinstance(live_for_s, bool) else 0
        if n <= 0:
            self._deadline = 0.0
            task = self._task
            # z wnętrza pętli (on_signals → hub → update) tylko termin — pętla wyjdzie sama
            if task is not None and not task.done() and task is not _current_task():
                task.cancel()
            return
        self._deadline = self._mono() + min(n, LIVE_MAX_S)
        if not self.running:
            self._starting = True
            try:
                self._task = self._task_factory(self._run(), "volcast-live")
            finally:
                self._starting = False

    def _open(self) -> bool:
        return self._mono() < self._deadline

    async def _run(self) -> None:
        while self._open():
            if await self._tick():
                self._deadline = 0.0
                return
            if not self._open():
                return
            await self._sleep(self._interval)

    async def _tick(self) -> bool:
        """Jeden odczyt na żywo; True = koniec okna (409/401)."""
        try:
            reading = self._telemetry.build_live_reading()
            if reading is None:
                return False
            res = await self._cloud.async_post_telemetry(reading, persist=False)
            if res.status in _STOP_STATUSES:
                _LOGGER.debug("Volcast live: stopped by cloud (%s)", res.status)
                return True
            if res.ok is True:
                await self._on_signals(res.signals_raw)
            else:
                _LOGGER.debug("Volcast live: post not accepted (%s)", res.status)
        except Exception as err:  # noqa: BLE001 — pojedynczy tik nigdy nie kończy okna
            _LOGGER.debug("Volcast live: tick failed (%s)", type(err).__name__)
        return False

    async def async_stop(self) -> None:
        """Gasi okno i czeka na koniec pętli; anulowanie samego `async_stop` przechodzi dalej."""
        self._deadline = 0.0
        task, self._task = self._task, None
        if task is None or task.done() or task is _current_task():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            current = _current_task()
            if current is not None and current.cancelling():
                raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Volcast live: stop failed (%s)", type(err).__name__)
