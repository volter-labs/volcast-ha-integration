"""Tożsamość urządzenia z rejestrów identyfikacyjnych — odcisk z solą instalacji.

Numer seryjny nigdy nie wychodzi z tego modułu w postaci jawnej: odcisk to
HMAC-SHA256(sól, profil|serial)[:16] (bez czytelnego seriala: profil|model|moc). Zwykły skrót
seriala dałoby się odwrócić (mała entropia numerów seryjnych), dlatego sól (≥ 16 B) jest wymagana.

Odcisk powstaje tylko dla urządzenia, które NAPRAWDĘ pasuje do profilu (model z `model_regex`
albo typ z `device_type.expect`) i ma czym się wyróżnić. Obraz zdegenerowany — same 0x0000,
same 0xFFFF, puste albo niedrukowalne pola (falownik w trakcie startu, inne urządzenie
odpowiadające zerami) — to tożsamość nieznana (None), nigdy stały odcisk, który potem
„potwierdziłby” dowolne urządzenie odpowiadające tak samo.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any, Mapping

from ..registers import RegisterError, RegisterImage, decode

RATED_POWER_RANGE_W = (1000.0, 30000.0)
MIN_SALT_BYTES = 16
_SERIAL_RE = re.compile(r"[0-9A-Za-z-]{6,}")
_MODEL_RE = re.compile(r"[\x21-\x7e][\x20-\x7e]*")


def _decoded(spec: Mapping[str, Any] | None, image: RegisterImage):
    if spec is None:
        return None
    try:
        return decode(spec, image)
    except (RegisterError, KeyError, ValueError, UnicodeError):
        return None


def _identity_words(profile, image: RegisterImage) -> list[int]:
    out: list[int] = []
    for addr, count in profile.modbus.identify_reads:
        try:
            out.extend(image.words(addr, count))
        except RegisterError:
            pass
    return out


def _fields(profile, image: RegisterImage) -> dict[str, Any]:
    """Pola tożsamości po walidacji; `serial` wyłącznie do odcisku (nigdy na zewnątrz)."""
    ident = profile.raw.get("identify", {})
    regs = ident.get("registers", {})
    words = _identity_words(profile, image)
    degenerate = not words or all(w == 0 for w in words) or all(w == 0xFFFF for w in words)
    serial = _decoded(regs.get("serial"), image)
    serial = serial.strip() if isinstance(serial, str) else None
    if not serial or not _SERIAL_RE.fullmatch(serial):
        serial = None
    matched = False
    model = None
    if "model_register" in ident:
        raw = _decoded(ident["model_register"], image)
        raw = raw.strip() if isinstance(raw, str) else None
        if raw and _MODEL_RE.fullmatch(raw):
            model = raw
            matched = any(re.match(rx, raw) for rx in ident.get("model_regex", ()))
    elif "device_type" in regs:
        dt = _decoded(regs["device_type"], image)
        if isinstance(dt, (int, float)) and not isinstance(dt, bool):
            model = str(int(dt))
            matched = int(dt) in regs["device_type"].get("expect", ())
    rated = None
    rated_key = profile.raw.get("limits", {}).get("rated_power_register")
    if rated_key:
        v = _decoded(regs.get(rated_key), image)
        if isinstance(v, (int, float)) and RATED_POWER_RANGE_W[0] <= v <= RATED_POWER_RANGE_W[1]:
            rated = float(v)
    return {"matched": matched and not degenerate, "serial": serial, "model": model, "rated_power_w": rated}


def identity_info(profile, image: RegisterImage) -> dict[str, Any]:
    """Jawne pola tożsamości (bez seriala): `matched` (urządzenie pasuje do profilu), `model`,
    `rated_power_w` (1–30 kW, inaczej None)."""
    f = _fields(profile, image)
    return {"matched": f["matched"], "model": f["model"], "rated_power_w": f["rated_power_w"]}


def device_fingerprint(salt: bytes, profile, image: RegisterImage) -> str | None:
    """Odcisk urządzenia; None = urządzenie nie pasuje do profilu albo nie ma czym się wyróżnić."""
    if not isinstance(salt, (bytes, bytearray)) or len(salt) < MIN_SALT_BYTES:
        raise ValueError("fingerprint needs the installation salt")
    f = _fields(profile, image)
    if not f["matched"]:
        return None
    if f["serial"]:
        msg = f"{profile.id}|{f['serial']}"
    elif "serial" in profile.raw.get("identify", {}).get("registers", {}):
        # Profil ma serial, ale odczyt go nie dał (np. start falownika): tożsamość NIEZNANA —
        # odcisk z modelu różniłby się od zapisanego i dałby fałszywe „inne urządzenie”.
        return None
    elif f["model"] and f["rated_power_w"] is not None:
        msg = f"{profile.id}|{f['model']}|{round(f['rated_power_w'])}"
    else:
        return None
    return hmac.new(bytes(salt), msg.encode(), hashlib.sha256).hexdigest()[:16]
