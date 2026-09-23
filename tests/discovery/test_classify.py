from custom_components.volcast.core.discovery.models import (
    ConfigEntrySnap, DeviceSnap, EntitySnap, StateSnap,
)
from custom_components.volcast.core.discovery.classify import classify


def _dev(id, manufacturer, entry="e1", model="M"):
    return DeviceSnap(id=id, manufacturer=manufacturer, model=model, name=None,
                      sw_version=None, hw_version=None, serial_number=None,
                      identifiers=(), config_entry_ids=(entry,))


def _ent(eid, platform, device_id=None, entry="e1", device_class=None, unit=None, uid=None):
    return EntitySnap(entity_id=eid, platform=platform, unique_id=uid or eid,
                      device_id=device_id, config_entry_id=entry, device_class=device_class,
                      unit=unit, translation_key=None, original_name=None, disabled=False)


def test_inverter_found_by_domain_with_host_and_entities():
    entries = [ConfigEntrySnap("e1", "solarman", "Deye", "192.168.1.50")]
    devs = [_dev("d1", "Deye")]
    ents = [_ent("sensor.battery_soc", "solarman", "d1"), _ent("select.work_mode", "solarman", "d1")]
    c = classify(devs, ents, entries, {})
    assert len(c.inverters) == 1
    inv = c.inverters[0]
    assert (inv.domain, inv.host, inv.matched_by) == ("solarman", "192.168.1.50", "domain")
    assert [e.entity_id for e in inv.entities] == ["sensor.battery_soc", "select.work_mode"]


def test_inverter_found_by_manufacturer_when_domain_unknown():
    entries = [ConfigEntrySnap("m1", "mqtt", "MQTT", None)]
    devs = [_dev("d9", "Sunsynk Ltd", entry="m1")]
    ents = [_ent("sensor.ss_soc", "mqtt", "d9", entry="m1")]
    c = classify(devs, ents, entries, {})
    assert c.inverters[0].matched_by == "manufacturer"
    assert c.inverters[0].domain == "mqtt"
    assert [e.entity_id for e in c.inverters[0].entities] == ["sensor.ss_soc"]


def test_no_inverter_no_prices_is_empty_not_error():
    c = classify([], [], [], {})
    assert c.inverters == [] and c.price_entities == [] and c.energy_candidates == []


def test_price_entities_by_platform():
    ents = [_ent("sensor.nordpool_kwh_pl", "nordpool", entry="p1"), _ent("sensor.x", "template")]
    c = classify([], ents, [], {})
    assert [e.entity_id for e in c.price_entities] == ["sensor.nordpool_kwh_pl"]


def test_energy_candidates_prioritise_load_and_cap_at_30():
    ents = [_ent(f"sensor.e{i}", "template", device_class="energy", unit="kWh") for i in range(40)]
    ents.append(_ent("sensor.house_consumption", "template", device_class="energy", unit="kWh"))
    c = classify([], ents, [], {})
    assert len(c.energy_candidates) == 30
    assert c.energy_candidates[0].entity_id == "sensor.house_consumption"


def test_energy_candidate_uses_state_device_class_when_registry_empty():
    ents = [_ent("sensor.pv_today", "template")]
    states = {"sensor.pv_today": StateSnap("sensor.pv_today", "3.2",
              {"device_class": "energy", "unit_of_measurement": "kWh", "state_class": "total_increasing"})}
    c = classify([], ents, [], states)
    assert [e.entity_id for e in c.energy_candidates] == ["sensor.pv_today"]


def test_manufacturer_substring_false_positives_rejected():
    # "sma" is a real inverter manufacturer token, but must not match as a
    # bare substring inside unrelated ZHA/Zigbee manufacturer names.
    entries = [ConfigEntrySnap("z1", "zha", "ZHA", None)]
    devs = [
        _dev("d1", "SmartThings", entry="z1"),
        _dev("d2", "Smartmi", entry="z1"),
        _dev("d3", "Smappee", entry="z1"),
        _dev("d4", "Smart Life", entry="z1"),
    ]
    c = classify(devs, [], entries, {})
    assert c.inverters == []


def test_manufacturer_whole_word_matches_accepted():
    entries = [ConfigEntrySnap("z2", "zha", "ZHA", None)]
    devs = [
        _dev("d5", "SMA Solar Technology AG", entry="z2"),
        _dev("d6", "Sunsynk Ltd", entry="z2"),
        _dev("d7", "GoodWe", entry="z2"),
    ]
    c = classify(devs, [], entries, {})
    assert len(c.inverters) == 3
    assert all(inv.matched_by == "manufacturer" for inv in c.inverters)


def test_energy_candidates_exclude_disabled():
    e = EntitySnap(entity_id="sensor.disabled_energy", platform="template",
                    unique_id="sensor.disabled_energy", device_id=None, config_entry_id=None,
                    device_class="energy", unit="kWh", translation_key=None, original_name=None,
                    disabled=True)
    c = classify([], [e], [], {})
    assert c.energy_candidates == []
