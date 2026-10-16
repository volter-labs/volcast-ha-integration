"""`core/identity_refs.json` (obrazy wzorcowe profili pakietu dla kontroli nakładania się profili feedu)
musi odpowiadać profilom pakietu i `tests/golden/`.

Po zmianie profilu pakietu albo obrazu wzorcowego: `VOLCAST_WRITE_IDENTITY_REFS=1 pytest
tests/core/test_identity_refs.py` przepisuje plik (zmienna czytana wyłącznie tutaj, w teście)."""
from __future__ import annotations

import json
import os

from custom_components.volcast.core import profile_feed as pf
from custom_components.volcast.core.modbus.identity import identity_info
from custom_components.volcast.core.profile import direct_verified, load_builtin, packaged_ids
from custom_components.volcast.core.registers import FC_HOLDING, FC_INPUT
from tests.core.modbus.helpers import GOLDEN, golden_image, goodwe_image


def _image(pid: str):
    return goodwe_image() if pid == "goodwe-et" else golden_image(pid)


def expected_refs() -> dict:
    out = {}
    for pid in packaged_ids():
        profile = load_builtin(pid)
        image = _image(pid)
        spaces = image._spaces
        ident = profile.raw.get("identify") or {}
        out[pid] = {
            "holding": {str(a): w for a, w in sorted(spaces[FC_HOLDING].items())},
            "input": {str(a): w for a, w in sorted(spaces[FC_INPUT].items())},
            "model": identity_info(profile, image)["model"] if "model_register" in ident else None,
            "domains": sorted({i["domain"] for i in (profile.raw.get("ha") or {}).get("integrations") or ()}),
            "verified": direct_verified(profile),
        }
    return out


def test_identity_refs_match_the_packaged_profiles_and_golden_images():
    expected = expected_refs()
    if os.environ.get("VOLCAST_WRITE_IDENTITY_REFS") == "1":
        pf.REFS_PATH.write_text(json.dumps(expected, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    assert GOLDEN.is_dir()
    assert json.loads(pf.REFS_PATH.read_text("utf-8")) == expected, (
        "identity_refs.json is stale — rerun with VOLCAST_WRITE_IDENTITY_REFS=1")


def test_every_packaged_profile_identifies_its_own_reference_image():
    for pid in packaged_ids():
        assert identity_info(load_builtin(pid), _image(pid))["matched"], pid
