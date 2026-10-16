"""Raport rozpoznania na żądanie chmury (pole `discovery_request` planu).

Chmura prosi o raport, gdy właściciel zgodził się na niego w arkuszu zgłoszenia. Wysyłamy
OSTATNI raport `DiscoveryRunner` (zbudowany przez `build_report`, z maskowaniem seriali,
MAC-ów i e-maili) — bez nowego przebiegu wykrywania, więc żądanie z chmury nie uruchamia
żadnej sondy sieci. Raport z nieudanego przebiegu (timeout / błąd runnera) nie jest wysyłany.

Przed wysyłką drugi przebieg maskuje w KAŻDYM tekście raportu (także w kluczach, po NFKC i bez
znaków formatujących): adresy IPv4 (też z zerami wiodącymi i w postaci slugu `192_168_1_10`),
IPv6 (strefy, adresy mapowane), hosty URL-i, nazwy hostów sieci domowej i DDNS, MAC-i (z
separatorami `:`/`-`/`_`/`.`, zwarte 12 cyfr szesnastkowych z literą, opisane „mac”) i e-maile.
Tytuł wpisu integracji (często login konta) i stany encji o nazwie wskazującej na dane konta
lub sieci (SSID, login, e-mail, token, lokalizacja…) idą jako `***`. Bez gwarancji dla
egzotycznych form IPv4 (szesnastkowe oktety, liczba całkowita, kropka ideograficzna).

Jeden `request_id` = najwyżej jedna udana wysyłka: 2xx, 409 (już złożony), 404 (żądanie nie
nasze / wygasło) i inne 4xx kończą sprawę; brak odpowiedzi, 5xx, 401 i 429 = ponowienie przy
następnym pobraniu planu, najwyżej `MAX_ATTEMPTS` prób na id. Próba jest liczona i zapisana
PRZED maskowaniem i wysyłką — przerwana wysyłka (anulowanie przy rozładunku, limit czasu) też się liczy.
Twardy limit czasu obejmuje maskowanie i POST. Wysłane id i liczniki prób są w Store, żeby przetrwały restart. Treść raportu nigdy nie trafia
do logów (tylko status HTTP / nazwa klasy). Żadnej logiki poza wysyłką.
"""
from __future__ import annotations

import asyncio
import functools
import ipaddress
import json
import logging
import re
import unicodedata
from collections.abc import Callable
from typing import Any

from homeassistant.helpers.storage import Store

from ..const import DISCOVERY_SENT_STORAGE_KEY, DISCOVERY_SENT_STORAGE_VERSION
from ..core.discovery.report import _EMAIL_RE, ERROR_RUNNER_PREFIX, ERROR_TIMEOUT

_LOGGER = logging.getLogger(__name__)

# Limit chmury to 256 KB ciała; raport trzymamy poniżej 200 KB (zapas na kopertę żądania).
MAX_REPORT_BYTES = 200 * 1024
MAX_ATTEMPTS = 3
SENT_MAX = 20
# Twardy limit całej wysyłki w tle (maskowanie + POST); po nim próba przepada (już policzona).
SEND_TIMEOUT_S = 15.0
# Granice przebiegu maskującego — głębszy/większy raport dostaje w tym miejscu znacznik
# zamiast danych (nigdy niezamaskowany tekst).
MAX_DEPTH = 32
MAX_NODES = 50_000
_CUT = "<CUT>"
_IP = "<IP>"
_HOST = "<HOST>"
_HIDDEN = "***"

# Sekcje raportu, bez których da się jeszcze rozpoznać falownik — zrzucane od największej,
# gdy raport po maskowaniu jest za duży; potem przycinane listy encji falowników.
_OPTIONAL_SECTIONS = ("energy_sensors", "price_entities", "network", "errors")
_ENTITIES_CUT = "inverters.entities"
# Klucze, których tekstowa wartość jest adresem urządzenia — maskowana w całości.
_HOST_KEYS = frozenset({"host", "hostname", "ip", "ip_address", "ip_addr", "address", "url"})
_HOST_SUFFIXES = ("_host", "_ip")
# Encja, której stan bywa daną konta lub sieci — stan (i opcje) idą jako `***`.
_SENSITIVE_RE = re.compile(
    r"(?i)ssid|wi[ _\-]?fi|wlan|network[ _\-]?name|host[ _\-]?name|user[ _\-]?name|account|login|"
    r"e[ _\-]?mail|passw|token|api[ _\-]?key|location|address|latitude|longitude|gps|owner|phone")
