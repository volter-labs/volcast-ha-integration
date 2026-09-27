"""Wykrywanie, warstwa 4: próba możliwości zidentyfikowanego falownika — wyłącznie odczyt (FC 3).

Każdy klucz `modbus.probe_keys` profilu jest czytany ze swojego rejestru (klucz `tou` — cały blok
harmonogramu: programy i włącznik). Wynik klucza:

* odczyt udany → `capabilities[klucz] = True`;
* wyjątek 2 → `capabilities[klucz] = False` (rejestru nie ma; wykonawca: `memory.unsupported`);
* odczyt nieudany we WSZYSTKICH `read_tries` próbach, z resetem kanału między próbami (ramka
  złej długości, zniekształcona, inny wyjątek) → rejestr istnieje, ale nie da się go odczytać:
  `echo_only` (wykonawca: `unreadable` dla klienta, pisarza i celu + `memory.unsupported`).
  Jedna zgubiona albo zła ramka, po której następna próba się udaje, nie wystarcza;
* zerwanie łącza, uśpiony falownik, cisza albo wyczerpany limit ramek → klucz bez werdyktu
  (nie ma go ani w `capabilities`, ani w `echo_only`), dalsza próba przerwana.

`direct_available`: dla profilu trybu i nastawy — `mode` i `power_w` czytelne i obsługiwane;
dla profilu okien czasowych — blok harmonogramu czytelny, a kod `device_type` urządzenia na
liście profilu (profil opisuje mapę trójfazową; inny kod = inna mapa, harmonogram nieobsługiwany).

Raport nie niesie adresu, numeru seryjnego, numeru loggera ani odcisku urządzenia w `to_dict()`
i `repr()`; kandydat (adres) jest dołączony wyłącznie dla wołającego, który z niego składa cel.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Sequence

from ..modbus.blocks import plan_blocks, spec_addresses
from ..modbus.client import RegisterClient
from ..modbus.identity import MIN_SALT_BYTES
from ..transports.base import InverterAsleep, LinkDown, ModbusException, RequestTimeout, TransportError
from .identify import (
    DEFAULT_BUDGET, MAX_CANDIDATES, Candidate, Identity, TransportFactory, close_quietly, identify_detailed,
    requests_of)

_TOU_FIELDS = ("start", "power_w", "soc", "grid_charge")
_GROUP = ("mode", "power_w")
_OK, _UNSUPPORTED, _UNREADABLE, _ABORT = "ok", "unsupported", "unreadable", "abort"


@dataclass(frozen=True)
class ProbeReport:
    identity: Identity | None
    capabilities: dict[str, bool]              # klucz → czy rejestr istnieje (wyjątek 2 = False)
    echo_only: tuple[str, ...]                 # istnieje, ale nieczytelny we wszystkich próbach
    direct_available: bool
    tou_readable: bool | None                  # None = profil bez harmonogramu
    modbus_status: str | None
    requests: int
    errors: tuple[str, ...]                    # nazwy klas błędów (i znaczniki), bez treści
    candidate: Candidate | None = field(default=None, repr=False, compare=False)

    @property
    def unreadable(self) -> frozenset[str]:
        """Klucze bez odczytu zwrotnego — zbiór `unreadable` klienta, pisarza i celu rejestrów."""
        return frozenset(self.echo_only)

    def to_dict(self) -> dict:
        ident = None
        if self.identity is not None:
            i = self.identity
            ident = {"profile_id": i.profile_id, "transport": i.transport, "port": i.port,
                     "unit_id": i.unit_id, "model": i.model, "rated_power_w": i.rated_power_w}
        return {"identity": ident, "capabilities": dict(self.capabilities), "echo_only": list(self.echo_only),
                "direct_available": self.direct_available, "tou_readable": self.tou_readable,
                "modbus_status": self.modbus_status, "requests": self.requests, "errors": list(self.errors)}


def _empty(*, requests: int = 0, errors: Iterable[str] = (), modbus_status: str | None = None,
           candidate: Candidate | None = None) -> ProbeReport:
    return ProbeReport(None, {}, (), False, None, modbus_status, requests, tuple(dict.fromkeys(errors)),
                       candidate)


def _key_blocks(profile, key: str) -> list[tuple[int, int]]:
    """Bloki odczytu klucza sondy; puste = profil nie zna rejestru klucza."""
    write = profile.raw.get("write", {})
    if key == "tou":
        tp = write.get("tou_program")
        if tp is None:
            return []
        addrs = [tp[f]["addr"] + i for f in _TOU_FIELDS if f in tp for i in range(tp["count"])]
        if "tou_enable" in write:
            addrs.append(write["tou_enable"]["addr"])
        return plan_blocks(addrs, profile.modbus.max_read_registers)
    spec = write.get(key) or profile.raw.get("read", {}).get(key)
    if not isinstance(spec, dict) or "addr" not in spec:
        return []
    return plan_blocks(spec_addresses(spec), profile.modbus.max_read_registers)


def _tou_device_ok(profile, identity: Identity) -> bool:
    """Harmonogram tylko dla kodu `device_type` z listy profilu (mapa, którą profil opisuje)."""
    spec = (profile.raw.get("identify", {}).get("registers", {}) or {}).get("device_type")
    if spec is None:
        return True
    try:
        code = int(identity.model) if identity.model is not None else None
    except ValueError:
        return False
    return code is not None and code in spec.get("expect", ())


class _Prober:
    def __init__(self, client: RegisterClient, budget: int) -> None:
        self.client = client
        self.transport = client.transport
        self.start = requests_of(self.transport)
        self.budget = budget
        self.errors: list[str] = []
        self.read_tries = max(1, int(getattr(getattr(self.transport, "cfg", None), "read_tries", 1)))

    def used(self) -> int:
        return max(0, requests_of(self.transport) - self.start)

    def note(self, name: str) -> None:
        if name not in self.errors:
            self.errors.append(name)

    async def block(self, addr: int, count: int) -> str:
        """Jeden blok: wszystkie `read_tries` próby, reset kanału między nimi."""
        left = self.read_tries
        while left > 0:
            remaining = self.budget - self.used()
            if remaining <= 0:
                self.note("budget")
                return _ABORT
            before = requests_of(self.transport)
            try:
                await self.client.read_block(addr, count, tries=min(left, remaining))
                return _OK
            except ModbusException as err:
                self.note(type(err).__name__)
                if err.code == 2:
                    return _UNSUPPORTED
            except (LinkDown, InverterAsleep) as err:
                self.note(type(err).__name__)
                return _ABORT
            except RequestTimeout as err:
                self.note(type(err).__name__)
                if err.silent:
                    return _ABORT                  # urządzenie zamilkło — to nie werdykt o rejestrze
            except TransportError as err:
                self.note(type(err).__name__)
            left -= max(1, requests_of(self.transport) - before)
            if left > 0:
                await self.transport.reset_channel()
        return _UNREADABLE

    async def key(self, blocks: Sequence[tuple[int, int]]) -> str:
        worst = _OK
        for addr, count in blocks:
            out = await self.block(addr, count)
            if out == _ABORT:
                return _ABORT
            if out == _UNSUPPORTED:
                worst = _UNSUPPORTED
            elif out == _UNREADABLE and worst == _OK:
                worst = _UNREADABLE
        return worst


async def probe(client: RegisterClient, profile, identity: Identity, *, budget: int) -> ProbeReport:
    """Próba możliwości: tylko FC 3, najwyżej `budget` ramek. Błąd transportu nie wychodzi na zewnątrz
    (przerywa próbę); inne wyjątki — tak (wołający zamyka transport)."""
    p = _Prober(client, budget)
    caps: dict[str, bool] = {}
    echo_only: list[str] = []
    tou_readable: bool | None = None
    has_tou = profile.control_model == "time_window" or "tou" in profile.modbus.probe_keys
    for key in profile.modbus.probe_keys:
        if key == "tou" and not _tou_device_ok(profile, identity):
            caps["tou"], tou_readable = False, False
            continue
        blocks = _key_blocks(profile, key)
        if not blocks:
            caps[key] = False
            p.note("unknown_key")
            continue
        out = await p.key(blocks)
        if out == _ABORT:
            break
        caps[key] = out != _UNSUPPORTED
        if out == _UNREADABLE:
            echo_only.append(key)
        if key == "tou":
            tou_readable = out == _OK
    if has_tou:
        available = tou_readable is True           # None = próba przerwana przed harmonogramem
    else:
        available = all(caps.get(k) is True and k not in echo_only for k in _GROUP)
    return ProbeReport(identity, caps, tuple(echo_only), available, tou_readable, profile.modbus.status,
                       p.used(), tuple(p.errors))


async def discover(candidates: Sequence[Candidate], profiles: Sequence[Any], *, transport_factory: TransportFactory,
                   conflicts: Callable[[str], tuple[str, ...]], salt: bytes,
                   budget: int = DEFAULT_BUDGET) -> list[ProbeReport]:
    """Raport na kandydata (ta sama kolejność, najwyżej `MAX_CANDIDATES`). Kandydat z kolizją
    statyczną (albo gdy sprawdzenia kolizji nie da się wykonać) jest pomijany bez żadnej ramki —
    błąd `conflict`. Każdy transport jest zamykany po próbie, także przy wyjątku; nic nie rzuca
    poza złą solą (błąd programisty)."""
    if not isinstance(salt, (bytes, bytearray)) or len(salt) < MIN_SALT_BYTES:
        raise ValueError("discovery needs the installation salt")
    reports: list[ProbeReport] = []
    for cand in list(candidates)[:MAX_CANDIDATES]:
        try:
            clash = tuple(conflicts(cand.host))
        except Exception:  # noqa: BLE001 — „nie wiemy” = kolizja (fail-closed)
            clash = ("unknown",)
        if clash:
            reports.append(_empty(errors=("conflict",), candidate=cand))
            continue
        client = None
        used = 0
        probe_start: int | None = None
        errors: tuple[str, ...] = ()
        try:
            ident, client, used, errors = await identify_detailed(
                cand, profiles, transport_factory=transport_factory, salt=salt, budget=budget)
            if ident is None or client is None:
                reports.append(_empty(requests=used, errors=errors, candidate=cand))
                continue
            probe_start = requests_of(client.transport)
            rep = await probe(client, client.profile, ident, budget=budget - used)
            reports.append(replace(rep, requests=used + rep.requests,
                                   errors=tuple(dict.fromkeys((*errors, *rep.errors))), candidate=cand))
        except Exception as err:  # noqa: BLE001 — awaria sondy nie psuje onboardingu
            spent = used
            if client is not None and probe_start is not None:
                spent += max(0, requests_of(client.transport) - probe_start)
            reports.append(_empty(requests=spent, errors=(*errors, type(err).__name__), candidate=cand))
        finally:
            if client is not None:
                await close_quietly(client.transport)
    return reports
