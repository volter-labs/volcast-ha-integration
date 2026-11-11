"""Atrapa łącza z osobnymi bankami rejestrów per adres urządzenia — rejestr z własnym `unit_id`
czytany albo pisany z adresu połączenia trafia w inny bank (test widzi to od razu)."""
from custom_components.volcast.core.transports.base import TransportStats

from .engine_profile import PLANT

LINK = "link"


class PlantLink:
    """Łącze z bankami per adres urządzenia (None w żądaniu = adres połączenia). `apply(unit, addr, word)`
    — słowo, które urządzenie naprawdę zapisze (domyślnie zamówione)."""

    def __init__(self, banks=None, *, kind="modbus_tcp", apply=None):
        self.kind = kind
        self.stats = TransportStats()
        self.banks = {LINK: {}, PLANT: {}}
        for unit, words in (banks or {}).items():
            self.banks[unit].update(words)
        self.apply = apply or (lambda unit, addr, word: word)
        self.calls: list[tuple] = []
        self.last_read_count = None

    def _bank(self, unit):
        return self.banks.setdefault(LINK if unit is None else unit, {})

    async def read(self, addr, count, *, tries=None, fc=3, unit=None):
        self.calls.append(("read", addr, count, fc, unit))
        self.stats.requests += 1
        self.last_read_count = count
        bank = self._bank(unit)
        return [bank.get(a, 0) for a in range(addr, addr + count)]

    async def write(self, addr, values, *, function, on_send=None, may_resend=None, unit=None):
        self.calls.append(("write", addr, list(values), function, unit))
        self.stats.requests += 1
        if on_send is not None:
            on_send()
        bank = self._bank(unit)
        for i, v in enumerate(values):
            bank[addr + i] = self.apply(LINK if unit is None else unit, addr + i, v)


class StrictLink(PlantLink):
    """Łącze sprzed adresów per rejestr: `read`/`write` BEZ parametru `unit` (i bez `fc`) — zapis
    klucza bez nowych pól musi wołać transport dokładnie jak dotąd."""

    async def read(self, addr, count, *, tries=None):
        return await super().read(addr, count, tries=tries)

    async def write(self, addr, values, *, function, on_send=None, may_resend=None):
        return await super().write(addr, values, function=function, on_send=on_send, may_resend=may_resend)
