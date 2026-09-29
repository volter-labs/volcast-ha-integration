import pytest

from custom_components.volcast.core.control.caps import capabilities_for, entity_mode_ready, missing_write_keys
from custom_components.volcast.core.control.select import ProfileChoice
from custom_components.volcast.core.control.limits import executor_limits, rated_power_from_model
from custom_components.volcast.core.profile import load_builtin

GW = load_builtin("goodwe-et")
ALL = ("mode", "power_w", "soc_min", "soc_max", "export_limit_w", "export_limit_enabled")
CAPS = {"force_charge_from_grid", "sell_from_battery", "force_discharge", "standby",
        "set_power_w", "limit_export", "set_soc_floor", "set_soc_ceiling"}


def test_all_mapped_gives_profile_capabilities():
    caps = capabilities_for(GW, ALL)
    assert all(caps.values()) and set(caps) == CAPS


def test_missing_mode_drops_every_capability():
    # Bez encji trybu nie ma sterowania wcale.
    caps = capabilities_for(GW, [k for k in ALL if k != "mode"])
    assert set(caps) == CAPS and not any(caps.values())


@pytest.mark.parametrize("missing,dropped", [
    ("soc_max", {"set_soc_ceiling"}),
    ("soc_min", {"set_soc_floor"}),
    ("export_limit_w", {"limit_export"}),
    ("export_limit_enabled", {"limit_export"}),
    ("power_w", {"set_power_w", "force_charge_from_grid", "sell_from_battery", "force_discharge",
                 "standby"}),
])
def test_missing_setting_drops_only_its_capabilities(missing, dropped):
    # Nastawa bez encji jest nieobsługiwana — chmura nie planuje z nią, reszta zostaje.
    caps = capabilities_for(GW, [k for k in ALL if k != missing])
    assert {k for k, v in caps.items() if not v} == dropped


def test_entity_mode_ready_needs_only_the_mode_entity():
    choice = ProfileChoice(GW, "goodwe", None)
    assert entity_mode_ready(choice, [k for k in ALL if k != "soc_max"]) is True
    assert entity_mode_ready(choice, [k for k in ALL if k != "mode"]) is False


def test_missing_write_keys_in_profile_order():
    assert missing_write_keys(GW, ALL) == ()
    assert missing_write_keys(GW, ("mode", "power_w")) == (
        "soc_min", "soc_max", "export_limit_w", "export_limit_enabled")
    assert missing_write_keys(GW, []) == tuple(GW.raw["write_policy"]["order"])


def test_undeclared_capability_stays_false():
    deye = load_builtin("deye-sg")
    caps = capabilities_for(deye, ("tou",))
    assert caps["sell_from_battery"] is False and caps["limit_export"] is False


@pytest.mark.parametrize("model,watts", [
    ("GW8K-ET", 8000.0), ("GW8KN-ET", 8000.0), ("GW29.9K-ET", 29900.0),
    ("SUN-10K-SG04LP3-EU", 10000.0), ("GW5048D-ES", None), (None, None), ("GW99K-ET", None),
    ("GW50K-ET", None),
])
def test_rated_power_from_model(model, watts):
    assert rated_power_from_model(model) == watts


def test_executor_limits_bounds_and_source():
    assert executor_limits(rated_power_w=8000.0) == {"rated_power_w": 8000, "source": "entities"}
    assert executor_limits(rated_power_w=8000.4, battery_capacity_kwh=10.237, source="user") == {
        "rated_power_w": 8000, "battery_capacity_kwh": 10.24, "source": "user"}
    assert executor_limits(rated_power_w=None) is None
    assert executor_limits(rated_power_w=1e9) is None
    assert executor_limits(rated_power_w=40000.0) is None
    assert executor_limits(rated_power_w=float("nan")) is None


def test_executor_limits_upper_bound_matches_guard():
    from custom_components.volcast.core.guards import MAX_POWER_W
    assert executor_limits(rated_power_w=MAX_POWER_W) == {"rated_power_w": 30000, "source": "entities"}
    assert executor_limits(rated_power_w=8000.0, max_charge_w=MAX_POWER_W + 1) == {
        "rated_power_w": 8000, "source": "entities"}
