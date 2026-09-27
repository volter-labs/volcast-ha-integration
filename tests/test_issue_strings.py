"""Każdy klucz zgłoszenia naprawy użyty w kodzie ma tytuł i opis w tekstach integracji.

Bez wpisu HA pokazuje w Naprawach pusty tytuł — właściciel nie wie, że sterowanie stoi.
"""
import json
import re
from pathlib import Path

import pytest

COMPONENT = Path(__file__).parents[1] / "custom_components" / "volcast"
_KEY_PATTERNS = (
    re.compile(r'translation_key="(\w+)"'),
    re.compile(r'_create_issue\([^,]+,\s*"(\w+)"'),
)


def _issue_keys_in_code() -> set[str]:
    keys: set[str] = set()
    for path in COMPONENT.rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        if "create_issue" not in src:
            continue
        for pattern in _KEY_PATTERNS:
            keys.update(pattern.findall(src))
    return keys


def test_code_uses_the_known_issue_keys():
    assert _issue_keys_in_code() >= {"production_tracking_available", "foreign_control", "control_error"}


@pytest.mark.parametrize("path", [COMPONENT / "strings.json", COMPONENT / "translations" / "en.json"])
def test_every_issue_key_has_title_and_description(path):
    issues = json.loads(path.read_text(encoding="utf-8"))["issues"]
    for key in _issue_keys_in_code():
        assert issues.get(key, {}).get("title") and issues[key].get("description"), key
    # Tekst Napraw podaje encję, którą zmieniono (parametr przekazywany przez wykonawcę).
    assert "{entity_id}" in issues["foreign_control"]["description"]
