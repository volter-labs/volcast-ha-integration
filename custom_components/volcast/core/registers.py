"""Rejestry ↔ wartości semantyczne według profilu. Bez wiedzy o transporcie.
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .params import Params

_WORDS = {"u16": 1, "i16": 1, "u32": 2, "i32": 2, "f32": 2}
FC_HOLDING = 3          # domyślna funkcja odczytu (`fc` w specyfikacji rejestru)
FC_INPUT = 4


class RegisterError(ValueError):
    pass


class RegisterImage:
    """Migawka rejestrów: adres → słowo 16-bit (z jednego lub wielu bloków odczytu).

    Rejestry holding (FC 3) i input (FC 4) to OSOBNE przestrzenie adresów — ten sam adres
    w obu to dwa różne rejestry. `words` bez przestrzeni = holding.
    """

    def __init__(self, words: Mapping[int, int], input_words: Mapping[int, int] | None = None) -> None:
        self._spaces: dict[int, dict[int, int]] = {FC_HOLDING: dict(words), FC_INPUT: dict(input_words or {})}

    @classmethod
    def from_blocks(cls, blocks: Mapping[int | tuple[int, int], Sequence[int]]) -> "RegisterImage":
        """Klucz bloku: adres początkowy (holding) albo `(funkcja, adres)`."""
        spaces: dict[int, dict[int, int]] = {FC_HOLDING: {}, FC_INPUT: {}}
        for key, words in blocks.items():
            fc, base = key if isinstance(key, tuple) else (FC_HOLDING, key)
            if fc not in spaces:
                raise ValueError(f"nieznana funkcja odczytu {fc!r}")
            out = spaces[fc]
            for i, w in enumerate(words):
                out[base + i] = w & 0xFFFF
        return cls(spaces[FC_HOLDING], spaces[FC_INPUT])

    def words(self, addr: int, n: int, fc: int = FC_HOLDING) -> list[int]:
        space = self._spaces.get(fc)
        if space is None:
            raise RegisterError(f"nieznana funkcja odczytu {fc!r}")
        try:
            return [space[addr + i] for i in range(n)]
        except KeyError as err:
            raise RegisterError(f"brak rejestru {err.args[0]}") from err


def decode(spec: Mapping[str, Any], image: RegisterImage) -> float | str:
    typ = spec["type"]
    fc = spec.get("fc", FC_HOLDING)
    if typ == "ascii":
        raw = b"".join(w.to_bytes(2, "big") for w in image.words(spec["addr"], spec["len"], fc))
        return raw.decode("latin-1").strip(" \x00")
    words = image.words(spec["addr"], _WORDS[typ], fc)
    if spec.get("word_order") == "lo_hi":
        words = list(reversed(words))
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


def encode_tou_enable(on: bool, current_word: int, profile, owner_word: int | None = None) -> RegisterWrite:
    """Słowo włącznika harmonogramu (bit włącznika + maska dni) — odczyt-modyfikacja-zapis.

    OFF zdejmuje wyłącznie bit włącznika (dni zostają). ON dokłada bit włącznika i WSZYSTKIE dni
    maski profilu: dopóki sterujemy, plan ma działać każdego dnia (dni właściciela wracają
    z migawki przy powrocie — surowe słowo). `owner_word` zostaje dla zgodności wywołań.
    """
    spec = profile.raw["write"]["tou_enable"]
    ebit = 1 << spec["enable_bit"]
    mask = spec.get("day_mask", 0)
    word = int(current_word) & 0xFFFF
    if not on:
        return RegisterWrite("tou_enable", spec["addr"], word & ~ebit & 0xFFFF)
    return RegisterWrite("tou_enable", spec["addr"], (word | ebit | mask) & 0xFFFF)
