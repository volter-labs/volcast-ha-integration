"""Ładowarka bez encji sterujących (same czujniki) na migawce rejestru o prawdziwym kształcie.

Fixture `fixtures/sensor_only_wallbox.json`: jeden wpis spoza falowników, jedno urządzenie, status (enum),
moc, energia sesji i dożywotnia (Wh, rosnące), prądy i napięcia faz, dwa czujniki binarne. Od wykrycia przez
formularz opcji po odczyt dla chmury, na prawdziwych funkcjach.
"""
import asyncio
from types import SimpleNamespace

from custom_components.volcast.control.loads_reader import LoadsReader
from custom_components.volcast.core.discovery.charger import classify_chargers
from custom_components.volcast.core.discovery.classify import classify
from tests.control.ha_fakes import FakeStates
from tests.discovery.test_charger import _load
from tests.test_ev_charger_flow import _flow
from tests.test_readonly_registry_end_to_end import REGISTRY, profile_raw
from custom_components.volcast.core.profile import profile_from_dict

WALLBOX = "sensor_only_wallbox"
OPTIONS = ["booting", "not_connected", "connected", "ready", "negotiating", "error", "charging_finished",
           "waiting_car", "charging_reduced", "charging"]
ROLES = {"status": "sensor.wall_box_status", "power": "sensor.wall_box_power",
         "energy": "sensor.wall_box_lifetime_energy"}


def _finding():
    devices, entities, states, entries = _load(WALLBOX)
    c = classify(devices, entities, entries, states)
    assert c.inverters == []                       # to nie falownik
    (f,) = c.chargers
    return f


def test_discovery_finds_one_monitor_charger_with_the_lifetime_counter():
    devices, entities, states, _ = _load(WALLBOX)
    (f,) = classify_chargers(devices, entities, states)
    assert (f.device_id, f.control_class) == ("dev-wb", "monitor")
    assert {role: r.entity_id for role, r in f.roles.items()} == ROLES        # energia = licznik dożywotni
    assert f.roles["status"].options == tuple(OPTIONS)
    assert f.roles["energy"].unit == "Wh" and f.missing == () and f.confidence == "high"
    # żadna encja sterująca ani niemierzalna (prąd/napięcie faz, czujniki binarne) nie wchodzi do ról
    assert not any(r.entity_id.startswith(("number.", "select.", "switch.", "binary_sensor."))
                   or "phase" in r.entity_id for r in f.roles.values())


def test_session_counter_is_never_chosen_over_the_lifetime_counter():
    devices, entities, states, _ = _load(WALLBOX)
    for ents in (entities, entities[::-1]):                       # kolejność encji nie ma znaczenia
        (f,) = classify_chargers(devices, ents, states)
        assert f.roles["energy"].entity_id == "sensor.wall_box_lifetime_energy"


def test_without_the_lifetime_counter_the_session_one_is_taken_negative_control():
    # kontrola ujemna: asercja o liczniku dożywotnim coś rozstrzyga — bez niego wybór pada na licznik sesji
    devices, entities, states, _ = _load(WALLBOX)
    rest = [e for e in entities if e.entity_id != "sensor.wall_box_lifetime_energy"]
    (f,) = classify_chargers(devices, rest, states)
    assert f.roles["energy"].entity_id == "sensor.wall_box_session_energy"


def test_options_flow_stores_the_monitor_shape_without_control_roles():
    f, forms, saved = _flow(_finding())
    f.hass.states._attrs.update({eid: {} for eid in ROLES.values()})
    f.hass.states._attrs["sensor.wall_box_status"] = {"options": OPTIONS}
    asyncio.run(f.async_step_ev_charger({"chargers": ["dev-wb"]}))
    assert {str(k) for k in forms[-1].schema} == {"status", "power", "energy"}        # bez pól sterowania
    r = asyncio.run(f.async_step_ev_charger_roles(dict(ROLES)))
    assert r["type"] == "create_entry"
    assert saved["ev_chargers"] == [{"device_id": "dev-wb", "label": "Wall box", "control_class": "monitor",
                                     "roles": ROLES}]


def test_loads_reader_sends_status_power_and_lifetime_energy_only():
    states = FakeStates()
    states.set("sensor.wall_box_status", "charging", {"options": OPTIONS})
    states.set("sensor.wall_box_power", "7200", {"unit_of_measurement": "W"})
    states.set("sensor.wall_box_lifetime_energy", "12345600", {"unit_of_measurement": "Wh"})
    chargers = [{"device_id": "dev-wb", "label": "Wall box", "control_class": "monitor", "roles": ROLES}]
    (e,) = LoadsReader(SimpleNamespace(states=states), SimpleNamespace(options={"ev_chargers": chargers})).read()
    assert (e["status_raw"], e["power_w"], e["energy_kwh"], e["control_class"]) \
        == ("charging", 7200.0, 12345.6, "monitor")
    assert e["status_options"] == OPTIONS
    assert not {"setpoint", "start_stop_state", "executor"} & set(e)
    assert e["source"] == {"executor": "ha", "device_ref": "dev-wb"}      # `executor` tylko jako źródło odczytu


def test_inverter_entry_and_wallbox_together_give_one_inverter_and_one_monitor():
    inv_devs, inv_ents, inv_states, inv_entries = _load(REGISTRY)
    wb_devs, wb_ents, wb_states, wb_entries = _load(WALLBOX)
    profile = profile_from_dict(profile_raw())
    c = classify(inv_devs + wb_devs, inv_ents + wb_ents, inv_entries + wb_entries,
                 {**inv_states, **wb_states}, profiles=[profile])
    assert [(i.domain, i.matched_by) for i in c.inverters] == [("solar_assistant", "domain")]
    assert [(f.device_id, f.control_class) for f in c.chargers] == [("dev-wb", "monitor")]
