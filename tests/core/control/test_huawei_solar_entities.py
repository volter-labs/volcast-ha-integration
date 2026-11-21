"""Profil huawei-sun2000 na rejestrze encji w kształcie integracji `huawei_solar` (wlcrs, 2.3.x).

Rejestr odtworzony z kodu źródłowego integracji: `unique_id` = `<numer seryjny falownika>_<klucz rejestru>`
dla każdej encji falownika, miernika i baterii (miernik oraz baterie to urządzenia potomne, ale prefiks
zostaje numerem falownika). Encje na poziomie pojedynczego modułu baterii (`storage_unit_1_*`) są
w integracji domyślnie wyłączone dla LUNA2000. Czyste funkcje, bez HA i bez sprzętu.
"""
import pytest

from custom_components.volcast.core.control.caps import entity_mode_ready
from custom_components.volcast.core.control.readings import RawState, normalize_readings_report
from custom_components.volcast.core.control.select import InverterHint, ProfileChoice, select_profile
from custom_components.volcast.core.discovery.classify import classify
from custom_components.volcast.core.discovery.models import ConfigEntrySnap, DeviceSnap, EntitySnap
from custom_components.volcast.core.entity_map import EntityCandidate, resolve_entities
from custom_components.volcast.core.profile import load_builtin

DOMAIN = "huawei_solar"
SN = "HV2250012345"
ENTRY = "hs1"


def _dev(dev_id, manufacturer, model):
    return DeviceSnap(id=dev_id, manufacturer=manufacturer, model=model, name=None, sw_version=None,
                      hw_version=None, serial_number=None, identifiers=(), config_entry_ids=(ENTRY,))


def _ent(platform_domain, key, dev_id, unit=None, *, sn=SN, disabled=False):
    return EntitySnap(entity_id=f"{platform_domain}.{sn.lower()}_{key}", platform=DOMAIN,
                      unique_id=f"{sn}_{key}", device_id=dev_id, config_entry_id=ENTRY, device_class=None,
                      unit=unit, translation_key=key, original_name=None, disabled=disabled)


def _registry(model="SUN2000-10K-MAP0"):
    """MAP0 + LUNA2000-S1 + miernik DTSU666-H, tryb zapisu parametrów włączony w integracji."""
    devices = [
        _dev("inv", "Huawei", model),
        _dev("meter", None, None),                       # DeviceInfo miernika nie niesie producenta ani modelu
        _dev("ces", "Huawei", "Batteries"),              # „connected_energy_storage”
        _dev("bat1", "Huawei", "LUNA 2000"),
    ]
    entities = [
        _ent("sensor", "input_power", "inv", "W"),
        _ent("sensor", "active_power", "inv", "W"),
        _ent("sensor", "power_meter_active_power", "meter", "W"),
        _ent("sensor", "power_meter_reactive_power", "meter", "var", disabled=True),
        _ent("sensor", "grid_exported_energy", "meter", "kWh"),
        _ent("sensor", "grid_accumulated_energy", "meter", "kWh"),
        _ent("sensor", "storage_state_of_capacity", "ces", "%"),
        _ent("sensor", "storage_charge_discharge_power", "ces", "W"),
        _ent("sensor", "storage_bus_voltage", "ces", "V"),
        _ent("sensor", "storage_bus_current", "ces", "A"),
        _ent("sensor", "storage_total_charge", "ces", "kWh"),
        # poziom modułu baterii: w integracji wyłączone domyślnie (nie-LG)
        _ent("sensor", "storage_unit_1_state_of_capacity", "bat1", "%", disabled=True),
        _ent("sensor", "storage_unit_1_charge_discharge_power", "bat1", "W", disabled=True),
        _ent("sensor", "storage_unit_1_battery_temperature", "bat1", "°C", disabled=True),
        _ent("select", "storage_working_mode_settings", "ces"),
        _ent("number", "storage_charging_cutoff_capacity", "ces", "%"),
        _ent("number", "storage_discharging_cutoff_capacity", "ces", "%"),
    ]
    return devices, entities, [ConfigEntrySnap(ENTRY, DOMAIN, "Huawei Solar", "192.0.2.10")]


def _candidates(entities):
    # jak `control.runtime._entity_candidates`: encje wyłączone w rejestrze nie są kandydatami
    return [EntityCandidate(e.entity_id, e.platform, e.unique_id, e.unit) for e in entities if not e.disabled]


