"""Teksty integracji: encje sterowania, klucze wymagane w konfiguracji i opcjach, zakaz identyfikatorów wewnętrznych."""
import json
from pathlib import Path

ROOT = Path(__file__).parents[1] / "custom_components" / "volcast"


def _load(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def test_en_translation_equals_strings():
    assert _load("strings.json") == _load("translations/en.json")


def test_required_keys_present():
    s = _load("strings.json")
    for reason in ("pairing_expired", "pairing_disabled", "pairing_failed", "paired_existing",
                   "multiple_accounts", "existing_entry_disabled", "converted_existing"):
        assert reason in s["config"]["abort"], reason
    assert "pair" in s["config"]["step"]["user"]["menu_options"]
    for step in ("forecast", "control", "details", "prices"):
        assert step in s["options"]["step"], step
    assert "entity_mode_unavailable" in s["options"]["abort"]
    for issue in ("control_error", "foreign_control"):
        assert issue in s["issues"], issue
    for ent in ("control_plan", "control_status"):
        assert ent in s["entity"]["sensor"], ent
    assert "control_switch" in s["entity"]["switch"]


def test_no_internal_identifiers_in_user_texts():
    import re
    text = (ROOT / "strings.json").read_text(encoding="utf-8")
    assert not re.search(r"D1\d\d|[Tt]ask \d|etap|zadani", text)
