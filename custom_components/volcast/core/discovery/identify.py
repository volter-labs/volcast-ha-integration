"""Wykrywanie, warstwa 3: kandydaci i identyfikacja falownika — wyłącznie odczyt (FC 3).

* Kandydaci: literały adresów z sieci lokalnej (kontrola `check_target`), bez nazw hostów
  i adresów publicznych; kolejność: wpis ręczny, host z wpisu integracji falownika w HA,
  odpowiedź na UDP 48899; bez powtórzeń (adres kanoniczny), najwyżej `MAX_CANDIDATES`.
* Transporty w kolejności: jawna lista kandydata, a bez niej — numer loggera (10 cyfr z UDP 48899)
  → `solarman_v5`; inaczej `goodwe_udp`, potem `modbus_tcp`.
* Dla każdego transportu i każdego profilu, który go obsługuje, czytane są `modbus.identify_reads`
  i dopasowywane regułą profilu (`model_regex` albo `device_type.expect` — nigdy blok seriala
  jako model). Tożsamość bez odcisku (`device_fp`) nie jest tożsamością: bez niej nie da się
  potwierdzić urządzenia przed zapisem.
* Limit ramek na kandydata (`budget`), ramki liczone licznikiem transportu (także ponowienia).
  Transport, który nie dał tożsamości, jest zamykany zawsze (także przy wyjątku); zwrócony
  klient ma otwarty transport — zamyka go wołający.

Numer seryjny urządzenia nigdy nie wychodzi z tego modułu (tylko odcisk z solą instalacji).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Sequence

from ..modbus.blocks import block_key, read_kwargs
from ..modbus.client import RegisterClient
from ..modbus.identity import MIN_SALT_BYTES, device_fingerprint, identity_info
from ..registers import RegisterImage
from ..transports.base import (
    InverterAsleep, LinkDown, ModbusException, RegisterTransport, RequestTimeout, TransportConfig,
    TransportError, check_target)

MAX_CANDIDATES = 4
DEFAULT_BUDGET = 24
_DEFAULT_TRANSPORTS = ("goodwe_udp", "modbus_tcp")
_V5_TRANSPORTS = ("solarman_v5",)

TransportFactory = Callable[[TransportConfig], RegisterTransport]


@dataclass(frozen=True)
class Candidate:
    host: str
    source: str                                # "udp_48899" | "ha_entry" | "manual"
    logger_serial: int | None = None
    transports: tuple[str, ...] = ()           # kolejność prób; puste = reguła domyślna

    def __repr__(self) -> str:                 # adres i numer loggera nie trafiają do logów
        return f"Candidate(source={self.source!r}, transports={self.transports!r})"


@dataclass(frozen=True)
class Identity:
    profile_id: str
    transport: str
    port: int
    unit_id: int
    model: str | None
    rated_power_w: float | None
    # HMAC-SHA256(sól, profil|serial)[:16] (bez seriala: model|moc) — poza repr (logi, diagnostyka)
    device_fp: str = field(repr=False)


def candidates_from(replies: Sequence[Any], ha_hosts: Iterable[str], manual: str | None,
                    *, allow_loopback: bool = False) -> list[Candidate]:
    """Kandydaci sondy: tylko adresy lokalne (`check_target`), bez powtórzeń, najwyżej `MAX_CANDIDATES`."""
    raw: list[tuple[str, str | None, int | None]] = []
    if manual:
        raw.append(("manual", manual, None))
    raw.extend(("ha_entry", h, None) for h in ha_hosts)
    raw.extend(("udp_48899", getattr(r, "ip", None), getattr(r, "logger_serial", None)) for r in replies)
    out: list[Candidate] = []
    index: dict[str, int] = {}
    for source, host, serial in raw:
        try:
            canon = check_target(host, allow_loopback=allow_loopback)
        except ValueError:
            continue
        if canon in index:
            i = index[canon]
            if out[i].logger_serial is None and serial is not None:
                out[i] = replace(out[i], logger_serial=serial)
            continue
        if len(out) >= MAX_CANDIDATES:
            continue
        index[canon] = len(out)
        out.append(Candidate(canon, source, serial))
    return out


def transport_order(candidate: Candidate) -> tuple[str, ...]:
    if candidate.transports:
        return tuple(candidate.transports)
    return _V5_TRANSPORTS if candidate.logger_serial is not None else _DEFAULT_TRANSPORTS


def _groups(kind: str, profiles: Sequence[Any]) -> list[tuple[tuple[int, int, float, float], list[Any]]]:
    """Profile obsługujące transport, pogrupowane po parametrach łącza (jedno połączenie na grupę)."""
    groups: dict[tuple[int, int, float, float], list[Any]] = {}
    for prof in profiles:
        opts = prof.modbus.transport_options.get(kind)
        if opts is None or kind not in prof.raw.get("transports", ()):
            continue
        key = (int(opts["port"]), int(prof.unit_id), opts["timeout_ms"] / 1000.0, opts["gap_ms"] / 1000.0)
        groups.setdefault(key, []).append(prof)
    return list(groups.items())


def requests_of(transport: Any) -> int:
    stats = getattr(transport, "stats", None)
    n = getattr(stats, "requests", 0)
    return n if isinstance(n, int) else 0


async def close_quietly(transport: Any) -> None:
    try:
        await transport.close()
    except Exception:  # noqa: BLE001 — zamknięcie nie może przerwać wykrywania
        pass


def _note(errors: list[str], name: str) -> None:
    if name not in errors:
        errors.append(name)


async def _read_identify(transport, prof, left: Callable[[], int], errors: list[str]):
    """(obraz, stan): stan ∈ ok | fail (to nie ten profil) | dead (łącze nie działa) | budget."""
    blocks: dict[int | tuple[int, int], list[int]] = {}
    read_tries = getattr(getattr(transport, "cfg", None), "read_tries", 1)
    for block in prof.modbus.identify_reads:
        remaining = left()
        if remaining <= 0:
            _note(errors, "budget")
            return None, "budget"
        try:
            blocks[block_key(block)] = await transport.read(block[0], block[1], tries=min(read_tries, remaining),
                                                            **read_kwargs(block))
        except (LinkDown, InverterAsleep) as err:
            _note(errors, type(err).__name__)
            return None, "dead"
        except RequestTimeout as err:
            _note(errors, type(err).__name__)
            return None, ("dead" if err.silent else "fail")
        except (ModbusException, TransportError) as err:
            _note(errors, type(err).__name__)
            return None, "fail"
    return RegisterImage.from_blocks(blocks), "ok"


async def identify_detailed(candidate: Candidate, profiles: Sequence[Any], *, transport_factory: TransportFactory,
                            salt: bytes, budget: int = DEFAULT_BUDGET):
    """Jak `identify`, plus lista nazw błędów (bez treści) — dla raportu sondy."""
    if not isinstance(salt, (bytes, bytearray)) or len(salt) < MIN_SALT_BYTES:
        raise ValueError("identification needs the installation salt")
    errors: list[str] = []
    used = 0
    for kind in transport_order(candidate):
        if kind == "solarman_v5" and candidate.logger_serial is None:
            continue
        for (port, unit, timeout_s, gap_s), profs in _groups(kind, profiles):
            if used >= budget:
                _note(errors, "budget")
                return None, None, used, tuple(errors)
            cfg = TransportConfig(kind=kind, host=candidate.host, port=port, unit=unit, timeout_s=timeout_s,
                                  gap_s=gap_s,
                                  logger_serial=candidate.logger_serial if kind == "solarman_v5" else None)
            try:
                transport = transport_factory(cfg)
            except Exception as err:  # noqa: BLE001 — zła konfiguracja/adres: następny transport
                _note(errors, type(err).__name__)
                continue
            start = requests_of(transport)
            found = None
            try:
                for prof in profs:
                    image, state = await _read_identify(
                        transport, prof, lambda: budget - used - (requests_of(transport) - start), errors)
                    if state == "ok":
                        info = identity_info(prof, image)
                        if not info["matched"]:
                            continue
                        fp = device_fingerprint(salt, prof, image)
                        if fp is None:
                            _note(errors, "identity_unknown")
                            continue
                        found = (Identity(prof.id, kind, port, unit, info["model"], info["rated_power_w"], fp),
                                 prof)
                        break
                    if state in ("dead", "budget"):
                        break
            except Exception as err:  # noqa: BLE001 — sonda nigdy nie rzuca poza błędem soli
                _note(errors, type(err).__name__)
            finally:
                used += max(0, requests_of(transport) - start)
                if found is None:
                    await close_quietly(transport)
            if found is not None:
                ident, prof = found
                return ident, RegisterClient(transport, prof, salt=bytes(salt)), used, tuple(errors)
    return None, None, used, tuple(errors)


async def identify(candidate: Candidate, profiles: Sequence[Any], *, transport_factory: TransportFactory,
                   salt: bytes, budget: int = DEFAULT_BUDGET) -> tuple[Identity | None, RegisterClient | None, int]:
    """(tożsamość, klient z otwartym transportem, liczba wysłanych ramek). Bez tożsamości — (None, None, n)."""
    ident, client, used, _ = await identify_detailed(candidate, profiles, transport_factory=transport_factory,
                                                     salt=salt, budget=budget)
    return ident, client, used
