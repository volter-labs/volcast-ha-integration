"""Drobne utwardzenia rdzenia: skończoność po przeliczeniu jednostki, wzorzec mocy
z nazwy modelu, walidacja źródła limitów i synteza statusów profilu/integracji."""
import json
import math

import pytest

from custom_components.volcast.core.control.baseline import baseline_params
from custom_components.volcast.core.control.limits import executor_limits, rated_power_from_model
from custom_components.volcast.core.control.select import control_verified
from custom_components.volcast.core.entity_map import canonical_value, entity_value
from custom_components.volcast.core.profile import PROFILES_DIR, profile_from_dict


def goodwe_raw() -> dict:
    return json.loads((PROFILES_DIR / "goodwe-et.json").read_text(encoding="utf-8"))


def test_core_minors_finite_after_unit_conversion():
    assert canonical_value("power_w", "1e303", "MW") is None


def test_core_minors_entity_value_finite_after_unit_conversion():
    p = profile_from_dict(goodwe_raw())
    assert entity_value("power_w", "1e303", p, "goodwe", unit="MW") is None


@pytest.mark.parametrize("bad", [math.inf, -math.inf, math.nan])
def test_core_minors_baseline_rejects_non_finite(bad):
    p = profile_from_dict(goodwe_raw())
    assert baseline_params(p, {"soc_min": bad}).soc_min is None


def test_core_minors_kw_regex_ignores_kwh():
    assert rated_power_from_model("BAT-10KWH") is None


def test_core_minors_kw_regex_still_reads_kw():
    assert rated_power_from_model("GW10K-ET") == 10000.0


def test_core_minors_limits_source_validated():
    with pytest.raises(ValueError):
        executor_limits(rated_power_w=8000, source="whatever")


@pytest.mark.parametrize("source", ["profile", "registers", "entities", "user"])
def test_core_minors_limits_source_accepted(source):
    assert executor_limits(rated_power_w=8000, source=source)["source"] == source


def test_core_minors_verified_profile_draft_integration_is_false():
    raw = goodwe_raw()
    raw["status"] = "verified"
    raw["ha"]["integrations"][0]["status"] = "draft"
    assert control_verified(profile_from_dict(raw), "goodwe") is False
