"""Kolizje klientów tego samego falownika — trybu bezpośredniego nie dzielimy z innym sterownikiem.

Statycznie (`static_conflicts`): włączony wpis konfiguracji integracji falownika (albo integracji
`modbus`) na tym samym adresie co cel, drugi wpis `volcast` z tym samym celem → odmowa. Adresy
wpisów rozwiązuje warstwa HA (nazwy hostów, `data` i `options`); czysta funkcja tu tylko porównuje.
Fail-closed: wpis domeny falownika, którego adresu nie da się ustalić (brak, nieparsowalny,
nierozwiązywalny), jest kolizją — „nie wiemy” znaczy „ktoś może tam pisać”.

W pracy (`ContentionMonitor`, czysta maszyna stanów na licznikach transportu): obce ramki albo
zerwania połączenia przez drugą stronę (przy działających odczytach) w oknie → stan `conflict`.
Ramki protokołu naszego loggera (`stats.unsolicited`, np. heartbeat Solarman V5) NIGDY nie są
sygnałem innego klienta. Skutek kolizji (warstwa wykonawcy): zero nowych zapisów planu, odczyty
rzadziej; powrót do trybu bazowego pozostaje dozwolony. Stan czyści się po `CLEAR_AFTER_S` bez sygnałów.

Rozjazd nastaw (`DriftTracker`) to osobny mechanizm — przejęcie (pauza), nie kolizja: pojedynczy
rozjazd to uzgodnienie, drugi rozjazd tego samego klucza od naszej ostatniej wartości w oknie —
także gdy w międzyczasie uzgodniliśmy go zapisem — to przejęcie (właściciel walczy ze sterowaniem).
Nasz zapis NIE kasuje historii rozjazdów; kasuje ją zmiana wartości planu (`forget`). Liczy się
wyłącznie odczyt rozpoczęty po końcu naszego ostatniego zapisu. Wołający zgłasza rozjazd raz na
odczyt, dopóki trwa.

Moduł nie loguje adresów.
"""
from __future__ import annotations

import ipaddress
from collections import deque
from dataclasses import dataclass
from typing import Iterable, Mapping

from ..discovery.known import INVERTER_DOMAINS
from ..transports.base import TransportStats
from .cycle import same_value

STRAY_LIMIT = 3
RESET_LIMIT = 3
DRIFT_LIMIT = 2
STRAY_WINDOW_S = 600.0
DRIFT_WINDOW_S = 1800.0
CLEAR_AFTER_S = 1800.0

SELF_DOMAIN = "volcast"
INVALID_TARGET = "invalid_target"
# domeny, które mogą pisać do falownika po sieci
CONFLICT_DOMAINS: frozenset[str] = frozenset(INVERTER_DOMAINS) | {"modbus"}


@dataclass(frozen=True)
class EntrySnap:
    domain: str
    # adresy z `data` + `options` po rozwiązaniu nazw (warstwa HA); None = nie da się ustalić.
    # Dla wpisu `volcast`: adres celu połączenia bezpośredniego, () = brak takiego połączenia.
    addresses: tuple[str, ...] | None
    disabled: bool
    is_self: bool = False

    def __repr__(self) -> str:                  # adresy nie trafiają do logów
        return f"EntrySnap(domain={self.domain!r}, disabled={self.disabled!r}, is_self={self.is_self!r})"


def _norm(address: str) -> str | None:
    """Postać porównywalna: literał IP bez strefy IPv6, IPv4 odpakowany z `::ffff:`; None = nie adres."""
    if not isinstance(address, str):
        return None
    try:
        ip = ipaddress.ip_address(address.strip().split("%", 1)[0])
    except ValueError:
        return None
    mapped = getattr(ip, "ipv4_mapped", None)
    return str(mapped if mapped is not None else ip)


def static_conflicts(target_host: str, entries: Iterable[EntrySnap]) -> tuple[str, ...]:
    """Domeny wpisów kolidujących z celem (każda raz, w kolejności wpisów); () = brak kolizji.

    Cel, który nie jest literałem IP, to `("invalid_target",)` — nie da się niczego wykluczyć.
    """
    target = _norm(target_host)
    if target is None:
        return (INVALID_TARGET,)
    out: dict[str, None] = {}
    for e in entries:
        if e.disabled or e.is_self:
            continue
        if e.domain == SELF_DOMAIN:
            if e.addresses is None:
                out[e.domain] = None                     # inny wpis `volcast` z nieznanym celem
            elif any(_norm(a) in (target, None) for a in e.addresses):
                out[e.domain] = None
            continue
        if e.domain not in CONFLICT_DOMAINS:
            continue
        if not e.addresses:
            out[e.domain] = None                         # adres nieustalony → fail-closed
            continue
        normed = [_norm(a) for a in e.addresses]
        if None in normed or target in normed:
            out[e.domain] = None
    return tuple(out)


