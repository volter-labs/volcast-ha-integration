"""strings.json i translations/en.json muszą być identyczne.

Home Assistant czyta translations/en.json w runtime dla custom integration,
więc rozjazd względem strings.json oznacza brakujące etykiety u użytkowników.
"""

import json
from pathlib import Path

COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "volcast"


def test_en_translations_match_strings():
    strings = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
    en = json.loads((COMPONENT / "translations" / "en.json").read_text(encoding="utf-8"))
    assert en == strings
