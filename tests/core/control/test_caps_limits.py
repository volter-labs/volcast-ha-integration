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


def test_bms_power_from_current_and_battery_voltage():
    from custom_components.volcast.core.control.limits import bms_power_w
    # 9 A × 304,9 V = 2744 W → w dół do 100 W
    assert bms_power_w(9, 304.9, 8000.0) == 2700.0
    assert bms_power_w(22, 304.9, 8000.0) == 6700.0
    assert bms_power_w(30, 400.0, 8000.0) == 8000.0          # przycięte do mocy znamionowej
    assert bms_power_w(100, 51.2) == 5100.0                  # bez mocy znamionowej — sam iloczyn


@pytest.mark.parametrize("current,voltage", [
    (0, 304.9), (-9, 304.9), (0.5, 304.9), (1001, 304.9), (65535, 304.9), (float("nan"), 304.9),
    (float("inf"), 304.9), (True, 304.9), ("9", 304.9), (None, 304.9),
    (9, None), (9, 0), (9, 39.9), (9, 1000.1), (9, float("nan")), (9, -304.9),
    (0.6, 100.0),                                       # 60 W < 100 W — poniżej sensownej mocy
])
def test_implausible_bms_values_are_omitted(current, voltage):
    from custom_components.volcast.core.control.limits import bms_power_w, register_limits
    assert bms_power_w(current, voltage, 8000.0) is None
    spec = {"max_charge_register": "bms_charge_current_max_a", "max_discharge_register": "bms_discharge_current_max_a"}
    values = {"soc": 50, "bms_charge_current_max_a": current, "bms_discharge_current_max_a": current,
              "battery_voltage_v": voltage}
    assert register_limits({"rated_power_w": 8000, "source": "registers"}, spec, values) == {
        "rated_power_w": 8000, "source": "registers"}                   # nigdy 0 ani śmieci
    assert register_limits(None, spec, values) is None


def test_executor_limits_has_no_bms_parameters():
    import inspect
    params = inspect.signature(executor_limits).parameters
    assert not {"bms_charge_current_a", "bms_discharge_current_a", "battery_voltage_v"} & set(params)


def test_bms_power_bounds_are_documented_constants():
    from custom_components.volcast.core.control import limits as L
    assert L.BMS_CURRENT_RANGE_A == (1.0, 1000.0)
    assert L.BATTERY_VOLTAGE_RANGE_V == (40.0, 1000.0)
    assert L.BMS_POWER_STEP_W == 100
    assert L.CHARGE_SAMPLE_SOC_MAX == 80
    assert L.DISCHARGE_SAMPLE_SOC_MIN == 20


