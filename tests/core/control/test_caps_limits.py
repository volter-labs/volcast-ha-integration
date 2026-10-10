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


def test_executor_limits_from_bms_current_and_battery_voltage():
    lim = executor_limits(rated_power_w=8000.0, bms_charge_current_a=9, bms_discharge_current_a=22,
                          battery_voltage_v=304.9, source="registers")
    assert lim == {"rated_power_w": 8000, "max_charge_w": 2700, "max_discharge_w": 6700, "source": "registers"}
    # przycięte do mocy znamionowej; bez znanej mocy znamionowej — sam iloczyn
    assert executor_limits(rated_power_w=8000.0, bms_charge_current_a=30, battery_voltage_v=400.0)[
        "max_charge_w"] == 8000
    assert executor_limits(rated_power_w=None, bms_charge_current_a=100, battery_voltage_v=51.2,
                           source="registers") == {"max_charge_w": 5100, "source": "registers"}
    # jawna moc ma pierwszeństwo przed iloczynem
    assert executor_limits(rated_power_w=8000.0, max_charge_w=3000.0, bms_charge_current_a=9,
                           battery_voltage_v=304.9)["max_charge_w"] == 3000


@pytest.mark.parametrize("current,voltage", [
    (0, 304.9), (-9, 304.9), (0.5, 304.9), (1001, 304.9), (65535, 304.9), (float("nan"), 304.9),
    (float("inf"), 304.9), (True, 304.9), ("9", 304.9), (None, 304.9),
    (9, None), (9, 0), (9, 39.9), (9, 1000.1), (9, float("nan")), (9, -304.9),
    (0.6, 100.0),                                       # 60 W < 100 W — poniżej sensownej mocy
])
def test_implausible_bms_values_are_omitted(current, voltage):
    lim = executor_limits(rated_power_w=8000.0, bms_charge_current_a=current, bms_discharge_current_a=current,
                          battery_voltage_v=voltage)
    assert lim == {"rated_power_w": 8000, "source": "entities"}        # nigdy 0 ani śmieci
    assert executor_limits(rated_power_w=None, bms_charge_current_a=current, battery_voltage_v=voltage) is None


def test_bms_power_bounds_are_documented_constants():
    from custom_components.volcast.core.control import limits as L
    assert L.BMS_CURRENT_RANGE_A == (1.0, 1000.0)
    assert L.BATTERY_VOLTAGE_RANGE_V == (40.0, 1000.0)
    assert L.BMS_POWER_STEP_W == 100


def test_register_limits_capacity_and_owner_values():
    from custom_components.volcast.core.control.limits import register_limits
    spec = {"max_charge_register": "bms_charge_current_max_a", "capacity_register": "battery_capacity_kwh"}
    values = {"bms_charge_current_max_a": 25, "battery_voltage_v": 300.0, "battery_capacity_kwh": 10.24,
              "bms_discharge_current_max_a": 25}
    # rozładowanie bez `max_discharge_register` w profilu — nie liczymy go, choć odczyt jest
    assert register_limits({"rated_power_w": 10000, "source": "registers"}, spec, values) == {
        "rated_power_w": 10000, "max_charge_w": 7500, "battery_capacity_kwh": 10.24, "source": "registers"}
    # pojemność wpisana przez właściciela wygrywa z rejestrem; bez bazy źródło = rejestry
    assert register_limits({"rated_power_w": 10000, "battery_capacity_kwh": 15.0, "source": "user"}, spec,
                           values)["battery_capacity_kwh"] == 15.0
    assert register_limits(None, spec, values) == {"max_charge_w": 7500, "battery_capacity_kwh": 10.24,
                                                   "source": "registers"}
    assert register_limits(None, spec, {**values, "battery_capacity_kwh": 0}) == {
        "max_charge_w": 7500, "source": "registers"}
    assert register_limits(None, {}, values) is None
    assert register_limits({"rated_power_w": 8000, "source": "registers"}, spec, {}) == {
        "rated_power_w": 8000, "source": "registers"}


