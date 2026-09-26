"""Rozpoznanie encji profilu na kandydatach z rzeczywistej instalacji (zanonimizowanych).

Deye zostaje przy sekcji `ha` w stanie roboczym — brak jeszcze zanonimizowanych
kandydatów z rzeczywistej instalacji Deye.
"""
import json
from pathlib import Path

import pytest

from custom_components.volcast.core.entity_map import EntityCandidate, resolve_entities
from custom_components.volcast.core.profile import load_builtin

G = Path(__file__).resolve().parents[1] / "golden"
SN = "GOLDENSERIAL0000"


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
    # Wszystkie klucze zapisu rozpoznane na rzeczywistej instalacji → status "verified"
    # (rozpoznanie encji, nie próba zapisu na żywym falowniku — patrz status_note).
    p = load_builtin("goodwe-et")
    assert p.raw["ha"]["integrations"][0]["status"] == "verified"


# Encje wystawiane przez rdzenną integrację `goodwe` w HA (bez dodatku HACS): tylko
# odczyty + `operation_mode`/`grid_export_limit`/`battery_discharge_depth`. Brak
# `ems_mode`, `ems_power_limit`, `soc_upper_limit` i przełącznika limitu eksportu —
# to wystawia dopiero niestandardowa integracja HACS, na której zweryfikowano profil.
CORE_ONLY_CANDIDATES = [
    EntityCandidate("sensor.e0", "goodwe", f"goodwe-battery_soc-{SN}"),
    EntityCandidate("sensor.e1", "goodwe", f"goodwe-ppv-{SN}"),
    EntityCandidate("sensor.e2", "goodwe", f"goodwe-pbattery1-{SN}"),
    EntityCandidate("sensor.e3", "goodwe", f"goodwe-house_consumption-{SN}"),
    EntityCandidate("sensor.e4", "goodwe", f"goodwe-active_power_total-{SN}"),
    EntityCandidate("sensor.e5", "goodwe", f"goodwe-battery_temperature-{SN}"),
    EntityCandidate("select.e6", "goodwe", f"goodwe-operation_mode-{SN}"),
    EntityCandidate("number.e7", "goodwe", f"goodwe-grid_export_limit-{SN}"),
    EntityCandidate("number.e8", "goodwe", f"goodwe-battery_discharge_depth-{SN}"),
]


def test_core_goodwe_integration_misses_control_entities():
    p = load_builtin("goodwe-et")
    r = resolve_entities(p, "goodwe", CORE_ONLY_CANDIDATES)
    assert not r.ambiguous
    # Odczyty dostępne w rdzeniu HA nadal się rozpoznają...
    assert {"soc", "pv_power_w", "battery_power_w", "load_power_w", "grid_power_w",
            "battery_temp_c", "soc_min", "export_limit_w"} <= set(r.mapped)
    # ...ale klucze zapisu bez odpowiednika w rdzeniu HA zgłaszają brak, nie
    # przypadkowe dopasowanie — kontrola nie działa na rdzennej integracji.
    assert {"mode", "power_w", "soc_max", "export_limit_enabled"} <= set(r.missing)
