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


def test_direct_connection_texts_present():
    s = _load("strings.json")
    opts = s["options"]
    assert "control_direct" in opts["step"]["control"]["menu_options"]
    for reason in ("direct_unverified", "direct_conflict", "direct_not_found", "direct_in_use"):
        assert reason in opts["abort"], reason
    for step in ("direct_search", "direct_pick", "direct_manual"):
        assert step in opts["step"], step
    for err in ("trial_with_entities", "trial_while_owned", "invalid_host", "logger_serial_required"):
        assert err in opts["error"], err
    for issue in ("direct_conflict", "foreign_control_direct", "nvm_budget", "direct_identity_changed",
                  "tou_snapshot_lost"):
        assert issue in s["issues"], issue
    assert "{reason}" in s["issues"]["direct_conflict"]["description"]
    assert "{setting}" in s["issues"]["foreign_control_direct"]["description"]
    for key in ("soc", "pv_power_w", "grid_power_w", "pv_energy_total_kwh", "mode", "link_quality"):
        assert f"control_direct_{key}" in s["entity"]["sensor"], key


def test_release_note_and_readme_describe_direct_access_honestly():
    import re
    repo = ROOT.parents[1]
    note = (repo / "docs" / "release-notes" / "v2.0.0-beta3.md").read_text(encoding="utf-8")
    readme = (repo / "README.md").read_text(encoding="utf-8")
    assert "Direct connection (beta)" in readme
    for text in (note, readme):
        assert "read-only" in text and ("draft" in text.lower() or "not verified" in text.lower())
    assert "turn off the Volcast control switch" in note
    for text in (note,):
        assert not re.search(r"D1\d\d|[Tt]ask \d|etap|zadani|PLAN-\d|G[123]\b", text)
