"""Zapis parametru przez usługę encji innej integracji (tryb encji).

Każde wywołanie ma własny `Context`; jego id zapamiętujemy, żeby odróżnić nasze
zapisy od cudzych (obca zmiana). Wynik to kod `write_sequence` — wyjątek HA nigdy
nie wychodzi poza pisarza. Inne wyjątki łapie `async_run_writes` (ERROR z nazwą klasy).

Odczyt zwrotny (tylko zapis z `EntityWrite.readback_s`, czyli profil z przełącznikiem zdalnego
sterowania albo mocą wg kierunku — `entity_map.entity_readback_s`; pozostałe zapisy jak dotąd):
- encja niedostępna przed zapisem → czekamy na jej dostępność najwyżej `readback_s` (select trybu
  bywa niedostępny tuż po włączeniu przełącznika); nadal niedostępna → ERROR bez wywołania usługi;
- po usłudze czekamy najwyżej `readback_s` na stan encji równy zamówionemu → OK;
- inaczej ERROR z przyczyną (`ReadbackOutcome`): `TIMEOUT` — stan bez zmian albo encja
  niedostępna (zapis mógł jeszcze dojść), `MISMATCH` — encja pokazuje inną wartość. Oba to ERROR
  dla sekwencji zapisów: tryb wstrzymany, wynik niejednoznaczny, bez cofania w ciemno.
Próba zapisu jest zawsze jedna — odczyt zwrotny nie ponawia usługi.
"""
from __future__ import annotations

import asyncio
import math
from collections import deque
from typing import Callable

import voluptuous as vol
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError, ServiceNotFound, ServiceValidationError

from ..core.entity_map import EntityWrite
from ..core.write_sequence import DENIED, ERROR, OK, UNSUPPORTED

WRITE_TIMEOUT_S = 10.0
_UNAVAILABLE = ("unavailable", "unknown")
# Ile ostatnich własnych kontekstów pamiętamy — kilka zapisów na cykl, zdarzenie
# zmiany stanu przychodzi zaraz po zapisie, więc zapas na kilkanaście cykli wystarcza.
_OWN_CONTEXTS = 128
# Odstęp sprawdzania stanu encji w czasie odczytu zwrotnego / oczekiwania na dostępność.
READBACK_POLL_S = 0.25

# Przyczyny nieudanego odczytu zwrotnego (`ReadbackOutcome.reason`).
MISMATCH = "mismatch"
TIMEOUT = "timeout"


class ReadbackOutcome(str):
    """Wynik ERROR po odczycie zwrotnym, z przyczyną (`MISMATCH`/`TIMEOUT`) i ostatnim stanem encji."""

    reason: str
    actual: str | None

    def __new__(cls, reason: str, actual: str | None) -> "ReadbackOutcome":
        obj = super().__new__(cls, ERROR)
        obj.reason = reason
        obj.actual = actual
        return obj


def _unavailable(state) -> bool:
    return state is None or state.state in _UNAVAILABLE


def _matches(w: EntityWrite, state: str) -> bool:
    """Czy stan encji odpowiada zamówionej wartości zapisu."""
    if w.domain == "select":
        return state == w.data.get("option")
    if w.domain == "switch":
        return state == {"turn_on": "on", "turn_off": "off"}.get(w.service)
    if w.domain == "time":
        return state[:5] == str(w.data.get("time", ""))[:5]           # HH:MM
    try:
        actual, wanted = float(state), float(w.data["value"])
    except (KeyError, TypeError, ValueError):
        return False
    return math.isfinite(actual) and math.isclose(actual, wanted, rel_tol=1e-6, abs_tol=1e-9)


class EntityServiceWriter:
    def __init__(self, hass, *, context_factory=Context, timeout_s: float = WRITE_TIMEOUT_S,
                 readback_poll_s: float = READBACK_POLL_S) -> None:
        self._hass = hass
        self._context_factory = context_factory
        self._timeout_s = timeout_s
        self._poll_s = readback_poll_s
        self._ours: deque[str] = deque(maxlen=_OWN_CONTEXTS)

    def is_ours(self, context_id: str | None) -> bool:
        """Czy zmiana stanu z tym kontekstem pochodzi z naszego zapisu."""
        return context_id is not None and context_id in self._ours

    async def async_write(self, w: EntityWrite) -> str:
        limit = getattr(w, "readback_s", None)
        state = self._hass.states.get(w.entity_id)
        if limit is not None and _unavailable(state):
            state = await self._await_state(w.entity_id, lambda s: not _unavailable(s), limit)
        if _unavailable(state):
            return ERROR            # chwilowo niedostępna — następny cykl spróbuje znowu
        if w.domain == "select":
            options = state.attributes.get("options")
            if isinstance(options, list) and w.data.get("option") not in options:
                return UNSUPPORTED  # ta instalacja nie zna tego trybu — nie próbujemy w kółko
        ctx = self._context_factory()
        # Zapamiętane PRZED wywołaniem: integracja zapisuje stan z naszym kontekstem
        # jeszcze w trakcie usługi, a zdarzenie może dojść przed jej zakończeniem.
        self._ours.append(ctx.id)
        try:
            await asyncio.wait_for(
                self._hass.services.async_call(w.domain, w.service, {"entity_id": w.entity_id, **w.data},
                                               blocking=True, context=ctx),
                self._timeout_s)
        # ServiceNotFound dziedziczy po ServiceValidationError, a ta po HomeAssistantError —
        # kolejność gałęzi ma znaczenie.
        except ServiceNotFound:
            return UNSUPPORTED
        except (ServiceValidationError, vol.Invalid):
            return DENIED
        except (HomeAssistantError, asyncio.TimeoutError):
            return ERROR
        if limit is None:
            return OK
        return await self._readback(w, state.state, limit)

    async def _readback(self, w: EntityWrite, before: str, limit: float) -> str:
        after = await self._await_state(w.entity_id, lambda s: not _unavailable(s) and _matches(w, s.state),
                                        limit)
        if not _unavailable(after) and _matches(w, after.state):
            return OK
        actual = None if after is None else after.state
        if _unavailable(after) or after.state == before:
            return ReadbackOutcome(TIMEOUT, actual)
        return ReadbackOutcome(MISMATCH, actual)

    async def _await_state(self, entity_id: str, done: Callable, limit: float):
        """Stan encji, gdy spełni `done`, albo ostatni stan po upływie `limit` sekund."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, limit)
        while True:
            state = self._hass.states.get(entity_id)
            if done(state):
                return state
            left = deadline - loop.time()
            if left <= 0:
                return state
            await asyncio.sleep(min(self._poll_s, left))
