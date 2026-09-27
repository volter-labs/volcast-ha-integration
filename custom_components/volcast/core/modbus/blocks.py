"""Plan bloków odczytu z map profilu — które słowa czytać i jak je złożyć w ramki.

Słowa niepotrzebne nie są czytane poza krótkimi dziurami między potrzebnymi (`max_gap`),
a blok nigdy nie przekracza limitu rejestrów na ramkę z profilu. Rejestry identyfikacyjne
(w tym numer seryjny) trafiają do planu wyłącznie na żądanie (`include_identify`).
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

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
                include_identify: bool = False) -> list[tuple[str, int, int]]:
    """Ciągłe zakresy słów per klucz (a dla składników sumy — per składnik): (klucz, start, liczba).

    Klucze TOU (pola programów i włącznik) mają nazwę `tou`; identyfikacja — `identify`.
    """
    runs: list[tuple[str, int, int]] = []

    def add(key: str, addrs: list[int]) -> None:
        if addrs:
            runs.append((key, addrs[0], len(addrs)))

    for key, spec in profile.raw["read"].items():
        if key == "tou_enabled" and not include_tou:
            continue
        name = "tou" if key == "tou_enabled" else key
        if "sum" in spec:
            for term in spec["sum"]:
                if "ref" not in term:
                    add(name, spec_addresses(term))
        else:
            add(name, spec_addresses(spec))
    write = profile.raw["write"]
    if include_write_keys:
        for key, spec in write.items():
            if key not in ("tou_program", "tou_enable"):
                add(key, spec_addresses(spec))
    if include_tou and "tou_program" in write:
        tp = write["tou_program"]
        for field in _TOU_REG_FIELDS:
            runs.append(("tou", tp[field]["addr"], tp["count"]))
        if "tou_enable" in write:
            runs.append(("tou", write["tou_enable"]["addr"], 1))
    if include_identify:
        runs.extend(("identify", a, n) for a, n in profile.modbus.identify_reads)
    return runs


def _key_runs(profile, **kw) -> list[tuple[int, int]]:
    return [(start, n) for _, start, n in _named_runs(profile, **kw)]


def read_plan(profile, *, include_write_keys: bool = True, include_tou: bool = True,
              include_identify: bool = False, exclude: Iterable[str] = ()) -> list[tuple[int, int]]:
    """Bloki odczytu. `exclude` — klucze pomijane (np. rejestry bez odczytu: odpowiedź
    niepoprawnej długości przy każdym odpytaniu to strata czasu łącza i fałszywe obce ramki);
    `tou` pomija wszystkie pola programów i włącznik. Słowo potrzebne innemu kluczowi zostaje."""
    skip = set(exclude)
    runs = [(k, start, n) for k, start, n in _named_runs(
        profile, include_write_keys=include_write_keys, include_tou=include_tou,
        include_identify=include_identify)]
    keep = {a for k, start, n in runs if k not in skip for a in range(start, start + n)}
    return plan_blocks(keep, profile.modbus.max_read_registers)


def split_block(block: tuple[int, int], profile) -> list[tuple[int, int]]:
    """Blok scalony przez dziurę → osobne bloki per klucz (gdy dziura jest nieczytelna).

    Zakres klucza częściowo poza blokiem jest przycinany do bloku; bez identyfikacji.
    """
    b0, bn = block
    b1 = b0 + bn
    out: set[tuple[int, int]] = set()
    for start, n in _key_runs(profile):
        s, e = max(start, b0), min(start + n, b1)
        if s < e:
            out.add((s, e - s))
    return sorted(out)
