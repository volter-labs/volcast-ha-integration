"""Tożsamość urządzenia z rejestrów identyfikacyjnych — odcisk z solą instalacji.

Numer seryjny nigdy nie wychodzi poza tę funkcję w postaci jawnej: odcisk to
HMAC-SHA256(sól, profil|serial)[:16] (bez seriala: profil|model|moc). Zwykły skrót seriala
dałoby się odwrócić (mała entropia numerów seryjnych), dlatego sól jest wymagana.
"""
from __future__ import annotations

import hashlib
import hmac
from typing import Any, Mapping

from ..registers import RegisterError, RegisterImage, decode

RATED_POWER_RANGE_W = (1000.0, 30000.0)


def _decoded(spec: Mapping[str, Any] | None, image: RegisterImage):
    if spec is None:
        return None
    try:
        return decode(spec, image)
    except (RegisterError, KeyError, ValueError):
        return None


def identity_fields(profile, image: RegisterImage) -> dict[str, Any]:
    """`serial` (tylko do odcisku!), `model`, `rated_power_w` (1–30 kW, inaczej None)."""
    ident = profile.raw.get("identify", {})
    regs = ident.get("registers", {})
    serial = _decoded(regs.get("serial"), image)
    serial = serial.strip() if isinstance(serial, str) and serial.strip() else None
    model = _decoded(ident.get("model_register"), image)
    if isinstance(model, str):
        model = model.strip() or None
    elif "device_type" in regs:
        dt = _decoded(regs["device_type"], image)
        model = None if dt is None or isinstance(dt, str) else str(int(dt))
    else:
        model = None
    rated = None
    rated_key = profile.raw.get("limits", {}).get("rated_power_register")
    if rated_key:
        v = _decoded(regs.get(rated_key), image)
        if isinstance(v, (int, float)) and RATED_POWER_RANGE_W[0] <= v <= RATED_POWER_RANGE_W[1]:
            rated = float(v)
    return {"serial": serial, "model": model, "rated_power_w": rated}


def device_fingerprint(salt: bytes, profile_id: str, fields: Mapping[str, Any]) -> str:
    if not isinstance(salt, (bytes, bytearray)) or not salt:
        raise ValueError("fingerprint needs the installation salt")
    serial = fields.get("serial")
    if serial:
        msg = f"{profile_id}|{serial}"
    else:
        rated = fields.get("rated_power_w")
        msg = f"{profile_id}|{fields.get('model') or ''}|{'' if rated is None else round(rated)}"
    return hmac.new(bytes(salt), msg.encode(), hashlib.sha256).hexdigest()[:16]
