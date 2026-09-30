"""Pobieranie planu (pull co 5 min). Błąd sieci nie kasuje planu lokalnego; zły
plan jest odrzucany w całości; zgoda z tej samej odpowiedzi jest stosowana nawet
przy odrzuconym planie (cofnięcie zgody nie może czekać na poprawny plan).
Deduplikacja po `schedule_id` ORAZ treści — id bywa to samo przy innej treści.

Odrzucony plan loguje ostrzeżenie raz na treść (chmura serwuje go co 5 min).
Wyjątek klienta albo callbacku kończy przebieg wynikiem "error" (sama nazwa klasy
w logu) — wołający po odświeżeniu zawsze robi swoje (np. cykl wykonawcy).
Plan, którego callback się nie powiódł, nie jest zapamiętany — następny przebieg
poda go jeszcze raz."""
from __future__ import annotations

import json
import logging
from typing import Awaitable, Callable

from ..core.slot import InvalidSchedule, Schedule, parse_schedule
from .client import CloudAuthError

_LOGGER = logging.getLogger(__name__)
SCHEDULE_FETCH_INTERVAL_S = 300


class ScheduleFetcher:
    def __init__(self, cloud, *, on_plan: Callable[[dict, Schedule], Awaitable[None]],
                 on_consent: Callable[[bool], Awaitable[None]],
                 on_auth_failure: Callable[[int], Awaitable[None]],
                 on_signals: Callable[[dict | None], Awaitable[None]] | None = None) -> None:
        self._cloud = cloud
        self._on_plan = on_plan
        self._on_consent = on_consent
        self._on_auth_failure = on_auth_failure
        self._on_signals = on_signals
        self._auth_failures = 0
        self._last_signature: str | None = None
        self._last_rejected: str | None = None
        self._last_error: str | None = None

    async def async_refresh(self) -> str:
        try:
            result = await self._async_refresh()
        except Exception as err:  # noqa: BLE001 — odświeżenie planu nie może wywrócić wołającego
            name = type(err).__name__
            if name != self._last_error:
                _LOGGER.warning("Volcast plan refresh failed (%s)", name)
            else:
                _LOGGER.debug("Volcast plan refresh failed again (%s)", name)
            self._last_error = name
            return "error"
        self._last_error = None
        return result

    async def _async_refresh(self) -> str:
        try:
            raw = await self._cloud.async_get_schedule()
        except CloudAuthError:
            self._auth_failures += 1
            await self._on_auth_failure(self._auth_failures)
            return "auth"
        if raw is None:
            return "network"
        self._auth_failures = 0
        await self._async_signals(raw)
        consent = raw.get("control_enabled")
        if isinstance(consent, bool):
            await self._on_consent(consent)
        body = {k: v for k, v in raw.items() if k not in ("control_enabled", "signals")}
        signature = json.dumps(body, sort_keys=True, default=str)
        if signature == self._last_signature:
            return "unchanged"
        try:
            schedule = parse_schedule(raw)
        except InvalidSchedule as err:
            if signature != self._last_rejected:
                _LOGGER.warning("Volcast plan rejected (%s) — keeping the previous plan", err.field)
            else:
                _LOGGER.debug("Volcast plan still rejected (%s)", err.field)
            self._last_rejected = signature
            return "rejected"
        await self._on_plan(raw, schedule)
        self._last_signature = signature
        self._last_rejected = None
        return "accepted"

    async def _async_signals(self, raw: dict) -> None:
        """Blok `signals` do odbiorcy PRZED deduplikacją; jego błąd nie wywraca odświeżenia."""
        if self._on_signals is None:
            return
        block = raw.get("signals")
        try:
            await self._on_signals(block if isinstance(block, dict) else None)
        except Exception as err:  # noqa: BLE001 — callback sygnałów nie może zatrzymać planu
            _LOGGER.debug("Volcast signals callback failed (%s)", type(err).__name__)
