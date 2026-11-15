"""Integracja pośrednicząca odczyt (`solar_assistant`) na migawce rejestru HA o prawdziwym kształcie.

Fixture `tests/discovery/fixtures/readonly_inverter_registry.json`: 144 encje jednego wpisu (103 piszące:
`number`/`select`/`switch`, 41 odczytowych), urządzenia z producentem i BEZ modelu; `unique_id` syntezowane z
kontraktu encji integracji (`<zakres>_<temat>`), nie przechwycone. Profil „tylko encje” jest wpisany w teście
(prawdziwy profil przychodzi z feedu). Wszystko przez prawdziwe funkcje: schemat, klasyfikacja, wybór profilu,
mapa encji, telemetria, wykrywanie ładowarek, wykonawca — bez podmiany kodu pod testem.
"""
import asyncio
import copy
import json
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from custom_components.volcast.control.executor import VolcastExecutor
from custom_components.volcast.control.telemetry import TelemetrySender
from custom_components.volcast.core import profile_feed
from custom_components.volcast.core.control.cycle import ERROR, WRITE
from custom_components.volcast.core.control.select import (
    InverterHint, ProfileChoice, control_verified, select_profile,
)
from custom_components.volcast.core.discovery.charger import classify_chargers
from custom_components.volcast.core.discovery.classify import classify, is_inverter
from custom_components.volcast.core.entity_map import EntityCandidate, resolve_entities
from custom_components.volcast.core.profile import (
    ORIGIN_PACKAGE, direct_verified, entities_only, load_profile, profile_from_dict, writes_allowed,
)
from custom_components.volcast.core.profile_schema import validate_profile
from tests.control import test_executor as base
from tests.control import test_executor_handback as hb
from tests.control.ha_fakes import FakeHass
from tests.control.test_entities_only_never_writes import CHARGE, SpyWriter, _owned_store
from tests.control.test_telemetry import Cloud, Exec
from tests.discovery.test_charger import _load

SA = "solar_assistant"
REGISTRY = "readonly_inverter_registry"
NOW = datetime(2026, 9, 23, 10, 0, 0, tzinfo=timezone.utc)
CAPS_OFF = {"force_charge_from_grid": False, "sell_from_battery": False, "force_discharge": False,
            "standby": False, "set_power_w": False, "limit_export": False, "set_soc_floor": False,
            "set_soc_ceiling": False, "time_windows": 0}
# Oczekiwana mapa: dokładnie te czujniki z urządzenia „Totals”, żadnej encji piszącej.
INTENDED = {
    "soc": "sensor.solarassistant_energy_battery_state_of_charge",
    "pv_power_w": "sensor.solarassistant_energy_pv_power",
    "load_power_w": "sensor.solarassistant_energy_load_power",
    "grid_power_w": "sensor.solarassistant_energy_grid_power",
    "battery_power_w": "sensor.solarassistant_energy_battery_power",
    "battery_temp_c": "sensor.solarassistant_energy_battery_temperature",
    "battery_voltage_v": "sensor.solarassistant_energy_battery_voltage",
}


def profile_raw() -> dict:
    """Profil „tylko encje” integracji pośredniczącej; znak baterii odwrócony (źródło: + = ładowanie)."""
    def sensor(rx, **kw):
        return {"domain": "sensor", "unique_id_regex": rx, **kw}
    return {
        "schema_version": 1, "id": "solarassistant", "label": "Read-only integration", "status": "draft",
        "sources": [{"what": "entity contract of the integration", "ref": "test"}],
        "access": "entities_only", "min_engine": 3,
        "ha": {"integrations": [{"domain": SA, "ems": False, "status": "draft",
                                 "model_regex": ["(?i)\\bsolar ?assistant\\b"], "entities": {
            "soc": sensor("_total/battery_state_of_charge$"),
            "pv_power_w": sensor("_total/pv_power$"),
            "load_power_w": sensor("_total/load_power$"),
            "grid_power_w": sensor("_total/grid_power$"),
            "battery_power_w": sensor("_total/battery_power$", transform="negate"),
            "battery_temp_c": sensor("_total/battery_temperature$"),
            "battery_voltage_v": sensor("_total/battery_voltage$")}}]},
        "capabilities": dict(CAPS_OFF),
    }