def test_peak_limit_keeps_24h_peak_against_bms_taper():
    from custom_components.volcast.core.control.limits import PeakLimit
    peak = PeakLimit()
    assert peak.peak(0.0) is None
    peak.observe(6700.0, 0.0)
    peak.observe(2700.0, 2 * 3600.0)                  # BMS ogranicza prąd przy wysokim SoC
    peak.observe(None, 2 * 3600.0 + 60)               # brak odczytu nie kasuje szczytu
    for bad in (float("nan"), float("inf"), True, "7000", -1.0):
        peak.observe(bad, 3 * 3600.0)                 # śmieci nie wchodzą do szczytu
    assert peak.peak(2 * 3600.0) == 6700.0
    assert peak.peak(23 * 3600.0 + 3599) == 6700.0
    assert peak.peak(24 * 3600.0) == 2700.0           # szczyt sprzed doby wygasł
    assert peak.peak(26 * 3600.0) is None             # po dobie bez odczytu — nic
    peak.observe(5000.0, 10.0)                        # zegar cofnięty: próbka z przeszłości nie psuje
    assert peak.peak(26 * 3600.0) is None


def test_register_limits_with_peaks():
    from custom_components.volcast.core.control.limits import PeakLimit, register_limits
    spec = {"max_charge_register": "bms_charge_current_max_a", "max_discharge_register": "bms_discharge_current_max_a"}
    peaks = {"max_charge_w": PeakLimit(), "max_discharge_w": PeakLimit()}
    base = {"rated_power_w": 8000, "source": "registers"}
    high = {"bms_charge_current_max_a": 25, "bms_discharge_current_max_a": 25, "battery_voltage_v": 300.0}
    low = {"bms_charge_current_max_a": 2, "bms_discharge_current_max_a": 25, "battery_voltage_v": 301.0}
    assert register_limits(base, spec, high, peaks=peaks, now_s=0.0)["max_charge_w"] == 7500
    assert register_limits(base, spec, low, peaks=peaks, now_s=60.0)["max_charge_w"] == 7500
    assert register_limits(base, spec, {}, peaks=peaks, now_s=120.0)["max_charge_w"] == 7500
    # moc znamionowa obniżona później (np. korekta właściciela) — szczyt nadal przycięty
    assert register_limits({"rated_power_w": 5000, "source": "user"}, spec, {}, peaks=peaks,
                           now_s=180.0) == {"rated_power_w": 5000, "max_charge_w": 5000,
                                            "max_discharge_w": 5000, "source": "user"}


DRAFT_ENTITY_PROFILES = (("huawei-sun2000", "huawei_solar"), ("foxess-h", "foxess_modbus"),
                         ("solax-x-hybrid", "solax_modbus"), ("solaredge-storedge", "solaredge_modbus_multi"))


@pytest.mark.parametrize("pid,domain", DRAFT_ENTITY_PROFILES)
def test_draft_integration_entry_is_not_entity_mode_ready(pid, domain):
    # Wpis `ha.integrations` w stanie draft służy tylko jako podpowiedź mapowania odczytów.
    choice = ProfileChoice(load_builtin(pid), domain, None)
    assert entity_mode_ready(choice, ALL) is False


def test_verified_profile_with_draft_integration_entry_is_not_ready():
    from custom_components.volcast.core.profile import PROFILES_DIR, profile_from_dict
    import json
    raw = json.loads((PROFILES_DIR / "goodwe-et.json").read_text(encoding="utf-8"))
    raw["ha"]["integrations"][0]["status"] = "draft"
    assert entity_mode_ready(ProfileChoice(profile_from_dict(raw), "goodwe", None), ALL) is False


def test_goodwe_et_stays_entity_mode_ready():
    assert entity_mode_ready(ProfileChoice(GW, "goodwe", "GW8KN-ET"), ALL) is True
