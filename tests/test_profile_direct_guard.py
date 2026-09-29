"""Strażnik: żaden wbudowany profil nie może mieć zweryfikowanej ścieżki rejestrów.

Tryb bezpośredni (rejestry) nie ma jeszcze hamulca do trybu neutralnego, zapisu
neutralnego przy rezerwie w czasie pauzy ani degradacji akcji przy brakujących
nastawach bezpieczeństwa. Dopóki ich nie ma, zapis rejestrów musi być nieosiągalny.
Ten test trzeba zdjąć razem z dodaniem tych zabezpieczeń w trybie bezpośrednim.
"""

import json
from pathlib import Path

PROFILES = Path(__file__).resolve().parents[1] / "custom_components" / "volcast" / "profiles"


def test_profiles_exist():
    assert list(PROFILES.glob("*.json"))


def test_no_bundled_profile_has_verified_modbus():
    verified = [
        p.name
        for p in PROFILES.glob("*.json")
        if (json.loads(p.read_text(encoding="utf-8")).get("modbus") or {}).get("status") == "verified"
    ]
    assert verified == []
