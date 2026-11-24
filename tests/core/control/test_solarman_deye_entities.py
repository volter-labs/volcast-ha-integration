"""Profil deye-sg na rejestrze encji w kształcie integracji `solarman` (davidrapan/ha-solarman, definicja
`deye_p3.yaml`: SUN-xK-SG04LP3 / SG01HP3).

Rejestr odtworzony z kodu źródłowego integracji: `unique_id` = slug(`<id wpisu>_<nazwa encji>_<platforma>`),
a w wydaniach z początku 2025 slug(`<nazwa urządzenia>_<numer seryjny>_<nazwa encji>_<platforma>`).
Urządzenie w rejestrze HA ma producenta „Deye” i model z definicji: „SG0*LP3” albo „SG0*HP3”.
Znaki mocy jak w rejestrach falownika (590 bateria > 0 = rozładowanie, 625 sieć > 0 = pobór), czyli jak w rdzeniu.
Czyste funkcje, bez HA i bez sprzętu.
"""
import pytest

from custom_components.volcast.core.control.caps import entity_mode_ready
from custom_components.volcast.core.control.readings import RawState, normalize_readings_report
from custom_components.volcast.core.control.select import InverterHint, ProfileChoice, select_profile
from custom_components.volcast.core.discovery.classify import classify
from custom_components.volcast.core.discovery.models import ConfigEntrySnap, DeviceSnap, EntitySnap
from custom_components.volcast.core.entity_map import EntityCandidate, resolve_entities
from custom_components.volcast.core.profile import load_builtin

DOMAIN = "solarman"
ENTRY = "01jd3k9q2m4x7v8b5n6c0w1z2y"          # identyfikator wpisu HA (ULID) po slugify = małe litery
LEGACY = "inverter_2712345678"                # prefiks z wydań 2025: nazwa urządzenia + numer seryjny


def _dev(dev_id, manufacturer, model):
    return DeviceSnap(id=dev_id, manufacturer=manufacturer, model=model, name=None, sw_version=None,
                      hw_version=None, serial_number=None, identifiers=(), config_entry_ids=(ENTRY,))


def _ent(platform_domain, name_slug, unit=None, *, prefix=ENTRY, disabled=False):
    key = f"{name_slug}_{platform_domain}"
    return EntitySnap(entity_id=f"{platform_domain}.inverter_{name_slug}", platform=DOMAIN,
                      unique_id=f"{prefix}_{key}", device_id="inv", config_entry_id=ENTRY, device_class=None,
                      unit=unit, translation_key=name_slug, original_name=None, disabled=disabled)


def _registry(model="SG0*LP3", prefix=ENTRY):
    """SUN-10K-SG04LP3 z magazynem niskonapięciowym; wycinek encji definicji `deye_p3.yaml`."""
    devices = [_dev("inv", "Deye", model)]
    e = lambda *a, **k: _ent(*a, prefix=prefix, **k)   # noqa: E731
    entities = [
        e("sensor", "battery", "%"),
        e("sensor", "battery_power", "W"),
        e("sensor", "battery_temperature", "°C"),
        e("sensor", "battery_soh", "%"),
        e("sensor", "today_battery_charge", "kWh"),
        e("sensor", "total_battery_charge", "kWh"),
        e("sensor", "pv_power", "W"),
        e("sensor", "pv1_power", "W"),
        e("sensor", "pv2_power", "W"),
        e("sensor", "grid_power", "W"),
        e("sensor", "internal_power", "W"),
        e("sensor", "external_power", "W"),
        e("sensor", "power", "W"),
        e("sensor", "load_power", "W"),
        e("sensor", "today_energy_import", "kWh"),
        e("sensor", "total_energy_import", "kWh"),
        e("sensor", "today_energy_export", "kWh"),
        e("sensor", "total_energy_export", "kWh"),
        e("sensor", "total_load_consumption", "kWh"),
        # encje zapisu integracji — profil ich nie wskazuje
        e("select", "battery_control_mode"),
        e("number", "battery_grid_charging_current", "A"),
        e("switch", "battery_grid_charging"),
        e("number", "program_1_soc", "%"),
    ]
    return devices, entities, [ConfigEntrySnap(ENTRY, DOMAIN, "Inverter", "192.0.2.20")]


def _candidates(entities):
    return [EntityCandidate(e.entity_id, e.platform, e.unique_id, e.unit) for e in entities if not e.disabled]


@pytest.fixture(scope="module")
def profile():
    return load_builtin("deye-sg")


