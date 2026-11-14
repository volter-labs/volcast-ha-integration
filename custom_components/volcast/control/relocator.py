"""Samoczynne odnajdywanie falownika po utracie łącza bezpośredniego (ten sam egzemplarz pod nowym adresem).

Relocator działa per wpis, tylko w trybie bezpośrednim (nie w próbie):

* Wyzwalacz: brak udanego odczytu przez `RELOCATE_AFTER_S` (licząc od startu Relocatora, gdy odczytu
  nie było wcale) albo nieudany start połączenia (`START_FAILED`). Inne odmowy (kolizja, host zajęty,
  zły cel) to nie martwe łącze — wtedy nic. Sprawdzenie co `CHECK_INTERVAL_S` (własny timer; połączenie
  nie ma sygnału „martwe”).
* Harmonogram: pierwsze wyszukiwanie od razu, kolejne po 5, 15 i dalej co 60 min (`SCHEDULE_S`, od końca
  poprzedniego). Udany odczyt (odczyt młodszy niż `RELOCATE_AFTER_S`) zeruje harmonogram. Stan
  harmonogramu i liczniki leżą w `hass.data` poza runtime — przeżywają przeładowanie po zapisie opcji.
* Jedno wyszukiwanie naraz na wpis: zadanie wpisu + blokada wspólna z ręcznym wyszukiwaniem w opcjach
  i onboardingu (`search_lock`). Zajęta blokada = pominięcie (następny takt), bez liczenia próby.
  Nic nie robi w trakcie onboardingu ani po zatrzymaniu (rozładunek wpisu anuluje też trwające wyszukiwanie).
* Kandydaci: rozgłoszenie UDP 48899 (transporty z donglem) + skan lokalnych /24 (`sweep`: unicast 48899
  albo TCP connect na porcie celu), bez adresów HA. Więcej niż `MAX_CANDIDATES` = wynik niepełny
  (`discover` obciąłby listę) — nic nie zmieniamy (`not_found`). Identyfikacja istniejącym
  `ds.async_search` (profil celu, port i unit celu jak w ręcznym wyszukiwaniu), decyzja `decide_relocation`
  (rodzaj odcisku z profilu w chwili decyzji), kolizje dla KAŻDEGO kandydata (`ds.async_clash`).
* `relocated`: zapis opcji zmienia WYŁĄCZNIE `host` (i `logger_serial` dla `solarman_v5`) celu;
  przeładowanie robi istniejący nasłuch opcji (sam adres tego samego egzemplarza nie jest zmianą sterowania).
  Bez zapisu, gdy cel zmienił się w trakcie albo gdy zmiana liczyłaby się jako zmiana sterowania.

Wyłącznie odczyt (bez zgody na sterowanie). Adres falownika ani kandydatów nigdy nie trafia do logów
ani diagnostyki — tylko wynik, liczba kandydatów i czas.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import math
import time
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

import homeassistant.util.dt as dt_util
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_time_interval

from ..const import OPT_DIRECT_TARGET
from ..core.discovery.identify import MAX_CANDIDATES, Candidate
from ..core.discovery.network import probe_udp_48899
from ..core.discovery.relocate import FP_SERIAL, Relocation, decide_relocation, fingerprint_kind
from ..core.discovery.sweep import local_subnets, sweep
from . import direct_search as ds
from .direct import START_FAILED, serial_identity

_LOGGER = logging.getLogger(__name__)

RELOCATE_AFTER_S = 180.0
CHECK_INTERVAL_S = 30.0
SCHEDULE_S = (0.0, 300.0, 900.0, 3600.0)        # odstęp przed próbą n (ostatni powtarzany)
TCP_CONNECT_S = 0.5
_UDP_TRANSPORTS = frozenset({"goodwe_udp", "solarman_v5"})
_V5 = "solarman_v5"

STATE_KEY = "volcast_relocation"                  # entry_id → RelocationState (przeżywa przeładowanie)
SEARCH_LOCKS_KEY = "volcast_search_locks"         # entry_id → asyncio.Lock (ręczne i samoczynne wyszukiwanie)

# Wyniki poza `decide_relocation`: cel zmieniony w trakcie wyszukiwania; zapis byłby zmianą sterowania.
TARGET_CHANGED, BLOCKED = "target_changed", "blocked"


def search_lock(hass, entry_id: str) -> asyncio.Lock:
    """Blokada wyszukiwania falownika wpisu — wspólna dla opcji, onboardingu i Relocatora."""
    locks = hass.data.setdefault(SEARCH_LOCKS_KEY, {})
    lock = locks.get(entry_id)
    if lock is None:
        lock = locks[entry_id] = asyncio.Lock()
    return lock


@dataclass
class RelocationState:
    """Harmonogram i liczniki wpisu (bez adresów)."""
    attempts: int = 0                   # wyszukiwania w bieżącym epizodzie martwego łącza
    next_due: float | None = None       # zegar monotoniczny; None = od razu po wyzwoleniu
    relocations: int = 0
    last_outcome: str | None = None
    last_at: str | None = None          # ISO UTC
    last_candidates: int | None = None
    last_truncated: bool = False

    def reset(self) -> None:
        self.attempts, self.next_due = 0, None

    def summary(self) -> dict:
        return {"relocations": self.relocations, "last_relocate_outcome": self.last_outcome,
                "last_relocate_at": self.last_at, "attempts": self.attempts,
                "last_relocate_candidates": self.last_candidates, "last_relocate_truncated": self.last_truncated}


def relocation_state(hass, entry_id: str) -> RelocationState:
    states = hass.data.setdefault(STATE_KEY, {})
    st = states.get(entry_id)
    if st is None:
        st = states[entry_id] = RelocationState()
    return st


def relocation_summary(hass, entry_id: str) -> dict | None:
    """Liczniki do diagnostyki; None = Relocator jeszcze nic nie robił w tym procesie."""
    st = (getattr(hass, "data", None) or {}).get(STATE_KEY, {}).get(entry_id)
    return st.summary() if st is not None else None


def forget(hass, entry_id: str) -> None:
    """Usunięcie wpisu: stan i blokada znikają."""
    for key in (STATE_KEY, SEARCH_LOCKS_KEY):
        (getattr(hass, "data", None) or {}).get(key, {}).pop(entry_id, None)


def interval_after(attempts: int) -> float:
    return SCHEDULE_S[min(max(attempts, 0), len(SCHEDULE_S) - 1)]


def _ipv4(host: Any) -> str | None:
    """Postać kanoniczna literału IPv4 (z odpakowaniem `::ffff:`); inne — None. Polityka adresów
    (tylko sieć lokalna) zostaje w warstwie transportu, która i tak odrzuca resztę przed pierwszą ramką."""
    if not isinstance(host, str) or not host:
        return None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    ip = getattr(ip, "ipv4_mapped", None) or ip
    return str(ip) if ip.version == 4 else None


def own_addresses(adapters: Iterable[Mapping[str, Any]]) -> set[str]:
    """Adresy IPv4 HA ze wszystkich adapterów — skan ich nie sonduje."""
    out: set[str] = set()
    for adapter in adapters or ():
        for entry in (adapter.get("ipv4") or ()) if isinstance(adapter, Mapping) else ():
            host = _ipv4(entry.get("address")) if isinstance(entry, Mapping) else None
            if host:
                out.add(host)
    return out


def ordered_subnets(subnets: Sequence[ipaddress.IPv4Network], old_host: str) -> list[ipaddress.IPv4Network]:
    """/24 starego adresu najpierw (najbardziej prawdopodobne miejsce), reszta w kolejności wejścia."""
    try:
        old = ipaddress.IPv4Address(old_host)
    except ValueError:
        return list(subnets)
    return sorted(subnets, key=lambda n: old not in n)


def merge_candidates(replies: Iterable[Any], swept: Iterable[Candidate], transport: str, *,
                     skip: Iterable[str] = ()) -> tuple[list[Candidate], bool]:
    """Kandydaci z rozgłoszenia (najpierw) i skanu: bez powtórzeń (adres kanoniczny), bez adresów HA,
    transport celu. (kandydaci, obcięte?) — obcięte, gdy unikalnych adresów jest więcej niż `MAX_CANDIDATES`
    (`discover` wziąłby tylko pierwsze, więc brak trafienia nic by nie znaczył)."""
    skipped = {h for h in (_ipv4(s) for s in skip) if h}
    found: dict[str, int | None] = {}
    raw = [(getattr(r, "ip", None), getattr(r, "logger_serial", None)) for r in replies or ()]
    raw += [(c.host, c.logger_serial) for c in swept or ()]
    for host, serial in raw:
        canon = _ipv4(host)
        if canon is None or canon in skipped:
            continue
        if found.get(canon) is None:
            found[canon] = serial if isinstance(serial, int) and not isinstance(serial, bool) else None
    out = [Candidate(h, "sweep", s if transport == _V5 else None, (transport,)) for h, s in found.items()]
    return out, len(out) > MAX_CANDIDATES


async def tcp_connect(host: str, port: int, timeout: float = TCP_CONNECT_S) -> bool:
    """Czy port TCP przyjmuje połączenie (bez żadnej ramki); gniazdo zamykane zawsze."""
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, asyncio.TimeoutError, ValueError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:  # noqa: BLE001 — połączenie i tak zamknięte
        pass
    return True


async def ha_adapters(hass) -> list:
    """Adaptery sieci HA (`homeassistant.components.network`); brak = pusta lista (skan tylko /24 celu)."""
    try:
        from homeassistant.components import network
        return list(await network.async_get_adapters(hass) or ())
    except Exception as err:  # noqa: BLE001 — bez adapterów skan obejmuje tylko /24 starego adresu
        _LOGGER.debug("Volcast direct relocation: network adapters unavailable (%s)", type(err).__name__)
        return []


def search_factory(port: int | None, unit_id: int | None) -> Callable | None:
    """Fabryka transportu z portem i unitem celu (jak ręczne wyszukiwanie) — raport ma wtedy ten sam
    port co cel, inaczej `decide_relocation` uznałby ten sam egzemplarz za obcy."""
    if port is None and unit_id is None:
        return None

    def factory(cfg):
        return ds.make_transport(replace(cfg, port=port if port is not None else cfg.port,
                                         unit=unit_id if unit_id is not None else cfg.unit),
                                 allow_loopback=ds.ALLOW_LOOPBACK)
    return factory


class Relocator:
    """Wyszukiwanie celu bezpośredniego pod nowym adresem; patrz opis modułu."""

    def __init__(self, hass, entry, conn, *, control_change: Callable[[Mapping, Mapping], bool],
                 onboarding_active: Callable[[], bool], search: Callable[..., Awaitable] | None = None,
                 clash: Callable[[str], Awaitable] | None = None,
                 adapters: Callable[[], Awaitable] | None = None,
                 udp_probe: Callable[..., Awaitable] = probe_udp_48899,
                 connect: Callable[..., Awaitable] = tcp_connect, sweep_fn: Callable[..., Awaitable] = sweep,
                 clock: Callable[[], float] = time.monotonic, utcnow=dt_util.utcnow,
                 check_s: float = CHECK_INTERVAL_S) -> None:
        self._hass, self._entry, self._conn = hass, entry, conn
        self._control_change = control_change
        self._onboarding_active = onboarding_active
        self._search = search or self._default_search
        self._clash = clash or (lambda host: ds.async_clash(hass, entry.entry_id, host))
        self._adapters = adapters or (lambda: ha_adapters(hass))
        self._udp_probe, self._connect, self._sweep = udp_probe, connect, sweep_fn
        self._clock, self._utcnow, self._check_s = clock, utcnow, check_s
        self._started_at = clock()
        self._stopped = False
        self._task: asyncio.Task | None = None
        self._unsub_timer: Callable[[], None] | None = None

    @property
    def state(self) -> RelocationState:
        return relocation_state(self._hass, self._entry.entry_id)

    @property
    def task(self) -> asyncio.Task | None:
        return self._task

    def start(self) -> Callable[[], None]:
        """Timer sprawdzania; zwraca zdjęcie (do `rt.unsubs`) = `stop`."""
        self._unsub_timer = async_track_time_interval(self._hass, self.check, timedelta(seconds=self._check_s))
        return self.stop

    def stop(self) -> None:
        """Rozładunek wpisu: koniec timera i trwającego wyszukiwania (bez zapisu opcji po tym punkcie)."""
        self._stopped = True
        if self._unsub_timer is not None:
            try:
                self._unsub_timer()
            except Exception:  # noqa: BLE001 — rozładunek nie może się wywrócić
                pass
            self._unsub_timer = None
        if self._task is not None and not self._task.done():
            self._task.cancel()

    # ── wyzwalacz ──

    def _dead(self, now: float) -> bool | None:
        """True = martwe łącze; False = żywe (udany odczyt); None = jeszcze nie wiadomo / nie nasza sprawa."""
        conn = self._conn
        refused = conn.refused()
        if refused == START_FAILED:
            return True
        if refused is not None:
            return None                  # kolizja, host zajęty, zły cel — nie szukamy innego adresu
        age = conn.age_s()
        if math.isfinite(age) and age < RELOCATE_AFTER_S:
            return False
        silent = age if math.isfinite(age) else now - self._started_at
        return True if silent >= RELOCATE_AFTER_S else None

    @callback
    def check(self, _now=None) -> None:
        if self._stopped or (self._task is not None and not self._task.done()):
            return
        now = self._clock()
        try:
            dead = self._dead(now)
        except Exception as err:  # noqa: BLE001 — sprawdzenie nigdy nie psuje wpisu
            _LOGGER.debug("Volcast direct relocation: link check failed (%s)", type(err).__name__)
            return
        st = self.state
        if dead is False:
            st.reset()
            return
        if not dead or (st.next_due is not None and now < st.next_due):
            return
        if self._onboarding_active():
            return
        self._task = self._entry.async_create_background_task(self._hass, self._async_run(),
                                                              "volcast_direct_relocate")

    # ── przebieg ──

    async def _async_run(self) -> None:
        lock = search_lock(self._hass, self._entry.entry_id)
        if lock.locked():
            return                       # ręczne wyszukiwanie trwa — następny takt
        st = self.state
        async with lock:
            if self._stopped:
                return
            st.attempts += 1
            st.next_due = self._clock() + interval_after(st.attempts)   # także gdy przebieg zostanie przerwany
            try:
                decision, target, count, truncated = await self._async_search()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 — wyszukiwanie nigdy nie psuje wpisu
                _LOGGER.warning("Volcast direct relocation: search failed (%s)", type(err).__name__)
                decision, target, count, truncated = Relocation("not_found"), None, None, False
        st.next_due = self._clock() + interval_after(st.attempts)
        st.last_candidates, st.last_truncated = count, truncated
        outcome = decision.outcome
        if outcome == "relocated" and not self._stopped:
            outcome = self._apply(decision, target)
        st.last_outcome, st.last_at = outcome, self._utcnow().isoformat()
        _LOGGER.debug("Volcast direct relocation: %s", outcome)

    def _target(self) -> dict | None:
        current = self._hass.config_entries.async_get_entry(self._entry.entry_id) or self._entry
        target = (current.options or {}).get(OPT_DIRECT_TARGET)
        return dict(target) if isinstance(target, Mapping) else None

    async def _async_search(self) -> tuple[Relocation, dict | None, int | None, bool]:
        target = self._target()
        profile = self._conn.profile
        # Rodzaj odcisku z profilu W CHWILI DECYZJI (cel z sondy go nie zapisuje); słaby odcisk = bez sondy.
        if target is None or serial_identity(target, profile) is None or fingerprint_kind(profile) != FP_SERIAL:
            return Relocation("not_found"), target, None, False
        transport, port, unit = target.get("transport"), target.get("port"), target.get("unit_id")
        old_host = str(target.get("host") or "")
        replies: list = []
        if transport in _UDP_TRANSPORTS:
            result = await self._udp_probe()
            replies = list(getattr(result, "replies", None) or ())
        adapters = await self._adapters()
        skip = own_addresses(adapters)
        subnets = ordered_subnets(local_subnets(adapters, old_host), old_host)
        swept = await self._sweep(subnets, transport=transport, port=port, connect=self._connect,
                                  udp_probe=self._udp_probe, skip=sorted(skip))
        candidates, truncated = merge_candidates(replies, swept, transport, skip=skip)
        if truncated or not candidates:
            return Relocation("not_found"), target, len(candidates), truncated
        reports = await self._search(candidates, port=port, unit_id=unit)
        clashes = {c.host: tuple(await self._clash(c.host)) for c in candidates}
        decision = decide_relocation({**target, "fp_kind": FP_SERIAL}, reports, clashes=clashes)
        return decision, target, len(candidates), False

    async def _default_search(self, candidates: Sequence[Candidate], *, port, unit_id) -> list:
        return await ds.async_search(self._hass, self._entry, [self._conn.profile], candidates=candidates,
                                     transport_factory=search_factory(port, unit_id))

    def _apply(self, decision: Relocation, searched: dict | None) -> str:
        """Zapis nowego adresu (synchronicznie, bez `await` między sprawdzeniem a zapisem)."""
        current = self._hass.config_entries.async_get_entry(self._entry.entry_id) or self._entry
        old = dict(current.options or {})
        target = old.get(OPT_DIRECT_TARGET)
        if searched is None or not isinstance(target, Mapping) or dict(target) != searched:
            return TARGET_CHANGED
        new_target = {**dict(target), "host": decision.host}
        if target.get("transport") == _V5 and decision.logger_serial is not None:
            new_target["logger_serial"] = decision.logger_serial
        new = {**old, OPT_DIRECT_TARGET: new_target}
        if self._control_change(old, new):
            return BLOCKED
        self.state.relocations += 1
        _LOGGER.warning("Volcast direct control: the inverter answered at a new local address — reconnecting")
        self._hass.config_entries.async_update_entry(current, options=new)
        return "relocated"