@pytest.fixture(scope="module")
def registry():
    return _load(REGISTRY)


@pytest.fixture
def profile():
    return profile_from_dict(profile_raw())


def _hints(finding, profiles):
    hints = [InverterHint(finding.domain, d.manufacturer, d.model) for d in finding.devices]
    return [h for h in hints if is_inverter(h, profiles)]


def _candidates(entities):
    return [EntityCandidate(e.entity_id, e.platform, e.unique_id, e.unit) for e in entities]


def _resolved(profile, registry):
    devices, entities, states, entries = registry
    inv = classify(devices, entities, entries, states, profiles=[profile]).inverters[0]
    choice = select_profile(_hints(inv, [profile]), [profile])
    return choice, resolve_entities(choice.profile, choice.integration_domain, _candidates(inv.entities))


# --- fixture: kształt rzeczywistej instalacji ------------------------------------------------------------

def test_fixture_has_the_real_shape(registry):
    devices, entities, states, entries = registry
    assert [(e.domain, e.host) for e in entries] == [(SA, "10.0.0.10")]
    assert {d.manufacturer for d in devices} == {"SolarAssistant"} and {d.model for d in devices} == {None}
    assert len(devices) == 5 and len(entities) == 144 and set(states) == {e.entity_id for e in entities}
    by_domain = {}
    for e in entities:
        by_domain[e.entity_id.split(".")[0]] = by_domain.get(e.entity_id.split(".")[0], 0) + 1
        assert e.platform == SA and e.unique_id.startswith("local:10.0.0.10_")
    assert by_domain == {"number": 55, "select": 29, "switch": 19, "sensor": 40, "binary_sensor": 1}
    assert len({e.unique_id for e in entities}) == 144


# --- a. walidacja i ładowanie profilu --------------------------------------------------------------------

def test_profile_passes_the_real_validator_and_loads_from_file(tmp_path):
    raw = profile_raw()
    assert validate_profile(raw, parse_tree=True) == []
    profile_feed._one_profile(copy.deepcopy(raw))            # ładowarka feedu też przyjmuje (nie rzuca)
    path = tmp_path / "solarassistant.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    p = load_profile(path)
    assert p.id == "solarassistant" and entities_only(p)
    assert writes_allowed(p) is False
    assert control_verified(p, SA) is False
    assert direct_verified(p) is False
    assert p.raw["capabilities"] == CAPS_OFF                  # żadna możliwość sterowania


def test_profile_without_the_negate_or_with_a_writable_key_is_rejected_negative_control():
    # kontrola ujemna: ta sama walidacja naprawdę odrzuca encję zapisu w profilu „tylko encje”
    raw = profile_raw()
    raw["ha"]["integrations"][0]["entities"]["power_w"] = {"domain": "number", "unique_id_regex": "_charge$"}
    assert validate_profile(raw)


# --- b. klasyfikacja -------------------------------------------------------------------------------------

def test_classify_finds_one_inverter_only_with_the_profile(registry, profile):
    devices, entities, states, entries = registry
    with_profile = classify(devices, entities, entries, states, profiles=[profile])
    assert [(i.domain, i.config_entry_id, i.matched_by) for i in with_profile.inverters] == [(SA, "sa1", "domain")]
    assert {d.id for d in with_profile.inverters[0].devices} == {d.id for d in devices}
    assert len(with_profile.inverters[0].entities) == 144
    assert classify(devices, entities, entries, states).inverters == []          # bez profilu: nic


# --- c. wybór profilu ------------------------------------------------------------------------------------

