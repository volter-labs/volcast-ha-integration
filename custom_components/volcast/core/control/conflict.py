"""Kolizje klientów tego samego falownika — trybu bezpośredniego nie dzielimy z innym sterownikiem.

Statycznie (`static_conflicts`): włączony wpis konfiguracji integracji falownika (albo integracji
`modbus`) na tym samym adresie co cel, drugi wpis `volcast` z tym samym celem → odmowa. Adresy
wpisów rozwiązuje warstwa HA (nazwy hostów, `data` i `options`); czysta funkcja tu tylko porównuje.
Wpis bez żadnego adresu (`()`) to integracja chmurowa — nigdy nie jest kolizją. Fail-closed: wpis
domeny falownika z adresem, którego nie da się ustalić (nieparsowalny, nierozwiązywalny — `None`),
jest kolizją — „nie wiemy” znaczy „ktoś może tam pisać”.

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

Drugi sterownik (`controllers_from_evidence`, blok `driver.control.conflicts` kontraktu): lista wpisów
`{kind, label, evidence}` wyłącznie z dowodów — kolizja adresu (`entry`), inny klient na łączu
(`lan_client`: zajęte połączenie albo sygnały `ContentionMonitor`), aktywny Box konta (`box`, z planu)
i automatyzacje HA zapisujące zmapowane encje (`automation`, `AutomationWriteTracker`: zapis = wywołanie
usługi z kontekstem przebiegu automatyzacji albo jego dzieckiem, okno 24 h, bufor przebiegów ≤ 200).
Flaga `ems` integracji nie jest dowodem. Kolejność stała (wpisy, łącze, Box, automatyzacje od
największej liczby zapisów), najwyżej `MAX_CONFLICTS` wpisów.

Moduł nie loguje adresów.
"""
from __future__ import annotations

import ipaddress
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

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
    # adresy z `data` + `options` po rozwiązaniu nazw (warstwa HA); None = nie da się ustalić,
    # () = wpis bez adresu (integracja chmurowa).
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
        if e.addresses is None:
            out[e.domain] = None                         # adres nieustalony → fail-closed
            continue
        if not e.addresses:
            continue                                     # wpis bez adresu = integracja chmurowa
        # Kolizja tylko z adresem równym naszemu (cel jest zawsze lokalny, więc i ten adres).
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



# ── drugi sterownik: lista `conflicts` z dowodów ───────────────────────────

AUTOMATION, ENTRY, LAN_CLIENT, BOX = "automation", "entry", "lan_client", "box"
CONFLICT_KINDS = (AUTOMATION, ENTRY, LAN_CLIENT, BOX)
MAX_CONFLICTS, MAX_LABEL, MAX_EVIDENCE = 8, 64, 120
AUTOMATION_WINDOW_S = 24 * 3600.0
AUTOMATION_CONTEXTS_MAX = 200
_WRITES_MAX = 1000                               # na automatyzację; licznik w dowodzie i tak tego nie przekracza
_UNKNOWN_CLASH = "unknown"                       # `async_clash` przy błędzie sprawdzenia (fail-closed)
_CLASH_EVIDENCE = "another entry uses the same inverter address"
_UNKNOWN_EVIDENCE = "conflict check failed"
_LAN_LABEL = "Modbus client"
_LAN_EVIDENCE = {
    "in_use": "the inverter connection is already in use",
    "stray_frames": "frames from another client on the inverter link",
    "peer_resets": "the inverter keeps dropping our connection",
}
_LAN_DEFAULT = "another client on the inverter link"
BOX_LABEL = "Volcast Box"
_BOX_EVIDENCE = "the account has an active Volcast Box"
_AUTOMATION_PREFIX = "automation."


def _entry(kind: str, label: str, evidence: str) -> dict:
    return {"kind": kind, "label": str(label)[:MAX_LABEL], "evidence": str(evidence)[:MAX_EVIDENCE]}


