"""Rozpoznanie encji profilu na kandydatach z prawdziwej instalacji (Task 17).

Deye-sg czeka na raport `b1` testera (etap 0 bramkuje tę część) — na razie tylko
GoodWe, na diagnostyce Michała.
"""
import json
from pathlib import Path

import pytest

from custom_components.volcast.core.entity_map import EntityCandidate, resolve_entities
from custom_components.volcast.core.profile import load_builtin

G = Path(__file__).resolve().parents[1] / "golden"


@pytest.mark.parametrize("profile_id,folder,required", [
    ("goodwe-et", "goodwe_et", {
        "soc", "mode", "power_w", "soc_min", "soc_max", "pv_power_w", "battery_power_w",
        "grid_power_w", "load_power_w", "battery_temp_c", "export_limit_w",
        "export_limit_enabled",
    }),
])
def test_profile_resolves_real_installation(profile_id, folder, required):
    raw = json.loads((G / folder / "ha_candidates.json").read_text())
    cands = [EntityCandidate(c["entity_id"], c["platform"], c["unique_id"]) for c in raw]
    p = load_builtin(profile_id)
    domain = p.raw["ha"]["integrations"][0]["domain"]
    r = resolve_entities(p, domain, cands)
    assert not r.ambiguous
    assert required <= set(r.mapped)


def test_goodwe_profile_ha_status_is_verified():
    # Wszystkie klucze zapisu rozpoznane z instalacji Michała → status "verified"
    # (Step 4 zadania 17); Deye zostaje "draft" do raportu b2.
    p = load_builtin("goodwe-et")
    assert p.raw["ha"]["integrations"][0]["status"] == "verified"