# Nazwa elektrowni/stacji w chmurach falowników bywa nazwiskiem lub adresem — ale „plant”/„station”
# niosą też pomiary (`plant_current_power`): ukrywamy tylko stan nieliczbowy.
_NAME_LIKE_RE = re.compile(r"(?i)plant|station")
_NUMBER_RE = re.compile(r"[+\-]?\d+(?:[.,]\d+)?(?:[eE][+\-]?\d+)?")
_ENTITY_NAME_KEYS = ("entity_id", "unique_id", "original_name", "translation_key", "friendly_name", "name")

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_RETRIABLE = frozenset({0, 401, 429})

# URL: schemat + cała część adresowa (dane logowania, host, port) → "<HOST>".
_URL_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]{0,31}://)[^/\s?#]+")
# MAC z jednolitym separatorem (`:`, `-`, `_` — slug HA, `.`), granice: nie litera/cyfra
# (podkreślnik jest granicą — `sensor.aa_bb_cc_dd_ee_ff_rssi`).
_MAC_SEP_RE = re.compile(
    r"(?<![0-9A-Za-z])[0-9A-Fa-f]{2}(?P<s>[:\-_.])(?:[0-9A-Fa-f]{2}(?P=s)){4}[0-9A-Fa-f]{2}(?![0-9A-Za-z])")
_MAC_DOTTED_RE = re.compile(r"(?<![0-9A-Za-z])[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}(?![0-9A-Za-z])")
# Zwarte 12 cyfr szesnastkowych z przynajmniej jedną literą A–F (same cyfry = licznik/czas, nie MAC).
_MAC_COMPACT_RE = re.compile(r"(?<![0-9A-Za-z])(?=[0-9]{0,11}[A-Fa-f])[0-9A-Fa-f]{12}(?![0-9A-Za-z])")
# Wartość opisana „mac” (`MAC: 001122334455`, `wifi_mac=00-11-…`) — także same cyfry.
_MAC_LABELLED_RE = re.compile(
    r"(?i)((?<![a-z])mac(?:[ _\-]?addr(?:ess)?)?\s*[:=]\s*)((?:[0-9a-f]{2}[:\-_.]?){5}[0-9a-f]{2})(?![0-9a-z])")
# Ciągi znaków szesnastkowych, dwukropków i kropek (z opcjonalną strefą `%eth0`) — kandydaci
# na IPv6; o masce decyduje `_ipv6_repl` (liczba dwukropków + walidacja), nie regex. Jedna
# klasa znaków bez zagnieżdżonych kwantyfikatorów = koszt liniowy.
_IPV6_RUN_RE = re.compile(r"[0-9A-Fa-f:.]{2,}(?:%[0-9A-Za-z_.\-]{1,32})?")
# IPv4 z zerami wiodącymi; separator kropka, a także `_`/`-` (HA slugifikuje adres w entity_id).
# Bez sprawdzania zakresu oktetów: fałszywe trafienie (np. wersja "1.2.3.4") jest tańsze niż wyciek.
_IPV4_RE = re.compile(r"(?<!\d)0*\d{1,3}(?P<s>[._\-])0*\d{1,3}(?P=s)0*\d{1,3}(?P=s)0*\d{1,3}(?!\d)")
# Nazwy hostów sieci domowej (mDNS, routery) i dynamicznego DNS w zwykłym tekście.
_LOCAL_HOST_RE = re.compile(
    r"(?i)\b[a-z0-9](?:[a-z0-9\-]{0,62}\.){1,8}"
    r"(?:local|lan|home\.arpa|home|internal|localdomain|fritz\.box|myfritz\.net|duckdns\.org|"
    r"ddns\.net|no-ip\.(?:org|com|biz|info)|dyndns\.(?:org|com)|nabu\.casa)\b")