def entry_conflicts(clash: Iterable[str]) -> tuple[dict, ...]:
    """Wpisy `entry` z domen kolizji adresu (każda raz, w kolejności); `unknown` = błąd sprawdzenia."""
    out: list[dict] = []
    for d in clash:
        if isinstance(d, str) and d and all(c["label"] != d[:MAX_LABEL] for c in out):
            out.append(_entry(ENTRY, d, _UNKNOWN_EVIDENCE if d == _UNKNOWN_CLASH else _CLASH_EVIDENCE))
    return tuple(out)


def controllers_from_evidence(automation_writes: Mapping[str, int], address_clashes: Iterable[str],
                              lan_client_seen: str | None, box_active: bool) -> list[dict]:
    """Lista `conflicts` (≤ `MAX_CONFLICTS`, stała kolejność, kształt kontraktu)."""
    out = list(entry_conflicts(address_clashes))
    if isinstance(lan_client_seen, str) and lan_client_seen:
        out.append(_entry(LAN_CLIENT, _LAN_LABEL, _LAN_EVIDENCE.get(lan_client_seen, _LAN_DEFAULT)))
    if box_active is True:
        out.append(_entry(BOX, BOX_LABEL, _BOX_EVIDENCE))
    writes = sorted(((a, n) for a, n in (automation_writes or {}).items()
                     if isinstance(a, str) and a and isinstance(n, int) and not isinstance(n, bool) and n > 0),
                    key=lambda item: (-item[1], item[0]))
    out.extend(_entry(AUTOMATION, a, f"{n} writes in 24 h") for a, n in writes)
    return out[:MAX_CONFLICTS]


def _entity_ids(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, (list, tuple, set, frozenset)):
        return tuple(e for e in raw if isinstance(e, str))
    return ()


class AutomationWriteTracker:
    """Zapisy automatyzacji do obserwowanych encji w oknie `AUTOMATION_WINDOW_S` (zegar monotoniczny).

    `note_trigger` — przebieg automatyzacji (kontekst zdarzenia `automation_triggered`); bufor
    `AUTOMATION_CONTEXTS_MAX` najnowszych. `note_call` — wywołanie usługi: zapis automatyzacji, gdy
    cel jest obserwowany, a kontekst (albo jego rodzic) jest przebiegiem z bufora. Wywołanie bez
    kontekstu automatyzacji (użytkownik, skrypt, nasz zapis) pomijane — to obca zmiana wykonawcy.
    """

    def __init__(self) -> None:
        self._runs: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._writes: dict[str, deque[float]] = {}

    def note_trigger(self, context_id: Any, automation_id: Any, now: float) -> None:
        if not (isinstance(context_id, str) and context_id and isinstance(automation_id, str)
                and automation_id.startswith(_AUTOMATION_PREFIX)):
            return
        self._runs.pop(context_id, None)
        self._runs[context_id] = (now, automation_id)
        while len(self._runs) > AUTOMATION_CONTEXTS_MAX:
            self._runs.popitem(last=False)
        self._prune(now)

    def note_call(self, context_id: Any, parent_id: Any, entity_ids: Any, watched: frozenset[str],
                  now: float) -> str | None:
        """Automatyzacja, której zapis policzono; None = to nie zapis automatyzacji do obserwowanej encji."""
        if not any(e in watched for e in _entity_ids(entity_ids)):
            return None
        self._prune(now)
        run = self._runs.get(context_id) if isinstance(context_id, str) else None
        if run is None and isinstance(parent_id, str):
            run = self._runs.get(parent_id)
        if run is None:
            return None
        automation = run[1]
        self._writes.setdefault(automation, deque(maxlen=_WRITES_MAX)).append(now)
        return automation

    def counts(self, now: float) -> dict[str, int]:
        self._prune(now)
        return {a: len(q) for a, q in self._writes.items() if q}

    def _prune(self, now: float) -> None:
        while self._runs:
            ctx, (t, _) = next(iter(self._runs.items()))
            if now - t <= AUTOMATION_WINDOW_S:
                break
            del self._runs[ctx]
        for a in list(self._writes):
            q = self._writes[a]
            while q and now - q[0] > AUTOMATION_WINDOW_S:
                q.popleft()
            if not q:
                del self._writes[a]