@pytest.mark.parametrize("prefix", [ENTRY, LEGACY])
def test_entity_map_resolves_the_deye_p3_sensors(profile, prefix):
    _devices, entities, _entries = _registry(prefix=prefix)
    res = resolve_entities(profile, DOMAIN, _candidates(entities))
    assert res.mapped == {
        "soc": "sensor.inverter_battery",
        "battery_power_w": "sensor.inverter_battery_power",
        "battery_temp_c": "sensor.inverter_battery_temperature",
        "pv_power_w": "sensor.inverter_pv_power",
        "grid_power_w": "sensor.inverter_grid_power",
        "load_power_w": "sensor.inverter_load_power",
        "grid_import_total_kwh": "sensor.inverter_total_energy_import",
        "grid_export_total_kwh": "sensor.inverter_total_energy_export",
    }
    assert res.missing == [] and res.ambiguous == {} and res.incompatible == {}


def test_only_sensors_are_mapped(profile):
    # Odczyt przez integrację; żadna encja zapisu (tryb, prąd ładowania, programy czasowe) nie jest wskazana.
    ents = profile.raw["ha"]["integrations"][0]["entities"]
    assert {spec["domain"] for spec in ents.values()} == {"sensor"}


def test_readings_keep_the_integration_signs(profile):
    raw = {
        "soc": RawState("55", "%"),
        "battery_power_w": RawState("1500", "W"),          # rozładowanie 1,5 kW
        "grid_power_w": RawState("-400", "W"),              # oddawanie 400 W
        "pv_power_w": RawState("3200", "W"),
        "load_power_w": RawState("4300", "W"),
    }
    out, dropped = normalize_readings_report(raw, profile, DOMAIN)
    assert dropped == ()
    assert out["battery_power_w"] == 1500.0 and out["grid_power_w"] == -400.0
    assert out["pv_power_w"] == 3200.0 and out["load_power_w"] == 4300.0 and out["soc"] == 55.0


def test_draft_profile_never_offers_entity_control(profile):
    _devices, entities, _entries = _registry()
    mapped = resolve_entities(profile, DOMAIN, _candidates(entities)).mapped
    assert entity_mode_ready(ProfileChoice(profile, DOMAIN, None), mapped) is False


def test_classify_finds_the_integration_by_domain(profile):
    devices, entities, entries = _registry()
    cls = classify(devices, entities, entries, {}, profiles=[profile])
    assert [(i.domain, i.matched_by) for i in cls.inverters] == [(DOMAIN, "domain")]


def _choice(manufacturer, model):
    profiles = [load_builtin(p) for p in ("deye-sg", "sofar-hyd", "solis-hybrid")]
    return select_profile([InverterHint(DOMAIN, manufacturer, model)], profiles)


@pytest.mark.parametrize("model", ["SG0*LP3", "SG0*HP3"])
def test_three_phase_hybrid_reads_through_entities(model):
    choice = _choice("Deye", model)
    assert (choice.profile.id, choice.integration_domain) == ("deye-sg", DOMAIN)


@pytest.mark.parametrize("manufacturer,model", [
    ("Deye", "SG0*LP1"),            # jednofazowy: inna mapa, zostaje podglądem
    ("Deye", "Micro"),
    ("Deye", None),
    ("Solarman", "Stick Logger"),   # integracja bez rozpoznanej definicji
])
def test_other_devices_get_no_entity_path(manufacturer, model):
    choice = _choice(manufacturer, model)
    assert choice is None or choice.integration_domain is None


def test_two_solarman_devices_leave_readings_unmapped(profile):
    # Wzorce nie rozróżniają urządzeń tej samej integracji: przy dwóch falownikach każdy klucz jest
    # niejednoznaczny i nie jest mapowany (brak odczytu zamiast odczytu z nie tego urządzenia).
    _d, first, _e = _registry()
    _d, second, _e = _registry(prefix=LEGACY)
    second = [EntitySnap(**{**e.__dict__, "entity_id": e.entity_id.replace("inverter_", "inverter_2_")})
              for e in second]
    res = resolve_entities(profile, DOMAIN, _candidates(first + second))
    assert res.mapped == {} and set(res.ambiguous) == set(profile.raw["ha"]["integrations"][0]["entities"])


def test_entity_path_never_verifies_control(profile):
    from custom_components.volcast.core.control.select import control_verified
    choice = _choice("Deye", "SG0*LP3")
    assert control_verified(choice.profile, choice.integration_domain) is False
