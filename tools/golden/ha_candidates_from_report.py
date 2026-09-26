"""Diagnostyka integracji (raport rozpoznania) → lista kandydatów encji do testów profili.

Zostawia wyłącznie: domenę encji, platformę, unique_id (z zamaskowanymi identyfikatorami),
opcje/min/max/krok/jednostkę. Bez entity_id użytkownika, stanów, nazw, hostów.

Najpierw budujemy CAŁY wynik w pamięci i sprawdzamy go; plik powstaje dopiero, gdy
nic nie wygląda na identyfikator urządzenia (numer seryjny, IP, MAC). Wykryty wyciek
albo zły raport = odmowa bez zapisu — nic nie zostaje w repo „do przejrzenia".
Użycie: python tools/golden/ha_candidates_from_report.py <diagnostyka.json> <wyjście.json>
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Prawdziwe identyfikatory (np. identyfikator wpisu konfiguracji w unique_id) —
# 32 znaki hex albo ULID/Crockford base32 na 26 znaków. Te maskujemy.
_ID = re.compile(r"(?i)\b[0-9a-f]{32}\b|\b[0-9A-HJKMNP-TV-Z]{26}\b")
# Diagnostyka HA już maskuje numer seryjny placeholderem <SN> — w złotym fixture
# podstawiamy stały fałszywy numer, spójny z resztą repo.
_SN_PLACEHOLDER = re.compile(r"<SN>")
FAKE_SERIAL = "GOLDENSERIAL0000"

# Wzorce, które w gotowym pliku oznaczają wyciek — ich NIE maskujemy, tylko odmawiamy:
# skoro przeszły maskowanie źródła, raport wymaga ręcznego przejrzenia.
_LEAKS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("niepodstawiony placeholder <SN>", re.compile(r"<SN>")),
    ("ciąg 8+ cyfr (możliwy numer seryjny)", re.compile(r"\d{8,}")),
    ("numer seryjny GoodWe", re.compile(r"\d{5}[A-Z]{3}\d{3}[A-Z]\d{4}")),
    ("adres IPv4", re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")),
    ("adres MAC", re.compile(r"(?i)(?<![0-9a-f])[0-9a-f]{2}([:-])(?:[0-9a-f]{2}\1){4}[0-9a-f]{2}(?![0-9a-f])")),
    ("adres MAC bez separatorów", re.compile(r"(?i)(?<![0-9a-z])[0-9a-f]{12}(?![0-9a-z])")),
)
# Mieszany token litery+cyfry na 10+ znaków (typowy numer seryjny innych marek) —
# sprawdzany w polach, które niosą dane instalacji (unique_id, podpowiedź), a nie w
# listach opcji z biblioteki (tam są stałe napisy w rodzaju "240VacHECO").
_TOKEN = re.compile(r"[A-Za-z0-9]{10,}")
_TOKEN_FIELDS = ("unique_id", "hint")


class ReportError(ValueError):
    """Raport nie ma oczekiwanego kształtu — nic nie zapisujemy."""


def _find_report(node):
    if isinstance(node, dict):
        if "inverters" in node and "schema" in node:
            return node
        for v in node.values():
            found = _find_report(v)
            if found:
                return found
    return None


def _mask_unique_id(unique_id: str) -> str:
    return _SN_PLACEHOLDER.sub(FAKE_SERIAL, _ID.sub("<ID>", unique_id))


def _text(e: dict, key: str, where: str) -> str:
    val = e.get(key)
    if not isinstance(val, str) or not val:
        raise ReportError(f"{where}.{key}: oczekiwano niepustego tekstu")
    return val


def build(diagnostics: object) -> list[dict]:
    """Kandydaci z raportu; zły kształt raportu → ReportError (nigdy KeyError/TypeError)."""
    report = _find_report(diagnostics)
    if report is None:
        raise ReportError("brak raportu rozpoznania w pliku")
    inverters = report.get("inverters")
    if not isinstance(inverters, list):
        raise ReportError("inverters: oczekiwano listy")
    out = []
    for n, inv in enumerate(inverters):
        ents = inv.get("entities") if isinstance(inv, dict) else None
        if not isinstance(ents, list):
            raise ReportError(f"inverters[{n}].entities: oczekiwano listy")
        for i, e in enumerate(ents):
            where = f"inverters[{n}].entities[{i}]"
            if not isinstance(e, dict):
                raise ReportError(f"{where}: oczekiwano obiektu")
            out.append({
                "entity_id": f"{_text(e, 'entity_domain', where)}.e{i}",
                "platform": _text(e, "platform", where),
                "unique_id": _mask_unique_id(_text(e, "unique_id", where)),
                "options": e.get("options"), "min": e.get("min"), "max": e.get("max"),
                "step": e.get("step"), "unit": e.get("unit"),
                "hint": e.get("translation_key") or e.get("original_name"),
            })
    return out


def find_leaks(blob: str) -> list[str]:
    """Opis każdego podejrzanego fragmentu gotowego pliku (pusta lista = czysto)."""
    problems = [f"{what}: {m.group(0)!r}" for what, rx in _LEAKS for m in rx.finditer(blob)]
    try:
        items = json.loads(blob)
    except ValueError:
        return problems + ["wynik nie jest poprawnym JSON-em"]
    fields = [c.get(f) for c in items if isinstance(c, dict) for f in _TOKEN_FIELDS] \
        if isinstance(items, list) else []
    for text in fields:
        for m in _TOKEN.finditer(text if isinstance(text, str) else ""):
            tok = m.group(0)
            if tok != FAKE_SERIAL and re.search(r"[A-Za-z]", tok) and re.search(r"\d", tok):
                problems.append(f"mieszany token 10+ znaków (możliwy numer seryjny): {tok!r}")
    return problems


def main(src: str, dst: str) -> None:
    try:
        diagnostics = json.loads(Path(src).read_text(encoding="utf-8"))
        blob = json.dumps(build(diagnostics), indent=1, ensure_ascii=False) + "\n"
    except (OSError, ValueError) as err:
        raise SystemExit(f"nie zapisuję {dst}: {err}") from err
    problems = find_leaks(blob)
    if problems:
        raise SystemExit(f"nie zapisuję {dst} — możliwy wyciek identyfikatorów, sprawdź raport ręcznie:\n"
                         + "\n".join(f"  - {p}" for p in problems))
    Path(dst).write_text(blob, encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