def test_register_limits_capacity_and_owner_values():
    from custom_components.volcast.core.control.limits import register_limits
    spec = {"max_charge_register": "bms_charge_current_max_a", "capacity_register": "battery_capacity_kwh"}
    values = {"soc": 50, "bms_charge_current_max_a": 25, "battery_voltage_v": 300.0, "battery_capacity_kwh": 10.24,
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


_SPEC = {"max_charge_register": "bms_charge_current_max_a", "max_discharge_register": "bms_discharge_current_max_a"}
_BASE = {"rated_power_w": 8000, "source": "registers"}


def _bms(soc, charge_a=25, discharge_a=25, voltage=300.0):
    return {"soc": soc, "bms_charge_current_max_a": charge_a, "bms_discharge_current_max_a": discharge_a,
            "battery_voltage_v": voltage}


@pytest.mark.parametrize("soc,charge,discharge", [
    (50, 7500, 7500),
    (79.9, 7500, 7500),
    (80, None, 7500),          # od 80 % BMS już ogranicza prąd ładowania
    (83, None, 7500),
    (100, None, 7500),
    (20.1, 7500, 7500),
    (20, 7500, None),          # do 20 % BMS ogranicza prąd rozładowania
    (10, 7500, None),
    (0, 7500, None),
    (None, None, None),        # SoC nieznane — nic
    (float("nan"), None, None),
    (True, None, None),
    (-1, None, None),
    (101, None, None),
])
def test_register_limits_sample_only_where_bms_does_not_throttle(soc, charge, discharge):
    from custom_components.volcast.core.control.limits import register_limits
    lim = register_limits(_BASE, _SPEC, _bms(soc)) or {}
    assert lim.get("max_charge_w") == charge and lim.get("max_discharge_w") == discharge


def test_register_limits_with_peaks():
    from custom_components.volcast.core.control.limits import PeakLimit, register_limits

    def run(peaks, values, now_s, base=_BASE):
        return register_limits(base, _SPEC, values, peaks=peaks, now_s=now_s) or {}

    peaks = {"max_charge_w": PeakLimit(), "max_discharge_w": PeakLimit()}
    # pierwszy odczyt przy 83 % SoC (9 A, ograniczone przez BMS) — pole pominięte, chmura bierze konfigurację
    assert "max_charge_w" not in run(peaks, _bms(83, charge_a=9), 0.0)
    # odczyt przy 50 % — szczyt
    assert run(peaks, _bms(50), 60.0)["max_charge_w"] == 7500
    # później 90 % i 2 A: szczyt się nie obniża (próbka spoza pasma nie wchodzi)
    assert run(peaks, _bms(90, charge_a=2), 120.0)["max_charge_w"] == 7500
    assert run(peaks, {}, 180.0)["max_charge_w"] == 7500
    # rozładowanie przy 10 % — pominięte, dopóki nie było próbki w paśmie
    fresh = {"max_charge_w": PeakLimit(), "max_discharge_w": PeakLimit()}
    assert "max_discharge_w" not in run(fresh, _bms(10, discharge_a=3), 0.0)
    assert run(fresh, _bms(60, discharge_a=22), 60.0)["max_discharge_w"] == 6600
    assert run(fresh, _bms(10, discharge_a=3), 120.0)["max_discharge_w"] == 6600
    # moc znamionowa obniżona później (np. korekta właściciela) — szczyt nadal przycięty
    assert run(peaks, {}, 240.0, {"rated_power_w": 5000, "source": "user"}) == {
        "rated_power_w": 5000, "max_charge_w": 5000, "max_discharge_w": 5000, "source": "user"}


def test_restart_at_high_soc_omits_charge_limit():
    from custom_components.volcast.core.control.limits import PeakLimit, register_limits
    # restart HA / przeładowanie opcji = nowy szczyt w pamięci; przy pełnej baterii nic nie wysyłamy
    peaks = {"max_charge_w": PeakLimit(), "max_discharge_w": PeakLimit()}
    lim = register_limits(_BASE, _SPEC, _bms(90, charge_a=2), peaks=peaks, now_s=0.0)
    assert "max_charge_w" not in lim and lim["max_discharge_w"] == 7500
    assert register_limits(_BASE, _SPEC, _bms(50), peaks=peaks, now_s=3600.0)["max_charge_w"] == 7500


def test_peak_limit_keeps_24h_peak_against_bms_taper():
    from custom_components.volcast.core.control.limits import PeakLimit
    peak = PeakLimit()
    assert peak.peak(0.0) is None
    peak.observe(6700.0, 0.0)
    peak.observe(2700.0, 2 * 3600.0)
    peak.observe(None, 2 * 3600.0 + 60)               # brak odczytu nie kasuje szczytu
    for bad in (float("nan"), float("inf"), True, "7000", -1.0):
        peak.observe(bad, 3 * 3600.0)                 # śmieci nie wchodzą do szczytu
    assert peak.peak(2 * 3600.0) == 6700.0
    assert peak.peak(23 * 3600.0 + 3599) == 6700.0
    assert peak.peak(24 * 3600.0) == 2700.0           # szczyt sprzed doby wygasł
    assert peak.peak(26 * 3600.0) is None             # po dobie bez odczytu — nic


def test_peak_limit_survives_wall_clock_jump_and_correction():
    from custom_components.volcast.core.control.limits import PeakLimit
    peak = PeakLimit()
    peak.observe(6700.0, 0.0)
    peak.observe(6000.0, 48 * 3600.0)                 # zegar skoczył o dwie doby do przodu
    peak.observe(5000.0, 3600.0)                      # korekta zegara: próbka z „teraz" zostaje
    assert peak.peak(3600.0) == 5000.0


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