class ContentionMonitor:
    """Sygnały innego klienta w oknie `STRAY_WINDOW_S` → `conflict`; czyszczenie po `CLEAR_AFTER_S` ciszy."""

    def __init__(self) -> None:
        self.state = "ok"
        self.reason: str | None = None
        self._prev = {"stray": 0, "peer_resets": 0}
        self._events: dict[str, deque[float]] = {"stray": deque(), "peer_resets": deque()}
        self._last_signal: float | None = None

    def note_stats(self, stats: TransportStats, now_mono: float) -> None:
        """Różnice liczników od poprzedniego wywołania. Licznik mniejszy niż poprzednio = nowy
        transport (liczony od zera). `unsolicited` celowo pomijane."""
        for name in ("stray", "peer_resets"):
            cur = getattr(stats, name, 0)
            cur = cur if isinstance(cur, int) and not isinstance(cur, bool) and cur >= 0 else 0
            prev = self._prev[name]
            delta = cur - prev if cur >= prev else cur
            self._prev[name] = cur
            if delta <= 0:
                continue
            if name == "peer_resets" and not self._reads_working(stats, now_mono):
                continue                                 # urządzenie poza siecią, nie drugi klient
            self._events[name].extend([now_mono] * min(delta, max(STRAY_LIMIT, RESET_LIMIT)))
            self._last_signal = now_mono
        self._prune(now_mono)
        if len(self._events["stray"]) >= STRAY_LIMIT:
            self._raise("stray_frames")
        elif len(self._events["peer_resets"]) >= RESET_LIMIT:
            self._raise("peer_resets")
        self.tick(now_mono)

    def tick(self, now_mono: float) -> None:
        if self.state == "conflict" and self._last_signal is not None \
                and now_mono - self._last_signal >= CLEAR_AFTER_S:
            self.state, self.reason = "ok", None
            for q in self._events.values():
                q.clear()

    @staticmethod
    def _reads_working(stats: TransportStats, now_mono: float) -> bool:
        last_ok = getattr(stats, "last_ok_mono", None)
        return isinstance(last_ok, (int, float)) and not isinstance(last_ok, bool) \
            and now_mono - last_ok <= STRAY_WINDOW_S

    def _prune(self, now_mono: float) -> None:
        for q in self._events.values():
            while q and now_mono - q[0] > STRAY_WINDOW_S:
                q.popleft()

    def _raise(self, reason: str) -> None:
        if self.state != "conflict":
            self.state, self.reason = "conflict", reason


class DriftTracker:
    """Rozjazd nastaw na urządzeniu względem naszego ostatniego zapisu: uzgodnienie albo przejęcie."""

    def __init__(self) -> None:
        self._drifts: dict[str, list[float]] = {}
        self._last_write_end: float | None = None

    def note_drift(self, key: str, now_mono: float) -> bool:
        """True = przejęcie (`DRIFT_LIMIT` rozjazdów klucza w `DRIFT_WINDOW_S`); licznik klucza zerowany."""
        hist = [t for t in self._drifts.get(key, ()) if 0.0 <= now_mono - t <= DRIFT_WINDOW_S]
        hist.append(now_mono)
        if len(hist) >= DRIFT_LIMIT:
            self._drifts.pop(key, None)
            return True
        self._drifts[key] = hist
        return False

    def note_own_write(self, key: str, end_mono: float) -> None:
        """Nasz zapis klucza (koniec wymiany): przesuwa barierę `usable`; historii rozjazdów nie kasuje."""
        if self._last_write_end is None or end_mono > self._last_write_end:
            self._last_write_end = end_mono

    def forget(self, key: str) -> None:
        """Wartość planu klucza się zmieniła — poprzednie rozjazdy nie dotyczą nowej wartości."""
        self._drifts.pop(key, None)

    def usable(self, reading_started_mono: float) -> bool:
        """Czy odczyt może świadczyć o rozjeździe — rozpoczęty PO końcu naszego ostatniego zapisu."""
        return self._last_write_end is None or reading_started_mono > self._last_write_end


def drifted_keys(last_written: Mapping[str, float | str], device: Mapping[str, float | str]) -> tuple[str, ...]:
    """Klucze, których wartość na urządzeniu różni się od naszego ostatniego zapisu (kwant rejestru);
    klucze bez odczytu pomijane."""
    out = []
    for key, ours in last_written.items():
        theirs = device.get(key)
        if theirs is None or ours is None:
            continue
        if not same_value(ours, theirs):
            out.append(key)
    return tuple(out)
