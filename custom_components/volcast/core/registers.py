"""Rejestry ↔ wartości semantyczne według profilu. Bez wiedzy o transporcie.
"""
from __future__ import annotations

import struct
from typing import Any, Mapping, Sequence

_WORDS = {"u16": 1, "i16": 1, "u32": 2, "i32": 2, "f32": 2}


class RegisterError(ValueError):
    pass


class RegisterImage:
    """Migawka rejestrów: adres → słowo 16-bit (z jednego lub wielu bloków odczytu)."""

    def __init__(self, words: Mapping[int, int]) -> None:
        self._w = dict(words)

    @classmethod
    def from_blocks(cls, blocks: Mapping[int, Sequence[int]]) -> "RegisterImage":
        out: dict[int, int] = {}
        for base, words in blocks.items():
            for i, w in enumerate(words):
                out[base + i] = w & 0xFFFF
        return cls(out)

    def words(self, addr: int, n: int) -> list[int]:
        try:
            return [self._w[addr + i] for i in range(n)]
        except KeyError as err:
            raise RegisterError(f"brak rejestru {err.args[0]}") from err


def decode(spec: Mapping[str, Any], image: RegisterImage) -> float | str:
    typ = spec["type"]
    if typ == "ascii":
        raw = b"".join(w.to_bytes(2, "big") for w in image.words(spec["addr"], spec["len"]))
        return raw.decode("latin-1").strip(" \x00")
    words = image.words(spec["addr"], _WORDS[typ])
    if typ == "f32":
        value: float = struct.unpack(">f", b"".join(w.to_bytes(2, "big") for w in words))[0]
    else:
        raw_int = words[0] if len(words) == 1 else (words[0] << 16) | words[1]
        if "undef" in spec and raw_int == spec["undef"]:
            # Brak podłączonego łańcucha PV bywa zgłaszany jako wartość „niezdefiniowana" — traktujemy jak 0.
            return 0
        bits = 16 * len(words)
        if typ.startswith("i") and raw_int >= 1 << (bits - 1):
            raw_int -= 1 << bits
        value = raw_int
    value = value * spec.get("scale", 1) * spec.get("sign", 1)
    if isinstance(value, float):
        value = round(value, 6)
        if value.is_integer():
            value = int(value) if typ != "f32" else value
    return value


def read_values(read_map: Mapping[str, Any], image: RegisterImage) -> dict[str, float | str | None]:
    """Wszystkie klucze mapy `read`. Brakujący rejestr → None (nie wyjątek)."""
    cache: dict[str, float | str | None] = {}

    def value(key: str, depth: int = 0) -> float | str | None:
        if key in cache:
            return cache[key]
        if depth > 8:
            raise RegisterError(f"cykl odwołań przy {key}")
        spec = read_map[key]
        try:
            if "sum" in spec:
                total = 0.0
                for term in spec["sum"]:
                    part = value(term["ref"], depth + 1) if "ref" in term else decode(term, image)
                    if part is None or isinstance(part, str):
                        cache[key] = None
                        return None
                    total += part * term.get("sign", 1)
                out: float | str | None = int(total) if float(total).is_integer() else round(total, 6)
            else:
                out = decode(spec, image)
        except RegisterError:
            out = None
        cache[key] = out
        return out

    return {k: value(k) for k in read_map}
