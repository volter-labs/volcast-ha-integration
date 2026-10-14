"""Podpisany feed profili marek (`profiles/v1`): weryfikacja, rejestr i aktualizacja raz na dobę.

Kontrakt: `docs/box/profile-feed-contract.md` w repo Volcast. Feed jest publiczny — zaufanie daje
WYŁĄCZNIE podpis Ed25519 nad kanonicznym JSON-em obiektu `feed`, weryfikowany kluczem publicznym
wpisanym w integrację. Każdy błąd (podpis, nieznany klucz, wersja, starszy albo podmieniony feed,
zły JSON, rozmiar, choćby jeden zły profil) = odrzucenie CAŁEGO feedu; trzymany zostaje, w logu tylko
kod przyczyny (bez treści feedu). Profil z feedu jest zawsze `draft` (także `modbus` i wpisy
`ha.integrations`) — feed nigdy nie podnosi zaufania; pakietowy `verified` z nim wygrywa
(`profile.load_builtin`).

Bez kluczy publicznych moduł jest bezczynny: nic nie czyta z magazynu, nic nie pobiera.
Rdzeń bez Home Assistanta: magazyn (`async_load`/`async_save`), pobieranie i wykonawca zadań
blokujących wstrzykuje warstwa HA. `cryptography` importowane leniwie — jego brak = odrzucenie.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import copy
import functools
import hashlib
import json
import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Mapping

from .profile_schema import validate_profile
from .regex_safety import fits, safe_match, safe_search

_LOGGER = logging.getLogger(__name__)

FEED_MAX_BYTES = 2 * 1024 * 1024
FEED_SCHEMA_VERSION = 1
FEED_FIRST_CHECK_DELAY_S = 300
FEED_CHECK_INTERVAL_S = 24 * 3600
# Twardy limit całego sprawdzenia (pobranie + weryfikacja + zapis) — zadanie w tle nigdy nie wisi.
FEED_CHECK_TIMEOUT_S = 90.0
FEED_MAX_PROFILES = 500

_MAX_SAFE_INT = 2 ** 53 - 1
_MIN_FRACTION = 1e-4
# Zapis chmury (`toISOString`): RRRR-MM-DDTHH:MM:SS.sssZ; dopuszczamy też bez milisekund.
_GENERATED_AT_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z")
_HEX_KEY_RE = re.compile(r"[0-9a-fA-F]{64}")
# Identyfikacja wąska (jak brama profili w CI): regex modelu zakotwiczony, niepasujący do pustego
# ani do modeli niezwiązanych z żadnym falownikiem; typ urządzenia bez wartości pustego rejestru.
_MODEL_PROBES = ("A", "Q", "z", "0", "7", "-", "QX", "ZZZZZZZZ", "Q0-Q0", "00000000", "xyz123", "NOT-A-MODEL")
_BLANK_REGISTER_VALUES = (0x0000, 0xFFFF)
_MAX_DEVICE_TYPE_EXPECT = 64
_FLOOR_MAX_AHEAD_S = 24 * 3600


class CanonicalError(ValueError):
    """Wartość spoza kanonicznego JSON-u kontraktu (nie da się jej podpisać ani zweryfikować)."""


class FeedRejected(Exception):
    """Feed odrzucony; `reason` to stały kod (bez treści feedu) — trafia do logu."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --- kanoniczny JSON (`canonical.ts`) ------------------------------------------------------------

def _has_lone_surrogate(s: str) -> bool:
    return any(0xD800 <= ord(c) <= 0xDFFF for c in s)


