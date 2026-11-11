"""Rejestry ↔ wartości semantyczne według profilu. Bez wiedzy o transporcie.
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field, replace
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
    w obu to dwa różne rejestry. `words` bez przestrzeni = holding. Tak samo osobne są przestrzenie
    urządzeń: słowa czytane z własnym adresem urządzenia (`unit_id` rejestru profilu) leżą pod
    `(funkcja, adres urządzenia)`; `unit` None = adres połączenia (jedyna przestrzeń dotychczas).
    """

    def __init__(self, words: Mapping[int, int], input_words: Mapping[int, int] | None = None,
                 units: Mapping[tuple[int, int], Mapping[int, int]] | None = None) -> None:
        self._spaces: dict[int, dict[int, int]] = {FC_HOLDING: dict(words), FC_INPUT: dict(input_words or {})}
        # przestrzenie rejestrów czytanych z własnym adresem urządzenia: (funkcja, adres urządzenia) → słowa
        self._unit_spaces: dict[tuple[int, int], dict[int, int]] = {}
        for (fc, unit), space in (units or {}).items():
            if fc not in (FC_HOLDING, FC_INPUT):
                raise ValueError(f"nieznana funkcja odczytu {fc!r}")
            self._unit_spaces[(fc, unit)] = dict(space)

    @classmethod
    def from_blocks(cls, blocks: Mapping[int | tuple[int, ...], Sequence[int]]) -> "RegisterImage":
        """Klucz bloku: adres początkowy (holding), `(funkcja, adres)` albo `(funkcja, adres, urządzenie)`."""
        spaces: dict[tuple[int, int | None], dict[int, int]] = {(FC_HOLDING, None): {}, (FC_INPUT, None): {}}
        for key, words in blocks.items():
            if isinstance(key, tuple):
                fc, base, unit = (*key, None) if len(key) == 2 else key
            else:
                fc, base, unit = FC_HOLDING, key, None
            if fc not in (FC_HOLDING, FC_INPUT):
                raise ValueError(f"nieznana funkcja odczytu {fc!r}")
            out = spaces.setdefault((fc, unit), {})
            for i, w in enumerate(words):
                out[base + i] = w & 0xFFFF
        units = {k: v for k, v in spaces.items() if k[1] is not None}
        return cls(spaces[(FC_HOLDING, None)], spaces[(FC_INPUT, None)], units)

    def words(self, addr: int, n: int, fc: int = FC_HOLDING, unit: int | None = None) -> list[int]:
        if fc not in (FC_HOLDING, FC_INPUT):
            raise RegisterError(f"nieznana funkcja odczytu {fc!r}")
        space = self._spaces[fc] if unit is None else self._unit_spaces.get((fc, unit), {})
        try:
            return [space[addr + i] for i in range(n)]
        except KeyError as err:
            raise RegisterError(f"brak rejestru {err.args[0]}") from err


def decode(spec: Mapping[str, Any], image: RegisterImage) -> float | str:
    """Wartość rejestru; rejestr z własnym `unit_id` wyłącznie z bloków czytanych z tego adresu."""
    typ = spec["type"]
    fc = spec.get("fc", FC_HOLDING)
    unit = spec.get("unit_id")
    if typ == "ascii":
        raw = b"".join(w.to_bytes(2, "big") for w in image.words(spec["addr"], spec["len"], fc, unit))
        return raw.decode("latin-1").strip(" \x00")
    words = image.words(spec["addr"], _WORDS[typ], fc, unit)
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
    """Zapis jednego klucza. `value` to surowa liczba całkowita rejestru — słowo 16-bit albo, przy
    `count` 2 (`type: u32`), liczba 32-bit dzielona na słowa wg `word_order`. `unit_id` None = adres
    urządzenia połączenia. Pola domyślne = dotychczasowy zapis jednego słowa (równość bez zmian)."""
    key: str      # parametr płaski albo "tou.<i>.<pole>"
    addr: int
    value: int
    count: int = 1
    word_order: str = "hi_lo"
    unit_id: int | None = None
    # przełącznik `on_off`: włączenie (True) / wyłączenie (False); None = inny klucz. Poza równością —
    # o kolejności wstrzymań decyduje sekwencja zapisów (`write_sequence`), nie treść ramki.
    turns_on: bool | None = field(default=None, compare=False)

    def words(self) -> list[int]:
        """Słowa ramki zapisu w kolejności adresów; wartość spoza zakresu → RegisterError."""
        return split_words(self.value, self.count, self.word_order)


def split_words(value: int, count: int, word_order: str = "hi_lo") -> list[int]:
    if count not in (1, 2) or word_order not in ("hi_lo", "lo_hi"):
        raise RegisterError(f"nieobsługiwany układ słów: {count} / {word_order!r}")
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 1 << (16 * count):
        raise RegisterError(f"wartość {value!r} poza zakresem {count} słów")
    words = [(value >> (16 * i)) & 0xFFFF for i in reversed(range(count))]      # starsze słowo pierwsze
    return list(reversed(words)) if word_order == "lo_hi" else words


def join_words(words: Sequence[int], word_order: str = "hi_lo") -> int:
    """Odwrotność `split_words`: słowa w kolejności adresów → surowa liczba."""
    ordered = list(reversed(words)) if word_order == "lo_hi" else list(words)
    out = 0
    for w in ordered:
        out = (out << 16) | (int(w) & 0xFFFF)
    return out