def test_selection_gives_the_read_only_choice(registry, profile):
    devices, entities, states, entries = registry
    inv = classify(devices, entities, entries, states, profiles=[profile]).inverters[0]
    hints = _hints(inv, [profile])
    assert len(hints) == 5 and all(h == InverterHint(SA, "SolarAssistant", None) for h in hints)
    choice = select_profile(hints, [profile])
    assert (choice.profile.id, choice.integration_domain, choice.model) == ("solarassistant", SA, None)
    assert select_profile(hints, []) is None                                     # bez profilu: brak wyboru


# --- d. mapa encji ---------------------------------------------------------------------------------------

def test_entity_map_has_only_the_intended_sensors_and_no_writable_entity(registry, profile):
    _, entities, _, _ = registry
    choice, res = _resolved(profile, registry)
    assert res.mapped == INTENDED
    assert res.missing == [] and res.ambiguous == {} and res.incompatible == {}
    writable = {e.entity_id for e in entities if e.entity_id.split(".")[0] in ("number", "select", "switch")}
    assert len(writable) == 103
    assert not set(res.mapped.values()) & writable
    assert not [eid for eid in res.mapped.values() if eid.startswith(("number.", "select.", "switch."))]
    assert all(eid.startswith("sensor.") for eid in res.mapped.values())
    # klucze jednoznaczne mimo bliźniaków `inverter_1/*` (sensor.inverters_2_* w rejestrze)
    twins = {e.unique_id for e in entities if e.unique_id.split("_", 1)[1].startswith("inverter_1/")
             and e.entity_id.split(".")[0] == "sensor"}
    assert {"local:10.0.0.10_inverter_1/pv_power", "local:10.0.0.10_inverter_1/load_power",
            "local:10.0.0.10_inverter_1/grid_power"} <= twins
    assert {e.unique_id for e in entities if e.entity_id in res.mapped.values()} \
        == {f"local:10.0.0.10_total/{m}" for m in ("battery_state_of_charge", "pv_power", "load_power", "grid_power",
                                                   "battery_power", "battery_temperature", "battery_voltage")}


def test_a_loose_pattern_would_be_ambiguous_negative_control(registry):
    # ten sam rejestr z wzorcem bez `_total/`: bliźniak `inverter_1/pv_power` robi klucz niejednoznacznym —
    # test wyżej naprawdę wykrywa taki błąd profilu
    raw = profile_raw()
    raw["ha"]["integrations"][0]["entities"]["pv_power_w"]["unique_id_regex"] = "/pv_power$"
    p = profile_from_dict(raw)
    res = resolve_entities(p, SA, _candidates(registry[1]))
    assert "pv_power_w" in res.ambiguous and len(res.ambiguous["pv_power_w"]) == 2 and "pv_power_w" not in res.mapped


# --- e. telemetria ---------------------------------------------------------------------------------------

def _hass(registry):
    h = FakeHass()
    for eid, st in registry[2].items():
        h.states.set(eid, st.state, st.attributes)
    return h


def test_telemetry_reading_has_canonical_units_and_signs(registry, profile):
    choice, res = _resolved(profile, registry)
    h = _hass(registry)
    # źródło: + = ładowanie baterii (1500), + = pobór z sieci (800)
    h.states.set(INTENDED["battery_power_w"], "1500", {"unit_of_measurement": "W"})
    h.states.set(INTENDED["grid_power_w"], "800", {"unit_of_measurement": "W"})
    cloud = Cloud()
    s = TelemetrySender(h, SimpleNamespace(entry_id="e1", options={}), cloud, Exec(), choice=choice,
                        profile_map=dict(res.mapped), manual_map={}, grid_negate=False, limits=None,
                        utcnow=lambda: NOW)
    assert asyncio.run(s.async_flush()) is True
    sent = cloud.sent[0]
    assert (sent["battery_soc"], sent["pv_power_w"], sent["load_power_w"]) == (57, 3200, 3500)
    assert sent["battery_power_w"] == -1500         # ładowanie u źródła → u nas ujemne (+ = rozładowanie)
    assert sent["grid_power_w"] == 800              # pobór zostaje dodatni
    driver = sent["driver"]
    assert driver["id"] == "solarassistant"
    assert not any(driver.get("capabilities", {}).values())     # żadna możliwość nie jest prawdziwa


