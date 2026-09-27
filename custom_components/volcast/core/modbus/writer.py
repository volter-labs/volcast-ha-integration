"""Pisarz rejestrów z odczytem zwrotnym po każdym zapisie.

| echo                     | odczyt zwrotny | wynik        |
|--------------------------|----------------|--------------|
| zgodne                   | = wartość      | OK           |
| zgodne                   | ≠ wartość      | DENIED       |
| zgodne                   | brak           | ERROR        |
| wyjątek 2                | —              | UNSUPPORTED  |
| wyjątek ≠ 2              | = / ≠ / brak   | OK / DENIED / ERROR |
| brak (albo obce echo)    | = wartość      | OK (zgubione potwierdzenie) |
| brak (albo obce echo)    | ≠ / brak       | ERROR        |

Ponowną wysyłkę (tylko UDP, przy całkowitej ciszy) i reset kanału po przekroczeniu czasu
robi transport — odczyt zwrotny idzie więc już świeżym kanałem i spóźnione echo zapisu nie
może go „ukraść”. Odczyt porównuje całe słowo (pola bitowe niosą bity właściciela).
Pisarz nigdy nie rzuca: każdy nieprzewidziany wyjątek to ERROR.
"""
from __future__ import annotations

import logging
from typing import Callable

from ..registers import RegisterWrite
from ..transports.base import ModbusException, TransportError
from ..write_sequence import DENIED, ERROR, OK, UNSUPPORTED
from .client import RegisterClient

_LOGGER = logging.getLogger(__name__)
_ECHO_OK, _ECHO_EXCEPTION, _ECHO_NONE = "ok", "exception", "none"


class RegisterWriter:
    def __init__(self, client: RegisterClient, profile, *,
                 on_send: Callable[[str], None] | None = None) -> None:
        self.client = client
        self.profile = profile
        self._on_send = on_send
        self._function = profile.modbus.write_function
        self.echo_only: frozenset[str] = frozenset()

    async def async_write(self, w: RegisterWrite) -> str:
        try:
            return await self._write(w)
        except Exception as err:  # noqa: BLE001 — pisarz nie rzuca; zapis niepewny
            _LOGGER.warning("direct write of %s failed: %s", w.key, type(err).__name__)
            return ERROR

    async def _write(self, w: RegisterWrite) -> str:
        if w.key in self.echo_only:
            return UNSUPPORTED               # bez odczytu zwrotnego nie piszemy (nie da się przywrócić)
        try:
            await self.client.transport.write(w.addr, [w.value], function=self._function,
                                              on_send=lambda: self._sent(w.key))
            echo = _ECHO_OK
        except ModbusException as err:
            if err.code == 2:
                return UNSUPPORTED
            echo = _ECHO_EXCEPTION           # wyjątek nie dowodzi, że rejestr jest nietknięty
        except TransportError as err:
            echo = _ECHO_NONE                # brak echa / zerwanie: zapis mógł dojść
            _LOGGER.debug("direct write of %s: %s", w.key, type(err).__name__)
        back = await self._read_back(w.addr)
        if back is None:
            return ERROR
        if back == w.value:
            return OK
        return ERROR if echo == _ECHO_NONE else DENIED

    async def _read_back(self, addr: int) -> int | None:
        try:
            return await self.client.read_register(addr)
        except TransportError as err:
            _LOGGER.debug("read-back failed: %s", type(err).__name__)
            return None

    def _sent(self, key: str) -> None:
        if self._on_send is None:
            return
        try:
            self._on_send(key)
        except Exception as err:  # noqa: BLE001 — licznik nie może zepsuć wymiany
            _LOGGER.error("write counter failed for %s: %s", key, type(err).__name__)


class NoWriteWriter:
    """Pisarz trybu próbnego: nigdy nie wysyła, zawsze DENIED (druga, niezależna blokada)."""

    def __init__(self) -> None:
        self.blocked_attempts = 0

    async def async_write(self, w) -> str:
        self.blocked_attempts += 1
        _LOGGER.error("direct write blocked in trial mode: %s", getattr(w, "key", "?"))
        return DENIED
