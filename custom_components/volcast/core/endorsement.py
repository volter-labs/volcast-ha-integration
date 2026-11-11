"""Certyfikat zapisu profilu z feedu: drugi, offline'owy podpis Ed25519 — jedyna droga, którą profil
z feedu dostaje prawo zapisu na falownik (pochodzenie `feed_endorsed`, `profile.writes_allowed`).

Pole profilu: `endorsement: {key_id, digest, signature}` (dokładnie te trzy pola).
  * `digest` = SHA-256 (hex, małe litery) kanonicznych bajtów (`profile_feed.canonical_bytes`, kontrakt
    feedu §3) profilu BEZ pól kosmetycznych (`label`, `status_note`, `sources`, `modbus.status_note`) i
    bez samego `endorsement` — zmiana opisu nie unieważnia certyfikatu, zmiana zachowania tak.
  * `signature` = Ed25519 (base64 standardowe, 64 bajty) kanonicznych bajtów obiektu
    `{domain, profile_id, scope: "write", digest, key_id}`.

Kolejność sprawdzeń i kody (te same co w chmurze i we wspólnych wektorach testowych): `missing` →
`malformed` → `unknown_key` → `digest_mismatch` → `bad_signature`. Weryfikacja nigdy nie rzuca. Sam
certyfikat nie patrzy na `status` — bramkę „certyfikat i `verified`” daje `endorsed_for_write`.
Klucz feedu (serwer) nie może certyfikatu podrobić: klucze certyfikatów są osobne, wpisane w wydanie.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from . import profile_feed

ENDORSEMENT_DOMAIN = "volcast-profile-endorsement/v1"
ENDORSEMENT_SCOPE = "write"
# Pola spoza digest (ścieżka z kropką = pole zagnieżdżone).
ENDORSEMENT_EXCLUDED_FIELDS = ("label", "status_note", "sources", "modbus.status_note", "endorsement")

# Klucze publiczne certyfikatów: {key_id: Ed25519 raw hex (32 bajty)}. Część prywatna leży offline i
# nigdy nie trafia na serwer. Jeden klucz dla obu środowisk (certyfikat dotyczy treści profilu).
# Rotacja: nowy klucz dopisany, stary usunięty w kolejnym wydaniu (przy kompromitacji — od razu).
# Tu, nie w `const.py`: rdzeń importuje się bez reszty integracji (`const` ją reeksportuje).
PROFILE_ENDORSEMENT_PUBLIC_KEYS: Mapping[str, str] = MappingProxyType({
    "endorse-2026-11": "ad139fc29126a322912809f347d013e9df05be29f777a287f495a3c930ada61c",
})

MISSING = "missing"
MALFORMED = "malformed"
UNKNOWN_KEY = "unknown_key"
DIGEST_MISMATCH = "digest_mismatch"
BAD_SIGNATURE = "bad_signature"

_FIELDS = frozenset(("key_id", "digest", "signature"))
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
# 64 bajty w base64 = 86 znaków + "==" (dopasowanie pełne: bez końcowego "\n").
_SIGNATURE_RE = re.compile(r"[A-Za-z0-9+/]{86}==")
_PUBLIC_KEY_RE = re.compile(r"[0-9a-fA-F]{64}")
_MAX_KEY_ID = 64


@dataclass(frozen=True)
class EndorsementResult:
    ok: bool
    key_id: str | None = None        # przy ok
    reason: str | None = None        # przy odrzuceniu: jeden z kodów powyżej


def _digest_payload(profile: Mapping[str, Any]) -> dict:
    """Płytka kopia bez pól spoza digest (wejście nietknięte)."""
    rest = {k: v for k, v in profile.items() if k not in ("label", "status_note", "sources", "endorsement")}
    if isinstance(rest.get("modbus"), dict):
        rest["modbus"] = {k: v for k, v in rest["modbus"].items() if k != "status_note"}
    return rest


def endorsement_digest(profile: Mapping[str, Any]) -> str:
    """SHA-256 (hex) treści profilu bez pól kosmetycznych. Rzuca `CanonicalError` dla treści spoza §3."""
    return hashlib.sha256(profile_feed.canonical_bytes(_digest_payload(profile))).hexdigest()


def endorsement_message(profile_id: str, digest: str, key_id: str) -> bytes:
    """Bajty podpisywanego komunikatu certyfikatu."""
    return profile_feed.canonical_bytes({"domain": ENDORSEMENT_DOMAIN, "profile_id": profile_id,
                                         "scope": ENDORSEMENT_SCOPE, "digest": digest, "key_id": key_id})


def _utf16_len(s: str) -> int:
    # Długość jak `String.length` w chmurze (jednostki UTF-16), żeby kody zgadzały się co do znaku.
    return len(s.encode("utf-16-le")) // 2


def _well_formed(e: Any) -> bool:
    if not isinstance(e, dict) or set(e) != _FIELDS:
        return False
    key_id, digest, sig = e["key_id"], e["digest"], e["signature"]
    return (isinstance(key_id, str) and 0 < _utf16_len(key_id) <= _MAX_KEY_ID
            and isinstance(digest, str) and _DIGEST_RE.fullmatch(digest) is not None
            and isinstance(sig, str) and _SIGNATURE_RE.fullmatch(sig) is not None)


def _signature_ok(message: bytes, signature: str, public_hex: Any) -> bool:
    """Ed25519 nad `message`; zły klucz, zły podpis albo brak biblioteki = False (fail closed)."""
    if not isinstance(public_hex, str) or not _PUBLIC_KEY_RE.fullmatch(public_hex):
        return False
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        return False
    try:
        raw = base64.b64decode(signature, validate=True)
        if len(raw) != 64:
            return False
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex)).verify(raw, message)
    except (InvalidSignature, binascii.Error, ValueError, TypeError):
        return False
    return True


def verify_endorsement(profile: Any, keys: Mapping[str, str] | None = None) -> EndorsementResult:
    """Weryfikuje certyfikat profilu kluczami `keys` (domyślnie klucze wydania). Nigdy nie rzuca."""
    try:
        return _verify(profile, PROFILE_ENDORSEMENT_PUBLIC_KEYS if keys is None else keys)
    except Exception:  # noqa: BLE001 — każdy nieprzewidziany błąd danych = brak prawa zapisu
        return EndorsementResult(False, reason=MALFORMED)


def _verify(profile: Any, keys: Mapping[str, str]) -> EndorsementResult:
    if not isinstance(profile, dict) or "endorsement" not in profile:
        return EndorsementResult(False, reason=MISSING)
    e = profile["endorsement"]
    pid = profile.get("id")
    if not _well_formed(e) or not isinstance(pid, str) or not pid:
        return EndorsementResult(False, reason=MALFORMED)
    try:
        digest = endorsement_digest(profile)
    except (profile_feed.CanonicalError, RecursionError, TypeError, ValueError):
        return EndorsementResult(False, reason=MALFORMED)
    key_id = e["key_id"]
    if key_id not in keys:
        return EndorsementResult(False, reason=UNKNOWN_KEY)
    if digest != e["digest"]:
        return EndorsementResult(False, reason=DIGEST_MISMATCH)
    if not _signature_ok(endorsement_message(pid, digest, key_id), e["signature"], keys[key_id]):
        return EndorsementResult(False, reason=BAD_SIGNATURE)
    return EndorsementResult(True, key_id=key_id)


def endorsed_for_write(profile: Any, keys: Mapping[str, str] | None = None) -> bool:
    """Bramka prawa zapisu profilu z feedu: ważny certyfikat znanym kluczem I `status: verified`."""
    return (isinstance(profile, dict) and profile.get("status") == "verified"
            and verify_endorsement(profile, keys).ok)