@pytest.fixture(scope="module")
def profile():
    return load_builtin("huawei-sun2000")


def test_entity_map_resolves_on_map0_luna2000_with_meter(profile):
    _devices, entities, _entries = _registry()
    res = resolve_entities(profile, DOMAIN, _candidates(entities))
    sid = SN.lower()
    assert res.mapped == {
        "soc": f"sensor.{sid}_storage_state_of_capacity",
        "battery_power_w": f"sensor.{sid}_storage_charge_discharge_power",
        "battery_voltage_v": f"sensor.{sid}_storage_bus_voltage",
        "pv_power_w": f"sensor.{sid}_input_power",
        "active_power_w": f"sensor.{sid}_active_power",
        "grid_power_w": f"sensor.{sid}_power_meter_active_power",
        "grid_export_total_kwh": f"sensor.{sid}_grid_exported_energy",
        "grid_import_total_kwh": f"sensor.{sid}_grid_accumulated_energy",
        "mode": f"select.{sid}_storage_working_mode_settings",
        "soc_min": f"number.{sid}_storage_discharging_cutoff_capacity",
        "soc_max": f"number.{sid}_storage_charging_cutoff_capacity",
    }
    # Temperatura baterii siedzi tylko na encji modułu, domyślnie wyłączonej — brak odczytu, nie błąd.
    assert res.missing == ["battery_temp_c"]
    assert res.ambiguous == {} and res.incompatible == {}


def test_no_load_entity_is_mapped(profile):
    # Integracja nie ma encji poboru domu dla samego falownika (jest tylko przy EMMA/SDongle): wtedy
    # `load_power_w` w telemetrii wcale nie ma, wtyczka go nie wylicza.
    assert "load_power_w" not in profile.raw["ha"]["integrations"][0]["entities"]


def test_readings_follow_core_sign_convention(profile):
    # Huawei: bateria > 0 = ładowanie, miernik > 0 = oddawanie do sieci; rdzeń liczy odwrotnie w obu.
    raw = {
        "soc": RawState("55.0", "%"),
        "battery_power_w": RawState("1500", "W"),         # ładowanie 1,5 kW
        "grid_power_w": RawState("-400", "W"),             # pobór 400 W z sieci
        "pv_power_w": RawState("3200", "W"),
        "active_power_w": RawState("1700", "W"),
        "battery_voltage_v": RawState("450.1", "V"),
    }
    out, dropped = normalize_readings_report(raw, profile, DOMAIN)
    assert dropped == ()
    assert out["battery_power_w"] == -1500.0
    assert out["grid_power_w"] == 400.0
    assert out["pv_power_w"] == 3200.0 and out["soc"] == 55.0


def test_draft_profile_never_offers_entity_control(profile):
    _devices, entities, _entries = _registry()
    mapped = resolve_entities(profile, DOMAIN, _candidates(entities)).mapped
    assert entity_mode_ready(ProfileChoice(profile, DOMAIN, None), mapped) is False


def test_classify_finds_the_integration_by_domain(profile):
    devices, entities, entries = _registry()
    cls = classify(devices, entities, entries, {}, profiles=[profile])
    assert [(i.domain, i.matched_by) for i in cls.inverters] == [(DOMAIN, "domain")]
    inv = cls.inverters[0]
    assert {d.id for d in inv.devices} == {"inv", "meter", "ces", "bat1"}
    assert len(inv.entities) == len(entities)


def _choice(model):
    return select_profile([InverterHint(DOMAIN, "Huawei", model)], [load_builtin("huawei-sun2000")])


@pytest.mark.parametrize("model", ["SUN2000-5KTL-L1", "SUN2000-10KTL-M1"])
def test_ktl_l1_m1_reads_through_entities(model):
    choice = _choice(model)
    assert (choice.profile.id, choice.integration_domain) == ("huawei-sun2000", DOMAIN)


@pytest.mark.parametrize("model", ["SUN2000-10K-MAP0", "SUN2000-5K-MAP0"])
def test_map0_reads_through_entities(model):
    choice = _choice(model)
    assert choice is not None and choice.integration_domain == DOMAIN


@pytest.mark.parametrize("model", ["SUN2000-10KTL-M2", "SUN2000-100KTL-M1", "SUN2000-5K-MAP1", None])
def test_uncovered_model_stays_a_preview_without_integration(model):
    choice = _choice(model)
    assert (choice.profile.id, choice.integration_domain) == ("huawei-sun2000", None)