def write_layout(spec: Mapping[str, Any]) -> tuple[int, str, int | None]:
    """Układ rejestru zapisu z profilu: (liczba słów, kolejność słów, adres urządzenia albo None)."""
    count = 2 if spec.get("type") == "u32" else 1
    return count, spec.get("word_order", "hi_lo"), spec.get("unit_id")


def write_scale(spec: Mapping[str, Any]) -> float:
    """Skala rejestru zapisu (wartość = surowa × skala, jak przy odczycie); brak = 1."""
    return spec.get("scale", 1)


def _round_clamp(v: float, hi: int) -> int:
    # Obcięcie do zakresu, potem zaokrąglenie do najbliższej liczby całkowitej.
    if not math.isfinite(v):
        raise RegisterError(f"nastawa nie jest liczbą skończoną: {v!r}")
    if v < 0:
        return 0
    if v > hi:
        return hi
    return int(v + 0.5)


# Strona bezpieczna zaokrąglenia nastawy ze skalą: -1 = w dół (moc, limit eksportu, górny próg),
# +1 = w górę (dolny próg). Wartość niecałkowita w jednostkach rejestru nie może dać więcej mocy niż plan.
_SCALED_ROUNDING = {"power_w": -1, "charge_power_w": -1, "discharge_power_w": -1, "export_limit_w": -1,
                    "soc_max": -1, "soc_min": 1}
_DIRECTION_KEYS = {"charge": "charge_power_w", "discharge": "discharge_power_w"}


def _scaled_raw(key: str, v: float, scale: float, hi: int, value_hi: float | None) -> int:
    """Surowa wartość nastawy ze skalą: najpierw zakres w jednostkach wartości (np. 0–100 %), potem
    podział przez skalę, zaokrąglenie w stronę bezpieczną klucza i zakres typu rejestru."""
    if not math.isfinite(v):
        raise RegisterError(f"nastawa nie jest liczbą skończoną: {v!r}")
    if value_hi is not None:
        v = min(v, value_hi)
    x = max(v, 0.0) / scale
    near = round(x)
    if math.isclose(x, near, rel_tol=1e-9, abs_tol=1e-9):
        raw = int(near)                                   # 0,3 / 0,1 = 2,9999… → 3
    elif _SCALED_ROUNDING.get(key, -1) > 0:
        raw = math.ceil(x)
    else:
        raw = math.floor(x)
    return max(0, min(raw, hi))


def direction_params(params: Params, profile) -> Params:
    """Profil z `write_policy.power_by_direction`: moc planu (`power_w`) trafia do nastawy zgodnej
    z kierunkiem trybu (`charge_power_w` / `discharge_power_w`); drugiej nastawy nie ruszamy, tryb
    bez kierunku (neutralny, postój) nie pisze żadnej. Bez trybu w parametrach kierunek jest nieznany —
    RegisterError (nic nie wysłane), tak samo przy sprzecznej jawnej nastawie tego kierunku."""
    if not getattr(profile, "power_by_direction", False) or params.power_w is None:
        return params
    if params.mode is None or params.mode not in profile.modes:
        raise RegisterError(f"power_w: kierunek nastawy nieznany bez trybu z profilu (tryb {params.mode!r})")
    key = _DIRECTION_KEYS.get(profile.mode_direction(params.mode))
    changes: dict[str, Any] = {"power_w": None}
    if key is not None:
        explicit = getattr(params, key)
        if explicit is not None and not math.isclose(float(explicit), float(params.power_w)):
            raise RegisterError(f"{key}: nastawa {explicit!r} sprzeczna z mocą planu {params.power_w!r}")
        changes[key] = params.power_w
    return replace(params, **changes)


def _direction_keys(wanted: set[str], params: Params, profile) -> set[str]:
    """Filtr kluczy w konwencji planu: `power_w` obejmuje nastawę kierunku trybu."""
    if "power_w" not in wanted:
        return wanted
    key = _DIRECTION_KEYS.get(profile.mode_direction(params.mode)) if params.mode in profile.modes else None
    return (wanted - {"power_w"}) | ({key} if key is not None else set())


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
    if getattr(profile, "power_by_direction", False):
        if wanted is not None:
            wanted = _direction_keys(wanted, params, profile)
        params = direction_params(params, profile)
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
        count, order, unit = write_layout(s)
        if count == 2 and s["encode"] not in ("watts", "percent"):
            raise RegisterError(f"{key}: zapis 32-bitowy tylko dla nastaw liczbowych")
        hi = 0xFFFFFFFF if count == 2 else 0xFFFF
        scale = write_scale(s)
        raw = getattr(params, key)
        enc = s["encode"]
        if enc == "mode":
            if raw not in profile.modes:
                raise RegisterError(f"{key}: nieznany tryb {raw!r}")
            value = profile.mode_value(raw)
        elif enc == "bool":
            value = 1 if raw else 0
        elif enc == "on_off":
            value = s["on"] if raw else s["off"]
        elif scale != 1:
            value = _scaled_raw(key, float(raw), scale, hi, 100.0 if enc == "percent" else None)
        elif enc == "percent":
            value = _round_clamp(raw, 100)
        else:
            value = _round_clamp(raw, hi)
        out.append(RegisterWrite(key, s["addr"], value, count=count, word_order=order, unit_id=unit,
                                 turns_on=bool(raw) if enc == "on_off" else None))
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