def test_telemetry_sign_flips_with_the_source_negative_control(registry, profile):
    # rozładowanie u źródła (−900) daje +900: znak naprawdę przechodzi przez transformację profilu
    choice, res = _resolved(profile, registry)
    h = _hass(registry)
    h.states.set(INTENDED["battery_power_w"], "-900", {"unit_of_measurement": "W"})
    cloud = Cloud()
    s = TelemetrySender(h, SimpleNamespace(entry_id="e1", options={}), cloud, Exec(), choice=choice,
                        profile_map=dict(res.mapped), manual_map={}, grid_negate=False, limits=None,
                        utcnow=lambda: NOW)
    asyncio.run(s.async_flush())
    assert cloud.sent[0]["battery_power_w"] == 900


# --- f. wykrywanie ładowarek -----------------------------------------------------------------------------

def test_no_charger_is_found_on_this_registry(registry, profile):
    devices, entities, states, entries = registry
    # „Inverters 1”: 55 numerów (część w A/W ze słowami ładowania), 29 selectów, 19 przełączników
    inv_dev = next(d for d in devices if d.name == "Inverters 1")
    own = [e for e in entities if e.device_id == inv_dev.id]
    assert {d: sum(e.entity_id.startswith(d + ".") for e in own) for d in ("number", "select", "switch")} \
        == {"number": 55, "select": 29, "switch": 19}
    assert classify(devices, entities, entries, states, profiles=[profile]).chargers == []
    # Heurystyka sama z siebie nic tu nie znajduje (czujnik trybu nie jest enumem) — wykluczenie zadziała
    # więc dopiero przy zmianie: „Device mode” jako enum z tokenami ładowania robi z „Inverters 1”
    # kandydata na ładowarkę (55 numerów w A/W, 19 przełączników), a wpis „tylko encje” nadal go wyklucza.
    trap = [replace(e, device_class="enum", capabilities={"options": ["standby", "grid_connected",
                                                                         "battery_charging", "fault"]})
            if e.entity_id == "sensor.inverters_2_device_mode" else e for e in entities]
    assert [f.device_id for f in classify_chargers(devices, trap, states)] == [inv_dev.id]
    assert classify(devices, trap, entries, states, profiles=[profile]).chargers == []


# --- g. wykonawca ----------------------------------------------------------------------------------------

def test_executor_with_this_choice_makes_zero_service_calls_even_with_stale_control_options(registry, profile):
    choice, res = _resolved(profile, registry)
    choice = ProfileChoice(profile_from_dict(profile_raw(), origin=ORIGIN_PACKAGE), SA, None)
    h = hb.dhass(mode="charge_battery", enable="on")            # encje zapisu po poprzednim profilu
    for eid, st in registry[2].items():
        h.states.set(eid, st.state, st.attributes)
    store = _owned_store(h)
    writer = SpyWriter(h)
    entry = SimpleNamespace(entry_id="e1", options={"control_mode": "entities"})
    mapped = {**hb.ENT, **res.mapped}                            # nieaktualna mapa zapisu + czujniki profilu
    ex = VolcastExecutor(h, entry, choice=choice, mapped=mapped, rated_power_w=8000.0, store=store,
                         writer=writer, clock=base.Clock(), utcnow=hb.Utc())

    async def go():
        await base.ready(ex, raw=base.plan(slots=CHARGE))
        await ex.async_tick()
        ex._clock.t += 120
        await ex.async_tick()
    asyncio.run(go())

    assert writer.writes == [] and h.services.calls == []
    assert ex.last_decision is None or ex.last_decision.status not in (WRITE, ERROR)
    assert ex.verification_can_write() is False