def _mac_mask(raw: str) -> str:
    """Prefiks producenta (OUI) zostaje, jak w `build_report`; reszta gwiazdki. MAC administrowany
    lokalnie (bit 0x02 pierwszego oktetu — druga cyfra 2/6/A/E) jest losowy, więc prefiks też
    identyfikuje urządzenie — maskowany w całości."""
    hexdigits = re.sub(r"[^0-9A-Fa-f]", "", raw)
    if len(hexdigits) >= 2 and hexdigits[1].upper() in "26AE":
        return "*" * 12
    return hexdigits[:6].upper() + "******"


# Forma już zamaskowana przez `build_report` (`AABBCC******`) — prefiks MAC-a lokalnego też znika.
_MASKED_MAC_RE = re.compile(r"(?<![0-9A-Za-z])[0-9A-Fa-f]{6}\*{6}")


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


def _apply(s: str) -> str:
    s = _URL_RE.sub(lambda m: m.group(1) + _HOST, s)
    s = _MAC_LABELLED_RE.sub(lambda m: m.group(1) + _mac_mask(m.group(2)), s)
    s = _MAC_SEP_RE.sub(lambda m: _mac_mask(m.group(0)), s)
    s = _MAC_DOTTED_RE.sub(lambda m: _mac_mask(m.group(0)), s)
    s = _MAC_COMPACT_RE.sub(lambda m: _mac_mask(m.group(0)), s)
    s = _MASKED_MAC_RE.sub(lambda m: _mac_mask(m.group(0)), s)
    s = _EMAIL_RE.sub("<EMAIL>", s)
    s = _IPV6_RUN_RE.sub(_ipv6_repl, s)
    s = _IPV4_RE.sub(_IP, s)
    return _LOCAL_HOST_RE.sub(_HOST, s)


@functools.lru_cache(maxsize=4096)
def _mask_text(text: str) -> str:
    # Pamięć podręczna: raport powtarza te same teksty (platforma, jednostka, klasa) tysiące razy.
    masked = _apply(text)
    norm = _strip_format(text)
    if norm == text:
        return masked
    # Tekst z formą zgodności/znakami formatującymi: jeśli postać znormalizowana coś ukrywa,
    # wysyłamy ją zamaskowaną (oryginał mógłby przemycić adres); inaczej oryginał bez zmian.
    masked_norm = _apply(norm)
    return masked_norm if masked_norm != norm else masked


def _key_label(k: Any) -> str | None:
    if not isinstance(k, str):
        return None
    low = k.lower()
    if low in _HOST_KEYS or low.endswith(_HOST_SUFFIXES):
        return "host"
    return "mac" if "mac" in low else None


class _Budget:
    def __init__(self) -> None:
        self.left = MAX_NODES


def _mask_str(value: str, label: str | None) -> str:
    if label == "host" and value:
        return _HOST
    if label == "mac":
        norm = _strip_format(value)
        if re.fullmatch(r"(?:[0-9A-Fa-f]{2}[:\-_. ]?){5}[0-9A-Fa-f]{2}", norm):
            return _mac_mask(norm)
    return _mask_text(value)


def _mask(value: Any, depth: int, budget: _Budget, label: str | None = None) -> Any:
    if budget.left <= 0 or depth > MAX_DEPTH:
        return _CUT
    budget.left -= 1
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _mask_str(value, label)
    if isinstance(value, dict):
        out: dict = {}
        for k, v in value.items():
            key = _mask_text(k) if isinstance(k, str) else _mask_text(str(k))
            out[key] = _mask(v, depth + 1, budget, _key_label(k))
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_mask(v, depth + 1, budget, label) for v in value]
    return _mask_text(str(value))


def mask_network(value: Any) -> Any:
    """Kopia `value` z zamaskowanymi adresami sieciowymi, MAC-ami i e-mailami w każdym tekście
    na każdej głębokości, także w kluczach słowników; wartości pod kluczami hosta/IP w całości.
    Krotki/zbiory → listy (JSON). Głębokość i liczba węzłów ograniczone — nadmiar zastępuje
    znacznik `<CUT>`."""
    return _mask(value, 0, _Budget())


