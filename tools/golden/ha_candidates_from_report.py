"""Diagnostyka integracji (raport rozpoznania) → lista kandydatów encji do testów profili.

Zostawia wyłącznie: domenę encji, platformę, unique_id (z zamaskowanymi identyfikatorami),
opcje/min/max/krok/jednostkę. Bez entity_id użytkownika, stanów, nazw, hostów.
Użycie: python tools/golden/ha_candidates_from_report.py <diagnostyka.json> <wyjście.json>
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Prawdziwe identyfikatory (np. niezamaskowany numer seryjny znaleziony w dzikim
# raporcie) — 32 znaki hex albo ULID/Crockford base32 na 26 znaków.
_ID = re.compile(r"(?i)\b[0-9a-f]{32}\b|\b[0-9A-HJKMNP-TV-Z]{26}\b")
# Diagnostyka HA już maskuje numer seryjny placeholderem <SN> (patrz identifiers) —
# w złotym fixture podstawiamy stały fałszywy numer, spójny z resztą repo
# (tools/golden/import_box_fixtures.py, testy modbus).
_SN_PLACEHOLDER = re.compile(r"<SN>")
FAKE_SERIAL = "GOLDENSERIAL0000"


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


def main(src: str, dst: str) -> None:
    report = _find_report(json.loads(Path(src).read_text()))
    if report is None:
        raise SystemExit("brak raportu rozpoznania w pliku")
    out = []
    for inv in report["inverters"]:
        for i, e in enumerate(inv["entities"]):
            out.append({
                "entity_id": f"{e['entity_domain']}.e{i}",
                "platform": e["platform"],
                "unique_id": _mask_unique_id(e["unique_id"]),
                "options": e.get("options"), "min": e.get("min"), "max": e.get("max"),
                "step": e.get("step"), "unit": e.get("unit"),
                "hint": e.get("translation_key") or e.get("original_name"),
            })
    Path(dst).write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    blob = Path(dst).read_text()
    # Sam plik nie może już zawierać placeholdera <SN> po podstawieniu, a żaden
    # ciąg 8+ cyfr pod rząd (możliwy numer seryjny) nie mógł się prześlizgnąć.
    assert not re.search(r"\d{8,}", blob), "możliwy numer seryjny — sprawdź ręcznie"


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
