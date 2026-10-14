"""Raport rozpoznania na żądanie chmury (pole `discovery_request` planu).

Chmura prosi o raport, gdy właściciel zgodził się na niego w arkuszu zgłoszenia. Wysyłamy
OSTATNI raport `DiscoveryRunner` (zbudowany przez `build_report`, z maskowaniem seriali,
MAC-ów i e-maili) — bez nowego przebiegu wykrywania, więc żądanie z chmury nie uruchamia
żadnej sondy sieci. Przed wysyłką drugi przebieg maskuje adresy sieciowe (IPv4, IPv6, hosty
z URL-i, nazwy hostów sieci domowej) w KAŻDYM tekście raportu, także w kluczach.

Jeden `request_id` = najwyżej jedna udana wysyłka: 2xx, 409 (już złożony) i 404 (żądanie nie
nasze / wygasło) kończą sprawę; brak odpowiedzi, 5xx, 401 i 429 = ponowienie przy następnym
pobraniu planu, najwyżej `MAX_ATTEMPTS` prób na id. Wysłane id i liczniki prób są w Store, żeby
przetrwały restart. Treść raportu nigdy nie trafia do logów (tylko status HTTP / nazwa klasy).
Żadnej logiki poza wysyłką.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
import unicodedata
from collections.abc import Callable
from typing import Any

from homeassistant.helpers.storage import Store

from ..const import DISCOVERY_SENT_STORAGE_KEY, DISCOVERY_SENT_STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)

# Limit chmury to 256 KB ciała; raport trzymamy poniżej 200 KB (zapas na kopertę żądania).
MAX_REPORT_BYTES = 200 * 1024
MAX_ATTEMPTS = 3
SENT_MAX = 20
# Granice przebiegu maskującego — głębszy/większy raport dostaje w tym miejscu znacznik
# zamiast danych (nigdy niezamaskowany tekst).
MAX_DEPTH = 32
MAX_NODES = 50_000
_CUT = "<CUT>"
_IP = "<IP>"
_HOST = "<HOST>"

# Sekcje raportu, bez których da się jeszcze rozpoznać falownik — zrzucane od największej,
# gdy raport po maskowaniu jest za duży.
_OPTIONAL_SECTIONS = ("energy_sensors", "price_entities", "network", "errors")
# Klucze, których tekstowa wartość jest adresem urządzenia — maskowana w całości.
_HOST_KEYS = frozenset({"host", "hostname", "ip", "ip_address"})

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_RETRIABLE = frozenset({0, 401, 429})

# URL: schemat + cała część adresowa (dane logowania, host, port) → "<HOST>".
_URL_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]{0,31}://)[^/\s?#]+")
# Ciągi znaków szesnastkowych, dwukropków i kropek (z opcjonalną strefą `%eth0`) — kandydaci
# na IPv6; o masce decyduje `_ipv6_repl` (liczba dwukropków + walidacja), nie regex. Jedna
# klasa znaków bez zagnieżdżonych kwantyfikatorów = koszt liniowy.
_IPV6_RUN_RE = re.compile(r"[0-9A-Fa-f:.]{2,}(?:%[0-9A-Za-z_.\-]{1,32})?")
# IPv4 z zerami wiodącymi; separator kropka, a także `_`/`-` (HA slugifikuje adres w entity_id).
# Bez sprawdzania zakresu oktetów: fałszywe trafienie (np. wersja "1.2.3.4") jest tańsze niż wyciek.
_IPV4_RE = re.compile(r"(?<!\d)0*\d{1,3}(?P<s>[._\-])0*\d{1,3}(?P=s)0*\d{1,3}(?P=s)0*\d{1,3}(?!\d)")
# Nazwy hostów sieci domowej (mDNS, routery) w zwykłym tekście.
_LOCAL_HOST_RE = re.compile(
    r"(?i)\b[a-z0-9](?:[a-z0-9\-]{0,62}\.){1,8}"
    r"(?:local|lan|home|internal|localdomain|fritz\.box|home\.arpa)\b")


def _ipv6_repl(m: re.Match[str]) -> str:
    raw = m.group(0)
    if raw.count(":") < 2:
        return raw
    core = raw.rstrip(".:")
    if core.startswith(":") and not core.startswith("::"):
        core = core[1:]
    tail = raw[len(raw.rstrip(".:")):]
    try:
        ipaddress.IPv6Address(core)
    except ValueError:
        # Zniekształcony adres (np. 9 grup) też maskujemy; czas "12:30:45" i MAC bez "::" zostają.
        if "::" not in core and core.count(":") < 7:
            return raw
    return _IP + tail


def _strip_format(text: str) -> str:
    """Postać do wyszukiwania: NFKC (cyfry/kropki pełnej szerokości → ASCII) bez znaków
    formatujących (zero-width), którymi da się rozciąć adres."""
    norm = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in norm if unicodedata.category(ch) != "Cf")


def _mask_text(text: str) -> str:
    def _apply(s: str) -> str:
        s = _URL_RE.sub(lambda m: m.group(1) + _HOST, s)
        s = _IPV6_RUN_RE.sub(_ipv6_repl, s)
        s = _IPV4_RE.sub(_IP, s)
        return _LOCAL_HOST_RE.sub(_HOST, s)

    masked = _apply(text)
    norm = _strip_format(text)
    if norm == text:
        return masked
    # Tekst z formą zgodności/znakami formatującymi: jeśli postać znormalizowana coś ukrywa,
    # wysyłamy ją zamaskowaną (oryginał mógłby przemycić adres); inaczej oryginał bez zmian.
    masked_norm = _apply(norm)
    return masked_norm if masked_norm != norm else masked


class _Budget:
    def __init__(self) -> None:
        self.left = MAX_NODES


def _mask(value: Any, depth: int, budget: _Budget, host_value: bool = False) -> Any:
    if budget.left <= 0 or depth > MAX_DEPTH:
        return _CUT
    budget.left -= 1
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _HOST if host_value and value else _mask_text(value)
    if isinstance(value, dict):
        out: dict = {}
        for k, v in value.items():
            key = _mask_text(k) if isinstance(k, str) else _mask_text(str(k))
            is_host = isinstance(k, str) and k.lower() in _HOST_KEYS
            out[key] = _mask(v, depth + 1, budget, is_host)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_mask(v, depth + 1, budget, host_value) for v in value]
    return _mask_text(str(value))


def mask_network(value: Any) -> Any:
    """Kopia `value` z zamaskowanymi adresami IPv4/IPv6, hostami URL-i i nazwami hostów sieci
    domowej w każdym tekście na każdej głębokości, także w kluczach słowników; wartości pod
    kluczami `host`/`ip` w całości. Krotki/zbiory → listy (JSON). Głębokość i liczba węzłów
    ograniczone — nadmiar zastępuje znacznik `<CUT>`."""
    return _mask(value, 0, _Budget())


def _size(report: dict) -> int:
    # Tak samo jak serializuje aiohttp (`json.dumps`, ensure_ascii).
    return len(json.dumps(report).encode())


def prepare_report(report: Any) -> dict | None:
    """Raport gotowy do wysyłki: zamaskowany i ≤ `MAX_REPORT_BYTES`. Za duży → zrzucamy
    sekcje opcjonalne od największej (lista w `truncated`); wciąż za duży albo nie-słownik → None
    (nie wysyłamy)."""
    if not isinstance(report, dict):
        return None
    out = mask_network(report)
    dropped: list[str] = []
    while _size(out) > MAX_REPORT_BYTES:
        present = [k for k in _OPTIONAL_SECTIONS if k in out]
        if not present:
            return None
        largest = max(present, key=lambda k: len(json.dumps(out[k])))
        del out[largest]
        dropped.append(largest)
        out["truncated"] = list(dropped)
    return out


def _request_id(block: Any) -> str | None:
    rid = block.get("request_id") if isinstance(block, dict) else None
    return rid.lower() if isinstance(rid, str) and _UUID.match(rid) else None


class DiscoveryReportSender:
    """Wysyłka raportu raz na `request_id` (pamięć w Store, ograniczone ponawianie)."""

    def __init__(self, hass, cloud, report: Callable[[], dict | None], *, store=None) -> None:
        self._cloud = cloud
        self._report = report
        self._store = store if store is not None else Store(
            hass, DISCOVERY_SENT_STORAGE_VERSION, DISCOVERY_SENT_STORAGE_KEY)
        self._sent: list[str] = []
        self._attempts: dict[str, int] = {}
        self._loaded = False
        self._busy = False

    async def async_handle(self, block: Any) -> str:
        """Obsługa pola `discovery_request` z planu. Wynik (do testów i logów debug): "none",
        "busy", "done", "not_ready", "sent", "refused", "too_large", "retry", "gave_up"."""
        rid = _request_id(block)
        if rid is None:
            return "none"
        if self._busy:
            return "busy"
        self._busy = True
        try:
            return await self._handle(rid)
        finally:
            self._busy = False

    async def _handle(self, rid: str) -> str:
        await self._load()
        if rid in self._sent:
            return "done"
        try:
            report = self._report()
        except Exception as err:  # noqa: BLE001 — źródło raportu nie może wywrócić planu
            _LOGGER.debug("Volcast discovery report unavailable (%s)", type(err).__name__)
            return "not_ready"
        if report is None:
            return "not_ready"          # wykrywanie jeszcze nie skończyło — próba nie liczona
        payload = prepare_report(report)
        if payload is None:
            _LOGGER.warning("Volcast discovery report too large to send — request skipped")
            await self._finish(rid)
            return "too_large"
        try:
            status = await self._cloud.async_post_discovery_report(rid, payload)
        except Exception as err:  # noqa: BLE001 — klient nie powinien rzucać; traktujemy jak brak odpowiedzi
            _LOGGER.debug("Volcast discovery report post failed (%s)", type(err).__name__)
            status = 0
        if status not in _RETRIABLE and status < 500:
            if not 200 <= status < 300:
                _LOGGER.debug("Volcast discovery report refused: HTTP %s", status)
            await self._finish(rid)
            return "sent" if 200 <= status < 300 else "refused"
        attempts = self._attempts.pop(rid, 0) + 1
        if attempts >= MAX_ATTEMPTS:
            _LOGGER.warning("Volcast discovery report not delivered after %s attempts (HTTP %s)",
                            attempts, status)
            await self._finish(rid)
            return "gave_up"
        self._attempts[rid] = attempts
        while len(self._attempts) > SENT_MAX:
            self._attempts.pop(next(iter(self._attempts)))
        await self._save()
        return "retry"

    async def _finish(self, rid: str) -> None:
        self._attempts.pop(rid, None)
        self._sent = [r for r in self._sent if r != rid][-(SENT_MAX - 1):] + [rid]
        await self._save()

    async def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            data = await self._store.async_load()
        except Exception as err:  # noqa: BLE001 — bez pamięci działamy w procesie
            _LOGGER.debug("Volcast discovery memory load failed (%s)", type(err).__name__)
            return
        if not isinstance(data, dict):
            return
        sent = data.get("sent")
        if isinstance(sent, list):
            self._sent = [s.lower() for s in sent if isinstance(s, str) and _UUID.match(s)][-SENT_MAX:]
        attempts = data.get("attempts")
        if isinstance(attempts, dict):
            self._attempts = {
                k.lower(): v for k, v in list(attempts.items())[-SENT_MAX:]
                if isinstance(k, str) and _UUID.match(k)
                and isinstance(v, int) and not isinstance(v, bool) and 0 < v < MAX_ATTEMPTS}

    async def _save(self) -> None:
        try:
            await self._store.async_save({"sent": list(self._sent), "attempts": dict(self._attempts)})
        except Exception as err:  # noqa: BLE001 — zapis pamięci nie może wywrócić planu
            _LOGGER.debug("Volcast discovery memory save failed (%s)", type(err).__name__)
