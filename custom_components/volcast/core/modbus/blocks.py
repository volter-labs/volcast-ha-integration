"""Plan bloków odczytu z map profilu — które słowa czytać i jak je złożyć w ramki.

Słowa niepotrzebne nie są czytane poza krótkimi dziurami między potrzebnymi (`max_gap`),
a blok nigdy nie przekracza limitu rejestrów na ramkę z profilu. Rejestry identyfikacyjne
(w tym numer seryjny) trafiają do planu wyłącznie na żądanie (`include_identify`).

Rejestry input (`fc: 4`) to osobna przestrzeń adresów: bloki planuje się per funkcja odczytu i nigdy
nie scala bloku holding z blokiem input. Blok to `(adres, liczba)` dla holding (FC 3 — postać sprzed
FC 4, bez zmian dla istniejących profili) albo `(adres, liczba, 4)` dla input; `block_fc` i `block_key`
rozkładają obie postacie.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from ..registers import FC_HOLDING, FC_INPUT

Block = tuple[int, ...]

_WORDS = {"u16": 1, "i16": 1, "u32": 2, "i32": 2, "f32": 2}
_TOU_REG_FIELDS = ("start", "power_w", "soc", "grid_charge")


def spec_addresses(spec: Mapping[str, Any]) -> list[int]:
    """Słowa zajęte przez specyfikację rejestru (składniki `sum` tak, odwołania `ref` nie)."""
    if "sum" in spec:
        out: list[int] = []
        for term in spec["sum"]:
            if "ref" not in term:
                out.extend(spec_addresses(term))
        return out
    typ = spec.get("type", "u16")
    n = spec["len"] if typ == "ascii" else _WORDS[typ]
    return list(range(spec["addr"], spec["addr"] + n))


def block_fc(block: Block) -> int:
    """Funkcja odczytu bloku: trzeci element albo 3 (holding)."""
    return block[2] if len(block) > 2 else FC_HOLDING


def make_block(addr: int, count: int, fc: int = FC_HOLDING) -> Block:
    return (addr, count) if fc == FC_HOLDING else (addr, count, fc)


def block_key(block: Block) -> int | tuple[int, int]:
    """Klucz bloku w `RegisterImage.from_blocks`: adres (holding) albo `(funkcja, adres)`."""
    fc = block_fc(block)
    return block[0] if fc == FC_HOLDING else (fc, block[0])


def read_kwargs(block: Block) -> dict[str, int]:
    """Argument `fc` dla `transport.read` — wyłącznie dla input; odczyt holding woła transport
    dokładnie jak przed FC 4 (transporty i atrapy bez parametru `fc` działają bez zmian)."""
    fc = block_fc(block)
    return {} if fc == FC_HOLDING else {"fc": fc}


def plan_blocks(addrs: Iterable[int], max_count: int, max_gap: int = 16) -> list[tuple[int, int]]:
    """Posortowane słowa → bloki (start, liczba); dziura ≤ `max_gap` słów jest doczytywana."""
    if max_count < 1:
        raise ValueError("max_count musi być >= 1")
    blocks: list[tuple[int, int]] = []
    start = last = None
    for a in sorted(set(addrs)):
        if start is not None and a - last - 1 <= max_gap and a - start + 1 <= max_count:
            last = a
            continue
        if start is not None:
            blocks.append((start, last - start + 1))
        start = last = a
    if start is not None:
        blocks.append((start, last - start + 1))
    return blocks


def _named_runs(profile, *, include_write_keys: bool = True, include_tou: bool = True,
                include_identify: bool = False) -> list[tuple[str, int, int, int]]:
    """Ciągłe zakresy słów per klucz (a dla składników sumy — per składnik):
    (klucz, start, liczba, funkcja odczytu).

    Klucze TOU (pola programów i włącznik) mają nazwę `tou`; identyfikacja — `identify`.
    Rejestry zapisu (i ich odczyt zwrotny) są zawsze holding.
    """
    runs: list[tuple[str, int, int, int]] = []

    def add(key: str, addrs: list[int], fc: int = FC_HOLDING) -> None:
        if addrs:
            runs.append((key, addrs[0], len(addrs), fc))

    for key, spec in profile.raw["read"].items():
        if key == "tou_enabled" and not include_tou:
            continue
        name = "tou" if key == "tou_enabled" else key
        if "sum" in spec:
            for term in spec["sum"]:
                if "ref" not in term:
                    add(name, spec_addresses(term), term.get("fc", FC_HOLDING))
        else:
            add(name, spec_addresses(spec), spec.get("fc", FC_HOLDING))
    write = profile.raw["write"]
    if include_write_keys:
        for key, spec in write.items():
            if key not in ("tou_program", "tou_enable"):
                add(key, spec_addresses(spec))
    if include_tou and "tou_program" in write:
        tp = write["tou_program"]
        for field in _TOU_REG_FIELDS:
            runs.append(("tou", tp[field]["addr"], tp["count"], FC_HOLDING))
        if "tou_enable" in write:
            runs.append(("tou", write["tou_enable"]["addr"], 1, FC_HOLDING))
    if include_identify:
        runs.extend(("identify", b[0], b[1], block_fc(b)) for b in profile.modbus.identify_reads)
    return runs


def _key_runs(profile, fc: int = FC_HOLDING, **kw) -> list[tuple[int, int]]:
    return [(start, n) for _, start, n, f in _named_runs(profile, **kw) if f == fc]


def read_plan(profile, *, include_write_keys: bool = True, include_tou: bool = True,
              include_identify: bool = False, exclude: Iterable[str] = ()) -> list[Block]:
    """Bloki odczytu: najpierw holding, potem input. `exclude` — klucze pomijane (np. rejestry
    bez odczytu: odpowiedź niepoprawnej długości przy każdym odpytaniu to strata czasu łącza
    i fałszywe obce ramki); `tou` pomija wszystkie pola programów i włącznik. Słowo potrzebne
    innemu kluczowi zostaje."""
    skip = set(exclude)
    runs = _named_runs(profile, include_write_keys=include_write_keys, include_tou=include_tou,
                       include_identify=include_identify)
    out: list[Block] = []
    for fc in (FC_HOLDING, FC_INPUT):
        keep = {a for k, start, n, f in runs if f == fc and k not in skip for a in range(start, start + n)}
        out.extend(make_block(a, n, fc) for a, n in plan_blocks(keep, profile.modbus.max_read_registers))
    return out


def split_block(block: Block, profile) -> list[Block]:
    """Blok scalony przez dziurę → osobne bloki per klucz (gdy dziura jest nieczytelna).

    Zakres klucza częściowo poza blokiem jest przycinany do bloku; bez identyfikacji. Podbloki
    mają funkcję odczytu bloku (zakresy kluczy innej przestrzeni adresów są pomijane).
    """
    b0, bn, fc = block[0], block[1], block_fc(block)
    b1 = b0 + bn
    out: set[Block] = set()
    for start, n in _key_runs(profile, fc):
        s, e = max(start, b0), min(start + n, b1)
        if s < e:
            out.add(make_block(s, e - s, fc))
    return sorted(out)
