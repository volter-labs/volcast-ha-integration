"""Translations must mirror en.json: same keys, same placeholders."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

TRANSLATIONS = Path(__file__).parent.parent / "custom_components" / "volcast" / "translations"
EXPECTED = ["pl", "de", "nl", "es", "fr", "uk", "pt", "pt-BR", "ro", "cs", "sk", "it"]
PLACEHOLDER = re.compile(r"\{[^{}]*\}")


def _flatten(node, prefix=""):
    out = {}
    if isinstance(node, dict):
        for key, value in node.items():
            out.update(_flatten(value, f"{prefix}.{key}" if prefix else key))
    else:
        out[prefix] = node
    return out


def _load(name):
    return _flatten(json.loads((TRANSLATIONS / f"{name}.json").read_text("utf-8")))


def problems(reference, other):
    """Return a list of human-readable differences (empty means OK)."""
    found = []
    for key in sorted(set(reference) - set(other)):
        found.append(f"missing key {key}")
    for key in sorted(set(other) - set(reference)):
        found.append(f"extra key {key}")
    for key in sorted(set(reference) & set(other)):
        if not isinstance(other[key], str) or not other[key].strip():
            found.append(f"empty or non-string {key}")
        elif sorted(PLACEHOLDER.findall(reference[key])) != sorted(
            PLACEHOLDER.findall(other[key])
        ):
            found.append(f"placeholder mismatch {key}")
    return found


def test_checker_detects_broken_sample():
    ref = {"a": "x {name}", "b": "y"}
    assert problems(ref, {"a": "x {name}", "b": "y"}) == []
    assert problems(ref, {"a": "x {nome}", "b": "y"}) == ["placeholder mismatch a"]
    assert problems(ref, {"a": "x {name}"}) == ["missing key b"]
    assert problems(ref, {"a": "x {name}", "b": "y", "c": "z"}) == ["extra key c"]


def test_all_expected_languages_present():
    present = sorted(p.stem for p in TRANSLATIONS.glob("*.json"))
    assert present == sorted(["en", *EXPECTED])


@pytest.mark.parametrize("lang", sorted(p.stem for p in TRANSLATIONS.glob("*.json")))
def test_translation_mirrors_english(lang):
    assert problems(_load("en"), _load(lang)) == []


def test_strings_json_matches_english():
    assert (TRANSLATIONS.parent / "strings.json").read_text("utf-8") == (
        TRANSLATIONS / "en.json"
    ).read_text("utf-8")