def _hide_entity(ent: Any) -> None:
    if not isinstance(ent, dict):
        return
    names = " ".join(str(ent.get(k) or "") for k in _ENTITY_NAME_KEYS)
    state = ent.get("state")
    numeric = isinstance(state, (int, float)) or (isinstance(state, str) and _NUMBER_RE.fullmatch(state.strip()))
    if not (_SENSITIVE_RE.search(names) or (_NAME_LIKE_RE.search(names) and not numeric)):
        return
    for k in ("state", "options"):
        if ent.get(k) is not None:
            ent[k] = _HIDDEN


def _hide_account_data(report: dict) -> None:
    """Tytuł wpisu integracji (często login konta) i stany encji kont/sieci → `***` (w miejscu)."""
    for inv in report.get("inverters") or ():
        if not isinstance(inv, dict):
            continue
        if inv.get("config_entry_title"):
            inv["config_entry_title"] = _HIDDEN
        for ent in inv.get("entities") or ():
            _hide_entity(ent)
    for ent in report.get("price_entities") or ():
        _hide_entity(ent)


def _size(report: dict) -> int:
    # Tak samo jak serializuje aiohttp (`json.dumps`, ensure_ascii).
    return len(json.dumps(report).encode())


def _trim_entities(report: dict) -> bool:
    """Połowa najdłuższej listy encji falownika; False, gdy nie ma już czego ciąć."""
    lists = [inv["entities"] for inv in report.get("inverters") or ()
             if isinstance(inv, dict) and isinstance(inv.get("entities"), list) and inv["entities"]]
    if not lists:
        return False
    longest = max(lists, key=len)
    del longest[len(longest) // 2:]
    return True


def prepare_report(report: Any) -> dict | None:
    """Raport gotowy do wysyłki: zamaskowany i ≤ `MAX_REPORT_BYTES`. Za duży → zrzucamy
    sekcje opcjonalne od największej, potem przycinamy listy encji falowników (co ucięto — w
    `truncated`); wciąż za duży albo nie-słownik → None (nie wysyłamy)."""
    if not isinstance(report, dict):
        return None
    out = mask_network(report)
    _hide_account_data(out)
    dropped: list[str] = []
    while _size(out) > MAX_REPORT_BYTES:
        present = [k for k in _OPTIONAL_SECTIONS if k in out]
        if present:
            largest = max(present, key=lambda k: len(json.dumps(out[k])))
            del out[largest]
            dropped.append(largest)
        elif _trim_entities(out):
            if _ENTITIES_CUT not in dropped:
                dropped.append(_ENTITIES_CUT)
        else:
            return None
        out["truncated"] = list(dropped)
    return out


def _request_id(block: Any) -> str | None:
    rid = block.get("request_id") if isinstance(block, dict) else None
    return rid.lower() if isinstance(rid, str) and _UUID.fullmatch(rid) else None


def _failed_run(report: dict) -> bool:
    """Raport z przebiegu, który się nie udał (timeout / błąd runnera) — pusty, nie do wysyłki."""
    errors = report.get("errors")
    return isinstance(errors, list) and any(
        isinstance(e, str) and (e == ERROR_TIMEOUT or e.startswith(ERROR_RUNNER_PREFIX)) for e in errors)


class DiscoveryReportSender:
    """Wysyłka raportu raz na `request_id` (pamięć w Store, ograniczone ponawianie)."""

    def __init__(self, hass, cloud, report: Callable[[], dict | None], *, store=None) -> None:
        self._hass = hass
        self._cloud = cloud
        self._report = report
        self._store = store if store is not None else Store(
            hass, DISCOVERY_SENT_STORAGE_VERSION, DISCOVERY_SENT_STORAGE_KEY)
        self._sent: list[str] = []
        self._attempts: dict[str, int] = {}
        self._loaded = False
        self._busy = False
        self._stopped = False

    def stop(self) -> None:
        """Rozładunek wpisu: żadna nowa wysyłka; trwająca nie dojdzie do POST-a."""
        self._stopped = True

    @staticmethod
    def wants(block: Any) -> bool:
        """Czy pole planu w ogóle niesie poprawne żądanie (bez tego nie startujemy zadania)."""
        return _request_id(block) is not None

    async def async_handle(self, block: Any) -> str:
        """Obsługa pola `discovery_request` z planu. Wynik (do testów i logów debug): "none",
        "busy", "stopped", "done", "not_ready", "sent", "refused", "too_large", "retry", "gave_up"."""
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

    async def _prepare(self, report: dict) -> dict | None:
        # Maskowanie to praca CPU — poza pętlą zdarzeń, gdy jest HA.
        if self._hass is not None:
            return await self._hass.async_add_executor_job(prepare_report, report)
        return prepare_report(report)

    async def _handle(self, rid: str) -> str:
        if self._stopped:
            return "stopped"
        await self._load()
        if rid in self._sent:
            return "done"
        try:
            report = self._report()
        except Exception as err:  # noqa: BLE001 — źródło raportu nie może wywrócić planu
            _LOGGER.debug("Volcast discovery report unavailable (%s)", type(err).__name__)
            return "not_ready"
        if not isinstance(report, dict) or _failed_run(report):
            return "not_ready"          # wykrywanie nie skończyło / nie udało się — próba nie liczona
        prev = self._attempts.get(rid, 0)
        if prev >= MAX_ATTEMPTS:        # ostatnia próba przerwana (anulowanie, limit czasu)
            await self._finish(rid)
            return "gave_up"
        attempt = prev + 1
        self._attempts.pop(rid, None)
        self._attempts[rid] = attempt
        while len(self._attempts) > SENT_MAX:
            self._attempts.pop(next(iter(self._attempts)))
        # Próba liczona PRZED maskowaniem i wysyłką — przerwanie (limit czasu, rozładunek) jej nie kasuje.
        await self._save()
        payload = await self._prepare(report)
        if payload is None:
            _LOGGER.warning("Volcast discovery report too large to send — request skipped")
            await self._finish(rid)
            return "too_large"
        if self._stopped:
            return "stopped"
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
        if attempt >= MAX_ATTEMPTS:
            _LOGGER.warning("Volcast discovery report not delivered after %s attempts (HTTP %s)",
                            attempt, status)
            await self._finish(rid)
            return "gave_up"
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
            self._sent = [s.lower() for s in sent if isinstance(s, str) and _UUID.fullmatch(s)][-SENT_MAX:]
        attempts = data.get("attempts")
        if isinstance(attempts, dict):
            self._attempts = {
                k.lower(): v for k, v in list(attempts.items())[-SENT_MAX:]
                if isinstance(k, str) and _UUID.fullmatch(k)
                and isinstance(v, int) and not isinstance(v, bool) and 0 < v <= MAX_ATTEMPTS}

    async def _save(self) -> None:
        try:
            await self._store.async_save({"sent": list(self._sent), "attempts": dict(self._attempts)})
        except Exception as err:  # noqa: BLE001 — zapis pamięci nie może wywrócić planu
            _LOGGER.debug("Volcast discovery memory save failed (%s)", type(err).__name__)


async def async_send_bounded(sender: DiscoveryReportSender, block: Any) -> None:
    """Jedna wysyłka w tle z twardym limitem czasu; błędy połknięte (bez treści w logu),
    anulowanie (rozładunek) przechodzi dalej."""
    try:
        outcome = await asyncio.wait_for(sender.async_handle(block), SEND_TIMEOUT_S)
    except asyncio.TimeoutError:
        _LOGGER.warning("Volcast discovery report timed out after %ss", SEND_TIMEOUT_S)
        return
    except Exception as err:  # noqa: BLE001 — wysyłka raportu nie może zostawić wyjątku w zadaniu
        _LOGGER.debug("Volcast discovery report failed (%s)", type(err).__name__)
        return
    _LOGGER.debug("Volcast discovery report: %s", outcome)
