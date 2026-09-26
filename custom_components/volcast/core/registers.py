"""Rejestry ↔ wartości semantyczne według profilu. Bez wiedzy o transporcie.
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .params import Params

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
        # `undef` dotyczy wyłącznie sum liczników PV (u32) — tak jak w Boksie, f32 go nie sprawdza.
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
    """Wszystkie klucze mapy `read`. Brakujący rejestr → None (nie wyjątek).

    Cykl w `ref` (A odwołuje się do B, B do A) to błąd profilu, nie brakujący odczyt —
    zgłaszamy go głośno zamiast cicho zwracać None dla obu kluczy.
    """
    cache: dict[str, float | str | None] = {}

    def value(key: str, visiting: frozenset[str] = frozenset()) -> float | str | None:
        if key in cache:
            return cache[key]
        if key in visiting:
            raise RegisterError(f"cykl odwołań przy {key}")
        visiting = visiting | {key}
        spec = read_map[key]
        if "sum" in spec:
            total = 0.0
            for term in spec["sum"]:
                if "ref" in term:
                    part = value(term["ref"], visiting)
                    if part is not None and not isinstance(part, str):
                        part = part * term.get("sign", 1)
                else:
                    # Znak rejestru jest już policzony w `decode` — tu się go nie dubluje.
                    try:
                        part = decode(term, image)
                    except RegisterError:
                        part = None
                if part is None or isinstance(part, str):
                    cache[key] = None
                    return None
                total += part
            out: float | str | None = int(total) if float(total).is_integer() else round(total, 6)
        else:
            try:
                out = decode(spec, image)
            except RegisterError:
                out = None
        cache[key] = out
        return out

    return {k: value(k) for k in read_map}


@dataclass(frozen=True)
class RegisterWrite:
    key: str      # parametr płaski albo "tou.<i>.<pole>"
    addr: int
    value: int


def _round_clamp(v: float, hi: int) -> int:
    # Obcięcie do zakresu, potem zaokrąglenie do najbliższej liczby całkowitej.
    if not math.isfinite(v):
        raise RegisterError(f"nastawa nie jest liczbą skończoną: {v!r}")
    if v < 0:
        return 0
    if v > hi:
        return hi
    return int(v + 0.5)


def ordered_keys(params: Params, profile) -> list[str]:
    """Klucze do zapisu, w kolejności profilu (`write_policy.order`)."""
    flat = params.flatten()
    out: list[str] = []
    for group in profile.write_order:
        if group == "tou":
            for i in range(1, len(params.tou or ()) + 1):
                out.extend(f"tou.{i}.{f}" for f in profile.tou_field_order if f"tou.{i}.{f}" in flat)
        elif group in flat:
            out.append(group)
    return out


def encode_writes(params: Params, profile, keys: Iterable[str] | None = None,
                  current: RegisterImage | None = None) -> list[RegisterWrite]:
    """Koduje parametry semantyczne na zapisy rejestrów, w kolejności profilu.

    `keys` ogranicza zbiór (np. po przefiltrowaniu przez throttling/uzgadnianie),
    ale nie zmienia kolejności. Pola bitowe (np. `grid_charge`) wymagają aktualnej
    migawki rejestrów, bo zapisują pojedynczy bit bez ruszania reszty słowa.
    """
    wanted = None if keys is None else set(keys)
    spec = profile.raw["write"]
    out: list[RegisterWrite] = []
    for key in ordered_keys(params, profile):
        if wanted is not None and key not in wanted:
            continue
        if key.startswith("tou."):
            _, idx, field = key.split(".")
            i = int(idx)
            count = spec["tou_program"]["count"]
            if i > count:
                # Bez tej granicy adres nachodzi cicho na rejestry programu 1 (i - count).
                raise RegisterError(f"{key}: program {i} przekracza liczbę programów profilu ({count})")
            prog = params.tou[i - 1]
            fs = spec["tou_program"][field]
            addr = fs["addr"] + i - 1
            if field == "start":
                if not float(prog.start_min).is_integer():
                    raise RegisterError(f"{key}: start_min musi być całkowitą liczbą minut, jest {prog.start_min!r}")
                start_min = int(prog.start_min)
                value = (start_min // 60) * 100 + start_min % 60      # HHMM dziesiętnie
            elif field == "power_w":
                value = _round_clamp(prog.power_w, 65535)
            elif field == "soc":
                value = _round_clamp(prog.soc, 100)
            else:
                if current is None:
                    raise RegisterError(f"{key}: pole bitowe wymaga odczytu rejestru {addr}")
                word = current.words(addr, 1)[0]
                mask = 1 << fs["bit"]
                value = (word | mask) if prog.grid_charge else (word & ~mask & 0xFFFF)
            out.append(RegisterWrite(key, addr, value))
            continue
        s = spec[key]
        raw = getattr(params, key)
        enc = s["encode"]
        if enc == "mode":
            if raw not in profile.modes:
                raise RegisterError(f"{key}: nieznany tryb {raw!r}")
            value = profile.mode_value(raw)
        elif enc == "bool":
            value = 1 if raw else 0
        elif enc == "percent":
            value = _round_clamp(raw, 100)
        else:
            value = _round_clamp(raw, 65535)
        out.append(RegisterWrite(key, s["addr"], value))
    return out