def _normalize(value: Any) -> Any:
    """Kopia wartości w postaci, którą `json.dumps` zapisze bajt w bajt jak `canonical.ts`."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > _MAX_SAFE_INT:
            raise CanonicalError("liczba całkowita poza zakresem")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalError("NaN/nieskończoność")
        if value.is_integer():                       # JS pisze 1.0 jako "1", -0 jako "0"
            as_int = int(value)
            if abs(as_int) > _MAX_SAFE_INT:
                raise CanonicalError("liczba całkowita poza zakresem")
            return as_int
        if abs(value) < _MIN_FRACTION:
            raise CanonicalError("ułamek < 1e-4")
        return value
    if isinstance(value, str):
        if _has_lone_surrogate(value):
            raise CanonicalError("samotny surogat")
        return value
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if not isinstance(k, str) or _has_lone_surrogate(k):
                raise CanonicalError("klucz obiektu spoza kontraktu")
            out[k] = _normalize(v)
        return out
    raise CanonicalError(f"typ {type(value).__name__} spoza JSON")


def canonical_bytes(value: Any) -> bytes:
    """Bajty UTF-8 kanonicznego JSON-u (klucze po punktach kodowych, bez spacji, znaki surowo)."""
    try:
        normalized = _normalize(value)
    except RecursionError as err:
        raise CanonicalError("zbyt głębokie zagnieżdżenie") from err
    text = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)
    return text.encode("utf-8")


def feed_etag(signature: str) -> str:
    """`ETag` chmury: `"` + SHA-256 (hex) napisu podpisu + `"`."""
    return '"' + hashlib.sha256(signature.encode("utf-8")).hexdigest() + '"'


# --- weryfikacja ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class VerifiedFeed:
    document: Mapping[str, Any]           # {"feed", "key_id", "signature"} — do zapisu w magazynie
    key_id: str
    signature: str
    generated_at: datetime
    profiles: Mapping[str, dict]          # id → profil po wymuszeniu `draft`
    ignored: tuple[str, ...] = ()         # wpisy o id profilu z pakietu (pominięte)
    overlapping: tuple[str, ...] = ()     # wpisy rozpoznające urządzenie profilu z pakietu (pominięte)

    @property
    def etag(self) -> str:
        return feed_etag(self.signature)


def _reject_constant(_name: str) -> Any:
    raise ValueError("NaN/Infinity")


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict:
    out: dict = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("powtórzony klucz")
        out[k] = v
    return out


def parse_feed_body(body: bytes) -> dict:
    """Treść odpowiedzi → dokument; rozmiar, UTF-8 i ścisły JSON (bez powtórzonych kluczy, NaN)."""
    if not isinstance(body, (bytes, bytearray)):
        raise FeedRejected("malformed")
    if len(body) > FEED_MAX_BYTES:
        raise FeedRejected("too_large")
    try:
        doc = json.loads(bytes(body).decode("utf-8"), object_pairs_hook=_no_duplicate_keys,
                         parse_constant=_reject_constant)
    except (ValueError, RecursionError) as err:
        raise FeedRejected("malformed") from err
    if not isinstance(doc, dict):
        raise FeedRejected("malformed")
    return doc


def _public_key(keys: Mapping[str, str], key_id: str):
    raw_hex = keys.get(key_id)
    if not isinstance(raw_hex, str) or not _HEX_KEY_RE.fullmatch(raw_hex):
        raise FeedRejected("unknown_key")
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as err:                       # bez biblioteki nic nie przechodzi
        raise FeedRejected("crypto_unavailable") from err
    return Ed25519PublicKey.from_public_bytes(bytes.fromhex(raw_hex))


def _signature_bytes(sig: Any) -> bytes:
    if not isinstance(sig, str) or not sig:
        raise FeedRejected("signature")
    try:
        raw = base64.b64decode(sig, validate=True)
    except (binascii.Error, ValueError) as err:
        raise FeedRejected("signature") from err
    if len(raw) != 64:
        raise FeedRejected("signature")
    return raw


def _parse_generated_at(value: Any) -> datetime:
    if not isinstance(value, str) or not _GENERATED_AT_RE.fullmatch(value):
        raise FeedRejected("generated_at")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00").astimezone(timezone.utc)
    except ValueError as err:
        raise FeedRejected("generated_at") from err


def _regex_ok(rx: Any) -> bool:
    if not isinstance(rx, str) or not rx.startswith("^"):
        return False
    try:
        return safe_search(rx, "") is None and not any(safe_match(rx, p) for p in _MODEL_PROBES)
    except re.error:
        return False


def _identify_narrow(raw: dict) -> bool:
    ident = raw.get("identify") or {}
    if not all(_regex_ok(rx) for rx in ident.get("model_regex") or ()):
        return False
    expect = ((ident.get("registers") or {}).get("device_type") or {}).get("expect") or []
    return len(expect) <= _MAX_DEVICE_TYPE_EXPECT and not set(expect) & set(_BLANK_REGISTER_VALUES)


def _as_draft(raw: dict) -> dict:
    """Kopia profilu z każdym statusem zaufania ustawionym na `draft` (feed nie podnosi zaufania)."""
    out = copy.deepcopy(raw)
    out["status"] = "draft"
    if isinstance(out.get("modbus"), dict):
        out["modbus"]["status"] = "draft"
    for integ in (out.get("ha") or {}).get("integrations") or ():
        if isinstance(integ, dict):
            integ["status"] = "draft"
    return out


# --- obrazy wzorcowe profili pakietu (kontrola nakładania się) ---------------------------------------

REFS_PATH = Path(__file__).with_name("identity_refs.json")


@functools.lru_cache(maxsize=1)
def packaged_refs() -> Mapping[str, Mapping[str, Any]]:
    """{id profilu pakietu: obrazy rejestrów, model, domeny HA, verified} — plik pilnowany testem
    zgodności z profilami pakietu i `tests/golden/`. Odczyt pliku: pierwsze wołanie w `run_job`."""
    return MappingProxyType(json.loads(REFS_PATH.read_text(encoding="utf-8")))


def packaged_verified_ids() -> frozenset[str]:
    return frozenset(pid for pid, ref in packaged_refs().items() if ref.get("verified") is True)


def _ref_image(ref: Mapping[str, Any]):
    from .registers import RegisterImage
    return RegisterImage({int(a): w for a, w in ref["holding"].items()},
                         {int(a): w for a, w in ref["input"].items()})


def overlaps_packaged(raw: dict, *, only: str | None = None) -> bool:
    """Czy profil rozpoznałby urządzenie z obrazu wzorcowego profilu pakietu (rejestry identyfikacji)
    albo jego model w tej samej domenie integracji HA. Zachowawczo: takie nakładanie się = odrzucenie."""
    from .modbus.identity import identity_info
    from .profile import profile_from_dict
    profile = profile_from_dict(raw)
    domains: dict[str, list[str]] = {}
    for integ in (raw.get("ha") or {}).get("integrations") or ():
        domains.setdefault(integ["domain"], []).extend(integ.get("model_regex") or ())
    own = list((raw.get("identify") or {}).get("model_regex") or ())
    for pid, ref in packaged_refs().items():
        if only is not None and pid != only:
            continue
        if identity_info(profile, _ref_image(ref))["matched"]:
            return True
        model = ref.get("model")
        shared = [d for d in ref.get("domains") or () if d in domains]
        if isinstance(model, str) and shared:
            if not fits(model):
                return True   # model za długi do sprawdzenia: zachowawczo nakładanie się
            regexes = own + [rx for d in shared for rx in domains[d]]
            if any(safe_search(rx, model) for rx in regexes):
                return True
    return False


def _one_profile(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise FeedRejected("profile_invalid")
    try:
        if validate_profile(raw):
            raise FeedRejected("profile_invalid")
        draft = _as_draft(raw)
        # Te same reguły co dla profilu z pakietu (id kebab-case, schemat, bezpieczeństwo wzorców
        # `*_regex` — `regex_problem` z drzewem parsera, fail closed), na kopii już zdegradowanej.
        if validate_profile(draft, parse_tree=True) or not _identify_narrow(draft):
            raise FeedRejected("profile_invalid")
    except (TypeError, AttributeError, ValueError, KeyError, re.error) as err:
        raise FeedRejected("profile_invalid") from err
    return draft


def _profiles(raw_list: Any) -> tuple[dict[str, dict], tuple[str, ...], tuple[str, ...]]:
    """(nowe profile, pominięte id pakietu, pominięte nakładające się). Etap 1: feed tylko DODAJE marki —
    wpis o id profilu z pakietu albo rozpoznający urządzenie profilu z pakietu jest pomijany (zależy od
    zestawu pakietu TEJ wersji integracji, więc nie może odrzucać całego feedu); wpis zepsuty albo
    niebezpieczny odrzuca cały feed."""
    if not isinstance(raw_list, list) or len(raw_list) > FEED_MAX_PROFILES:
        raise FeedRejected("profile_invalid")
    packaged = packaged_refs()
    out: dict[str, dict] = {}
    ignored: list[str] = []
    overlapping: list[str] = []
    seen: set[str] = set()
    for raw in raw_list:
        draft = _one_profile(raw)
        pid = draft["id"]
        if pid in seen:
            raise FeedRejected("profile_invalid")
        seen.add(pid)
        if pid in packaged:
            ignored.append(pid)
            continue
        try:
            overlap = overlaps_packaged(draft)
        except (TypeError, AttributeError, ValueError, KeyError, re.error) as err:
            raise FeedRejected("profile_invalid") from err
        if overlap:
            overlapping.append(pid)
            continue
        out[pid] = draft
    return out, tuple(sorted(ignored)), tuple(sorted(overlapping))


def verify_feed(doc: Any, *, keys: Mapping[str, str], held: VerifiedFeed | None,
                floor: datetime | None = None) -> VerifiedFeed:
    """Weryfikacja w kolejności kontraktu (§2); `held` = feed już trzymany (anty-rollback), `floor` =
    najnowsze `generated_at` kiedykolwiek przyjęte (gdy trzymanego feedu nie da się już zweryfikować)."""
    # Pola koperty poza tymi trzema nie są podpisane — pomijamy je (nie trafiają też do magazynu).
    if not isinstance(doc, dict) or not {"feed", "key_id", "signature"} <= set(doc):
        raise FeedRejected("malformed")
    feed, key_id = doc["feed"], doc["key_id"]
    if not isinstance(feed, dict) or not isinstance(key_id, str):
        raise FeedRejected("malformed")
    # 1. znany klucz, zgodny z polem w podpisanym obiekcie
    public = _public_key(keys, key_id)
    if feed.get("key_id") != key_id:
        raise FeedRejected("key_mismatch")
    # 2. podpis nad kanonicznymi bajtami `feed`
    sig = _signature_bytes(doc["signature"])
    try:
        payload = canonical_bytes(feed)
    except CanonicalError as err:
        raise FeedRejected("malformed") from err
    try:
        from cryptography.exceptions import InvalidSignature
    except ImportError as err:
        raise FeedRejected("crypto_unavailable") from err
    try:
        public.verify(sig, payload)
    except InvalidSignature as err:
        raise FeedRejected("signature") from err
    # 3. wersja schematu
    version = feed.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != FEED_SCHEMA_VERSION:
        raise FeedRejected("schema_version")
    # 4. anty-rollback: starszy odrzucony, równy tylko z identycznym podpisem
    generated_at = _parse_generated_at(feed.get("generated_at"))
    if floor is not None and generated_at < floor:
        raise FeedRejected("rollback")
    if held is not None:
        if generated_at < held.generated_at:
            raise FeedRejected("rollback")
        if generated_at == held.generated_at and doc["signature"] != held.signature:
            raise FeedRejected("replay")
    profiles, ignored, overlapping = _profiles(feed.get("profiles"))
    document = {"feed": copy.deepcopy(feed), "key_id": key_id, "signature": doc["signature"]}
    return VerifiedFeed(document=document, key_id=key_id, signature=doc["signature"],
                        generated_at=generated_at, profiles=profiles, ignored=ignored, overlapping=overlapping)


def verify_feed_body(body: bytes, *, keys: Mapping[str, str], held: VerifiedFeed | None,
                     floor: datetime | None = None) -> VerifiedFeed:
    return verify_feed(parse_feed_body(body), keys=keys, held=held, floor=floor)


# --- rejestr profili z feedu (czyta `profile.py`) --------------------------------------------------

_FEED_PROFILES: Mapping[str, dict] = MappingProxyType({})


def set_feed_profiles(profiles: Mapping[str, dict]) -> None:
    """Podmienia cały zestaw profili z feedu (kopie; podmiana jednym przypisaniem)."""
    global _FEED_PROFILES
    _FEED_PROFILES = MappingProxyType({k: copy.deepcopy(v) for k, v in profiles.items()})


def feed_ids() -> frozenset[str]:
    return frozenset(_FEED_PROFILES)


def feed_profile(profile_id: str) -> dict | None:
    raw = _FEED_PROFILES.get(profile_id)
    return copy.deepcopy(raw) if raw is not None else None


# --- aktualizacja ----------------------------------------------------------------------------------

@dataclass(frozen=True)
class FeedFetch:
    """Wynik pobrania: "ok" (z treścią), "not_modified" (304) albo "error" (sieć, 4xx/5xx, rozmiar)."""
    status: str
    body: bytes | None = None


def _usable_keys(keys: Mapping[str, str] | None) -> dict[str, str]:
    return {k: v for k, v in (keys or {}).items()
            if isinstance(k, str) and k and isinstance(v, str) and _HEX_KEY_RE.fullmatch(v)}


class ProfileFeedUpdater:
    """Feed z magazynu przy starcie (ponownie weryfikowany) i sprawdzenie w sieci najwyżej raz na dobę
    (pierwsze `FEED_FIRST_CHECK_DELAY_S` po starcie, `If-None-Match` z ETag). Magazyn zapisywany
    wyłącznie po pełnej weryfikacji."""

    def __init__(self, store, *, keys: Mapping[str, str] | None,
                 clock: Callable[[], float] = time.time,
                 run_job: Callable[..., Awaitable[Any]] | None = None,
                 on_new_profiles: Callable[[frozenset[str], Mapping[str, dict]], None] | None = None) -> None:
        self._store = store
        self._keys = _usable_keys(keys)
        self._clock = clock
        self._run_job = run_job
        self._on_new = on_new_profiles
        self._next_check = clock() + FEED_FIRST_CHECK_DELAY_S
        self._loaded = False
        self._load_lock = asyncio.Lock()
        self._busy = False
        self.held: VerifiedFeed | None = None
        # Najnowsze przyjęte `generated_at` (zapisywane obok feedu) — anty-rollback także wtedy, gdy
        # trzymanego feedu nie da się już zweryfikować (np. po rotacji klucza).
        self._floor: datetime | None = None

    @property
    def active(self) -> bool:
        return bool(self._keys)

    def due(self) -> bool:
        return self.active and not self._busy and self._clock() >= self._next_check

    def defer(self) -> None:
        """Następne sprawdzenie za dobę (np. po przekroczeniu limitu czasu — bez ponawiania co 5 min)."""
        self._next_check = self._clock() + FEED_CHECK_INTERVAL_S

    async def _job(self, fn, *args):
        if self._run_job is not None:
            return await self._run_job(fn, *args)
        return fn(*args)

    async def async_load(self) -> None:
        """Raz: feed z magazynu, zweryfikowany od nowa (np. klucz wycofany w nowym wydaniu)."""
        if not self.active:
            return
        async with self._load_lock:
            if self._loaded:
                return
            self._loaded = True
            try:
                data = await self._store.async_load()
            except Exception as err:  # noqa: BLE001 — bez magazynu działamy na profilach pakietu
                _LOGGER.debug("Volcast profile feed store load failed (%s)", type(err).__name__)
                return
            if not isinstance(data, dict):
                return
            checked = data.get("checked_at")
            now = self._clock()
            if isinstance(checked, (int, float)) and not isinstance(checked, bool) and checked <= now:
                self._next_check = max(self._next_check, checked + FEED_CHECK_INTERVAL_S)
            self._floor = self._stored_floor(data.get("floor"), now)
            try:
                held = await self._job(_verify_stored, data.get("document"), self._keys)
            except FeedRejected as err:
                _LOGGER.warning("Volcast stored profile feed rejected (%s) — using packaged profiles",
                                err.reason)
                return
            self.held = held
            self._raise_floor(held.generated_at)
            set_feed_profiles(held.profiles)
            _log_skipped(held)

    @staticmethod
    def _stored_floor(raw: Any, now: float) -> datetime | None:
        """Próg z magazynu; dalej niż dobę w przyszłości = pominięty (uszkodzony zapis nie blokuje feedu)."""
        try:
            floor = _parse_generated_at(raw)
        except FeedRejected:
            return None
        limit = datetime.fromtimestamp(now, timezone.utc) + timedelta(seconds=_FLOOR_MAX_AHEAD_S)
        return floor if floor <= limit else None

    def _raise_floor(self, generated_at: datetime) -> None:
        if self._floor is None or generated_at > self._floor:
            self._floor = generated_at

    async def async_check(self, fetch: Callable[[str | None], Awaitable[FeedFetch]]) -> str:
        """Jedno sprawdzenie: "inert", "busy", "error", "unchanged", "rejected" albo "updated"."""
        if not self.active:
            return "inert"
        if self._busy:
            return "busy"
        self._busy = True
        previous_due = self._next_check
        self._next_check = self._clock() + FEED_CHECK_INTERVAL_S
        try:
            await self.async_load()
            return await self._check(fetch)
        except asyncio.CancelledError:
            self._next_check = previous_due          # rozładunek w trakcie — sprawdzenie nie przepada
            raise
        finally:
            self._busy = False

    async def _check(self, fetch) -> str:
        held = self.held
        try:
            result = await fetch(held.etag if held is not None else None)
        except Exception as err:  # noqa: BLE001 — klient nie powinien rzucać; to błąd sieci
            _LOGGER.debug("Volcast profile feed fetch failed (%s)", type(err).__name__)
            return "error"
        status = getattr(result, "status", None)
        if status == "not_modified" and held is not None:
            await self._save(held)
            return "unchanged"
        if status != "ok" or not isinstance(result.body, (bytes, bytearray)):
            return "error"
        try:
            verified = await self._job(_verify_body, bytes(result.body), self._keys, held, self._floor)
        except FeedRejected as err:
            _LOGGER.warning("Volcast profile feed rejected (%s) — keeping the previous profiles", err.reason)
            return "rejected"
        if held is not None and verified.signature == held.signature:
            await self._save(held)
            return "unchanged"
        self._raise_floor(verified.generated_at)
        await self._save(verified)
        before = frozenset(held.profiles) if held is not None else frozenset()
        self.held = verified
        set_feed_profiles(verified.profiles)
        _log_skipped(verified)
        new_ids = frozenset(verified.profiles) - before
        if new_ids and self._on_new is not None:
            try:
                self._on_new(new_ids, MappingProxyType(dict(verified.profiles)))
            except Exception as err:  # noqa: BLE001 — powiadomienie nie cofa aktualizacji
                _LOGGER.debug("Volcast profile feed notification failed (%s)", type(err).__name__)
        _LOGGER.info("Volcast profile feed updated (%s profiles)", len(verified.profiles))
        return "updated"

    async def _save(self, feed: VerifiedFeed) -> None:
        try:
            await self._store.async_save({"document": copy.deepcopy(dict(feed.document)),
                                          "etag": feed.etag, "checked_at": self._clock(),
                                          "floor": _iso(self._floor or feed.generated_at)})
        except Exception as err:  # noqa: BLE001 — feed i tak działa w procesie
            _LOGGER.warning("Volcast profile feed could not be saved (%s)", type(err).__name__)


def _log_skipped(feed: VerifiedFeed) -> None:
    """Pominięte wpisy — wyłącznie ich id (kebab-case ze schematu), bez treści feedu."""
    if feed.ignored:
        _LOGGER.warning("Volcast profile feed: ignored entries for profiles shipped with the integration (%s)",
                        ", ".join(feed.ignored))
    if feed.overlapping:
        _LOGGER.warning("Volcast profile feed: ignored entries that match an inverter of a shipped profile (%s)",
                        ", ".join(feed.overlapping))


def _iso(dt: datetime) -> str:
    return f"{dt:%Y-%m-%dT%H:%M:%S}.{dt.microsecond:06d}Z"


def _verify_body(body: bytes, keys: Mapping[str, str], held: VerifiedFeed | None,
                 floor: datetime | None = None) -> VerifiedFeed:
    return verify_feed_body(body, keys=keys, held=held, floor=floor)


def _verify_stored(document: Any, keys: Mapping[str, str]) -> VerifiedFeed:
    return verify_feed(document, keys=keys, held=None)


async def async_check_bounded(updater: ProfileFeedUpdater,
                              fetch: Callable[[str | None], Awaitable[FeedFetch]]) -> None:
    """Jedno sprawdzenie w tle z twardym limitem czasu; błędy połknięte, anulowanie przechodzi."""
    try:
        outcome = await asyncio.wait_for(updater.async_check(fetch), FEED_CHECK_TIMEOUT_S)
    except asyncio.TimeoutError:
        updater.defer()
        _LOGGER.warning("Volcast profile feed check timed out after %ss", FEED_CHECK_TIMEOUT_S)
        return
    except Exception as err:  # noqa: BLE001 — zadanie w tle nie zostawia wyjątku
        _LOGGER.debug("Volcast profile feed check failed (%s)", type(err).__name__)
        return
    _LOGGER.debug("Volcast profile feed: %s", outcome)
