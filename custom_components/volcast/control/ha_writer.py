"""Zapis parametru przez usługę encji innej integracji (tryb encji).

Każde wywołanie ma własny `Context`; jego id zapamiętujemy, żeby odróżnić nasze
zapisy od cudzych (obca zmiana). Wynik to kod `write_sequence` — wyjątek HA nigdy
nie wychodzi poza pisarza. Inne wyjątki łapie `async_run_writes` (ERROR z nazwą klasy).
"""
from __future__ import annotations

import asyncio
from collections import deque

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


class EntityServiceWriter:
    def __init__(self, hass, *, context_factory=Context, timeout_s: float = WRITE_TIMEOUT_S) -> None:
        self._hass = hass
        self._context_factory = context_factory
        self._timeout_s = timeout_s
        self._ours: deque[str] = deque(maxlen=_OWN_CONTEXTS)

    def is_ours(self, context_id: str | None) -> bool:
        """Czy zmiana stanu z tym kontekstem pochodzi z naszego zapisu."""
        return context_id is not None and context_id in self._ours

    async def async_write(self, w: EntityWrite) -> str:
        state = self._hass.states.get(w.entity_id)
        if state is None or state.state in _UNAVAILABLE:
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
        return OK
