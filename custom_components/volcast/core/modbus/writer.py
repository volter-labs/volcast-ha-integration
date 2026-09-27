"""Pisarz rejestrów: odczyt PRZED zapisem, zapis, odczyt zwrotny — wynik z porównania.

Żaden rejestr nie jest pisany bez możliwości odczytu zwrotnego, chyba że profil jawnie
wymienia go w `modbus.echo_only` (potwierdzenie samym echem, jak urządzenie referencyjne).
Odczyt przed zapisem (jedna ramka FC 3, bez kosztu NVM) pozwala odróżnić „nie ustawił”
od „ustawił inaczej”. Odmowa (DENIED) to WYŁĄCZNIE prawdziwa odmowa urządzenia: ramka zapisu
poszła, urządzenie odpowiedziało, a rejestr został bez zmian. Wszystko, co nie wysłało zapisu
(nieudany odczyt przed zapisem, adres spoza profilu), to ERROR — chwilowa awaria nie może
zostać zapamiętana jako odmowa (rdzeń wstrzymuje ponowienie odmówionej prośby).

| echo                      | odczyt zwrotny                 | wynik |
|---------------------------|--------------------------------|-------|
| —  (odczyt przed: wyjątek 2) | —                           | UNSUPPORTED (nic nie wysłano) |
| —  (odczyt przed: inny błąd) | —                           | ERROR (nic nie wysłano) |
| wyjątek 2                 | —                              | UNSUPPORTED |
| dowolne                   | = zamówiona                    | OK |
| dowolne                   | brak                           | ERROR |
| zgodne / wyjątek ≠ 2, 5   | = sprzed zapisu                | DENIED |
| zgodne / wyjątek ≠ 2, 5   | inna, w stronę bezpieczną      | OK_ADJUSTED z wartością rzeczywistą (tylko liczby) |
| zgodne / wyjątek ≠ 2, 5   | inna, w stronę groźną / nie-liczba | ERROR |
| brak / wyjątek 5          | ≠ zamówiona                    | ERROR (zapis mógł jeszcze dojść) |

Pola bitowe (bit ładowania z sieci programu, włącznik harmonogramu) są składane na słowie
z odczytu PRZED zapisem, nie z obrazu ostatniego odpytania.

Ponowną wysyłkę (tylko UDP, przy całkowitej ciszy) i reset kanału po przekroczeniu czasu
robi transport — odczyt zwrotny idzie świeżym kanałem. Odczyt porównuje całe słowo (pola
bitowe niosą bity właściciela). Jeden zamek na pisarza: odczyt przed, zapis i odczyt zwrotny
jednego klucza nie przeplatają się z innym zapisem. Pisarz nigdy nie rzuca.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Iterable

from ..registers import RegisterWrite
from ..transports.base import ModbusException, TransportError
from ..write_sequence import DENIED, ERROR, OK, UNSUPPORTED, AdjustedOutcome
from .client import RegisterClient

_LOGGER = logging.getLogger(__name__)
_ECHO_OK, _ECHO_EXCEPTION, _ECHO_NONE = "ok", "exception", "none"
_EXC_ACKNOWLEDGE = 5                   # „przyjęte, w trakcie” — jak brak potwierdzenia
# Klucze liczbowe, dla których wartość przycięta przez urządzenie ma sens jako „zastosowana”.
_NUMERIC_ENCODINGS = ("watts", "percent")


def _matches(key: str, keys: frozenset[str]) -> bool:
    """Klucz w zbiorze; `tou` obejmuje pola programów (`tou.<i>.<pole>`) i włącznik."""
    return key in keys or ("tou" in keys and (key.startswith("tou.") or key == "tou_enable"))


def expected_address(profile, key: str) -> int | None:
    """Adres rejestru klucza według profilu; None = klucz spoza map zapisu."""
    spec = profile.raw["write"]
    if key.startswith("tou."):
        parts = key.split(".")
        tp = spec.get("tou_program")
        if tp is None or len(parts) != 3 or not parts[1].isdigit() or parts[2] not in tp \
                or not 1 <= int(parts[1]) <= tp["count"]:
            return None
        return tp[parts[2]]["addr"] + int(parts[1]) - 1
    s = spec.get(key)
    return s.get("addr") if isinstance(s, dict) else None


# Kierunek bezpieczny odchylenia: -1 = rzeczywista nie większa niż zamówiona (moc, limit eksportu,
# górny próg), +1 = nie mniejsza (dolny próg). Odchylenie w drugą stronę = ERROR (np. minimalna moc
# urządzenia ponad zamówioną: tryb pracowałby na większej mocy niż w planie).
_SAFE_DEVIATION = {"power_w": -1, "export_limit_w": -1, "soc_max": -1, "soc_min": 1, "tou.power_w": -1}


def _numeric_actual(profile, key: str, word: int, requested: int) -> float | None:
    """Rzeczywista wartość klucza liczbowego z odczytu, gdy odchylenie jest w stronę bezpieczną;
    None dla trybu, przełączników, bitów, startów, SoC programów i odchylenia w stronę groźną."""
    if key.startswith("tou."):
        name = "tou." + key.split(".")[-1]
    else:
        name = key
        if ((profile.raw["write"].get(key) or {}).get("encode")) not in _NUMERIC_ENCODINGS:
            return None
    direction = _SAFE_DEVIATION.get(name)
    if direction is None or (word - requested) * direction < 0:
        return None
    return float(word)


def _fresh_value(profile, key: str, value: int, before: int) -> int:
    """Pole bitowe: odczyt-modyfikacja-zapis na słowie ŚWIEŻO odczytanym przed zapisem
    (bity właściciela zmienione od ostatniego odpytania nie mogą zostać nadpisane)."""
    write = profile.raw["write"]
    if key.startswith("tou.") and key.endswith(".grid_charge"):
        mask = 1 << write["tou_program"]["grid_charge"]["bit"]
        return (before & ~mask & 0xFFFF) | (value & mask)
    if key == "tou_enable":
        spec = write["tou_enable"]
        ebit = 1 << spec["enable_bit"]
        if not value & ebit:
            return before & ~ebit & 0xFFFF
        days = spec.get("day_mask", 0)
        out = before | ebit
        if before & days == 0:
            out |= (value & days) or days          # harmonogram „w żadnym dniu” byłby martwy
        return out & 0xFFFF
    return value


class RegisterWriter:
    def __init__(self, client: RegisterClient, profile, *,
                 on_send: Callable[[str], None] | None = None, unreadable: Iterable[str] = ()) -> None:
        self.client = client
        self.profile = profile
        self._on_send = on_send
        self._function = profile.modbus.write_function
        self._lock = asyncio.Lock()
        # potwierdzane samym echem — wyłącznie z jawnej listy profilu
        self.echo_only: frozenset[str] = frozenset(profile.modbus.echo_only)
        # bez odczytu zwrotnego (z sondy): nie pisane wcale, nawet bez próby odczytu
        self.unreadable: frozenset[str] = frozenset(unreadable)

    async def async_write(self, w: RegisterWrite) -> str:
        try:
            async with self._lock:
                return await self._write(w)
        except Exception as err:  # noqa: BLE001 — pisarz nie rzuca; zapis niepewny
            _LOGGER.warning("direct write of %s failed: %s", w.key, type(err).__name__)
            return ERROR

    async def _write(self, w: RegisterWrite) -> str:
        if expected_address(self.profile, w.key) != w.addr or isinstance(w.value, bool) \
                or not isinstance(w.value, int) or not 0 <= w.value <= 0xFFFF:
            _LOGGER.error("direct write of %s refused: address or value outside the profile", w.key)
            return ERROR                   # nic nie wysłano — to nie odmowa urządzenia
        if _matches(w.key, self.echo_only):
            return await self._write_echo_only(w)
        if _matches(w.key, self.unreadable):
            return UNSUPPORTED             # bez odczytu zwrotnego nie piszemy (nie da się przywrócić)
        try:
            before = await self.client.read_register(w.addr)
        except TransportError as err:
            if isinstance(err, ModbusException) and err.code == 2:
                return UNSUPPORTED
            _LOGGER.debug("pre-write read of %s failed: %s", w.key, type(err).__name__)
            return ERROR                   # nic nie wysłano; chwilowa awaria, nie odmowa
        w = RegisterWrite(w.key, w.addr, _fresh_value(self.profile, w.key, w.value, before))
        echo = await self._send(w)
        if echo == UNSUPPORTED:
            return UNSUPPORTED
        back = await self._read_back(w.addr)
        if back is None:
            return ERROR
        if back == w.value:
            return OK
        if echo == _ECHO_NONE:
            return ERROR
        if back == before:
            return DENIED
        actual = _numeric_actual(self.profile, w.key, back, w.value)
        if actual is None:
            return ERROR                   # tryb/bit/start inny albo odchylenie w groźną stronę
        _LOGGER.warning("direct write of %s applied with a different value by the inverter", w.key)
        return AdjustedOutcome(actual)

    async def _send(self, w: RegisterWrite) -> str:
        try:
            await self.client.transport.write(w.addr, [w.value], function=self._function,
                                              on_send=lambda: self._sent(w.key))
            return _ECHO_OK
        except ModbusException as err:
            if err.code == 2:
                return UNSUPPORTED
            # wyjątek nie dowodzi, że rejestr jest nietknięty; 5 = „przyjęte, w trakcie”
            return _ECHO_NONE if err.code == _EXC_ACKNOWLEDGE else _ECHO_EXCEPTION
        except TransportError as err:
            _LOGGER.debug("direct write of %s: %s", w.key, type(err).__name__)
            return _ECHO_NONE              # brak echa / zerwanie: zapis mógł dojść

    async def _write_echo_only(self, w: RegisterWrite) -> str:
        echo = await self._send(w)
        if echo == UNSUPPORTED:
            return UNSUPPORTED
        return OK if echo == _ECHO_OK else ERROR

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
