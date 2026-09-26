import json

from custom_components.volcast.core.discovery.models import (
    Classification, DeviceSnap, EntitySnap, InverterFinding, StateSnap)
from custom_components.volcast.core.discovery.network import LoggerReply, NetworkProbeResult
from custom_components.volcast.core.discovery.report import (
    build_report, compact_attributes, mask_serials, summarize)

SN = "2712345678"


def _cls(n_entities=2):
    dev = DeviceSnap("d1", "Deye", "SUN-10K-SG04LP3-EU", f"Deye {SN}", "1.0", None, SN,
                     (("solarman", SN),), ("e1",))
    ents = [EntitySnap(f"sensor.deye_{i}", "solarman", f"solarman_{SN}_s{i}", "d1", "e1",
                       None, None, None, None, False) for i in range(n_entities)]
    ents.append(EntitySnap("select.deye_work_mode", "solarman", f"solarman_{SN}_work_mode",
                           "d1", "e1", None, None, None, "Work mode", False))
    inv = InverterFinding("solarman", "e1", "Deye", "192.168.1.50", [dev], ents, "domain")
    return Classification([inv], [], [])


STATES = {"select.deye_work_mode": StateSnap("select.deye_work_mode", "Selling First",
          {"options": ["Selling First", "Zero Export To Load"]})}
NET = NetworkProbeResult(True, [LoggerReply(f"192.168.1.50,AABBCCDDEEFF,{SN}", "192.168.1.50", "AABBCCDDEEFF", SN)])


def _report(**kw):
    base = dict(classification=_cls(), states=STATES, history_days={}, network=NET,
                errors=[], integration_version="2.0.0b1", ha_version="2026.9.0",
                generated_at="2026-09-24T10:00:00+00:00")
    base.update(kw)
    return build_report(**base)


def test_serial_never_appears_anywhere_in_report():
    assert SN not in json.dumps(_report())


def test_select_options_and_masked_unique_id_kept():
    ent = [e for e in _report()["inverters"][0]["entities"] if e["entity_id"] == "select.deye_work_mode"][0]
    assert ent["options"] == ["Selling First", "Zero Export To Load"]
    assert ent["unique_id"] == "solarman_<SN>_work_mode"
    assert ent["entity_domain"] == "select"


def test_mac_and_reply_name_masked():
    rep = _report()["network"]["udp_48899"]["replies"][0]
    assert rep["mac"] == "AABBCC******" and rep["name"] == "…5678"


def test_summary_and_compact_attributes_bounded_for_huge_integration():
    r = _report(classification=_cls(n_entities=800))
    assert len(summarize(r)) <= 255
    assert len(json.dumps(compact_attributes(r))) <= 4096


def test_summary_when_nothing_found():
    r = _report(classification=Classification([], [], []), network=NetworkProbeResult(True, []))
    assert summarize(r) == "No inverter integration found · no price entities · network: 0 loggers"


def test_mask_serials_ignores_short_tokens():
    assert mask_serials("abc_12_x", {"12"}) == "abc_12_x"


# --- maskowanie w każdym polu raportu ---

ALNUM_SN = "HV2150012345"


def _cls_lowercase_entity_id():
    """Realistyczna migawka: HA slugifikuje serial do entity_id/unique_id małymi literami
    (np. integracje SMA/Deye), a klasyfikacja niesie serial w oryginalnej wielkości liter."""
    dev = DeviceSnap("d1", "SMA", "SB5.0", f"SMA {ALNUM_SN}", "1.0", None, ALNUM_SN,
                     (("sma", ALNUM_SN),), ("e1",))
    ent = EntitySnap(f"sensor.sn_{ALNUM_SN.lower()}_power", "sma",
                     f"sn_{ALNUM_SN.lower()}_power", "d1", "e1", None, None, None, None, False)
    inv = InverterFinding("sma", "e1", "SMA", "192.168.1.60", [dev], [ent], "domain")
    return Classification([inv], [], [])


def test_lowercase_entity_id_and_unique_id_masked():
    r = _report(classification=_cls_lowercase_entity_id())
    ent = r["inverters"][0]["entities"][0]
    assert ALNUM_SN.lower() not in ent["entity_id"]
    assert ALNUM_SN.lower() not in ent["unique_id"]
    assert ALNUM_SN.lower() not in json.dumps(r).lower()


def test_unparsed_raw_reply_serial_and_mac_masked():
    unparsed = LoggerReply(f"SN={SN};MAC=AABBCCDDEEFF", None, None, None)
    r = _report(network=NetworkProbeResult(True, [unparsed]))
    raw = r["network"]["udp_48899"]["replies"][0]["raw"]
    assert SN not in raw
    assert "AABBCCDDEEFF" not in raw
    assert "DDEEFF" not in json.dumps(r)


def test_error_string_masked_in_report_and_compact_attributes():
    r = _report(errors=[f"ha-ingestion: KeyError: sensor.deye_{SN}_power missing"])
    assert SN not in r["errors"][0]
    assert SN not in json.dumps(compact_attributes(r))


def test_padded_serial_masked():
    padded = "  SN123456\x00"
    dev = DeviceSnap("d1", "Deye", "SUN-10K", "Deye SN123456", "1.0", None, padded,
                     (), ("e1",))
    inv = InverterFinding("solarman", "e1", "Deye", "192.168.1.50", [dev], [], "domain")
    r = _report(classification=Classification([inv], [], []))
    assert "SN123456" not in json.dumps(r)


def test_serial_in_model_and_options_masked():
    dev = DeviceSnap("d1", "Deye", f"SUN-10K-{SN}", f"Deye {SN}", "1.0", None, SN, (), ("e1",))
    ent = EntitySnap("select.deye_mode", "solarman", "solarman_mode", "d1", "e1",
                     None, None, None, None, False)
    inv = InverterFinding("solarman", "e1", "Deye", "192.168.1.50", [dev], [ent], "domain")
    states = {"select.deye_mode": StateSnap("select.deye_mode", "auto",
              {"options": [f"Mode {SN}", "Zero Export"]})}
    r = _report(classification=Classification([inv], [], []), states=states)
    assert SN not in json.dumps(r)


# --- serial z separatorami, pola strukturalne, MAC ---

SEP_SN = "7F123456-78"


def test_separator_insensitive_serial_masks_slugified_entity_id():
    # HA slugify turns '-' into '_' and lowercases — the raw serial has a hyphen.
    dev = DeviceSnap("d1", "SolarEdge", "SE5000", f"SolarEdge {SEP_SN}", "1.0", None, SEP_SN,
                     (("solaredge_modbus_multi", SEP_SN),), ("e1",))
    ent = EntitySnap("sensor.solaredge_7f123456_78_power", "solaredge_modbus_multi",
                     "solaredge_7f123456_78_power", "d1", "e1", None, None, None, None, False)
    inv = InverterFinding("solaredge_modbus_multi", "e1", "SolarEdge", "192.168.1.70",
                          [dev], [ent], "domain")
    r = _report(classification=Classification([inv], [], []))
    entity = r["inverters"][0]["entities"][0]
    assert entity["entity_id"] == "sensor.solaredge_<SN>_power"
    assert entity["unique_id"] == "solaredge_<SN>_power"


def test_ip_and_wordy_identifier_do_not_corrupt_structural_fields():
    # identifier equal to the host IP, and a digit-less identifier equal to a domain word —
    # neither should ever become a *serial* candidate (so they can't corrupt unrelated
    # fields via substring match), and structural fields (host/domain, not identifiers)
    # stay untouched. The identifier *value* itself is still masked field-by-field
    # since it is emitted verbatim and may carry PII (e-mail, username).
    dev = DeviceSnap("d1", "Deye", "SUN-10K", f"Deye {SN}", "1.0", None, SN,
                     (("solarman", "192.168.1.50"), ("solarman", "solarman")), ("e1",))
    inv = InverterFinding("solarman", "e1", "Deye", "192.168.1.50", [dev], [], "domain")
    r = _report(classification=Classification([inv], [], []))
    assert r["inverters"][0]["host"] == "192.168.1.50"
    assert r["inverters"][0]["domain"] == "solarman"
    assert r["inverters"][0]["devices"][0]["identifiers"][0] == ["solarman", "<SN>"]
    assert r["inverters"][0]["devices"][0]["identifiers"][1] == ["solarman", "<SN>"]


def test_mac_adjacent_to_underscore_masked():
    ent = EntitySnap("sensor.aabbccddeeff_rssi", "solarman", "aabbccddeeff_rssi",
                     "d1", "e1", None, None, None, None, False)
    dev = DeviceSnap("d1", "Deye", "SUN-10K", f"Deye {SN}", "1.0", None, SN, (), ("e1",))
    inv = InverterFinding("solarman", "e1", "Deye", "192.168.1.50", [dev], [ent], "domain")
    r = _report(classification=Classification([inv], [], []))
    unique_id = r["inverters"][0]["entities"][0]["unique_id"]
    assert unique_id == "AABBCC******_rssi"


def test_plain_digit_run_not_masked_as_mac():
    states = {"select.deye_mode": StateSnap("select.deye_mode", "123456789012", {})}
    ent = EntitySnap("select.deye_mode", "solarman", "solarman_mode", "d1", "e1",
                     None, None, None, None, False)
    dev = DeviceSnap("d1", "Deye", "SUN-10K", f"Deye {SN}", "1.0", None, SN, (), ("e1",))
    inv = InverterFinding("solarman", "e1", "Deye", "192.168.1.50", [dev], [ent], "domain")
    r = _report(classification=Classification([inv], [], []), states=states)
    assert r["inverters"][0]["entities"][0]["state"] == "123456789012"


def test_options_tuple_masked_and_type_preserved():
    states = {"select.deye_mode": StateSnap("select.deye_mode", "auto",
              {"options": (f"Mode {SN}", "Zero Export")})}
    ent = EntitySnap("select.deye_mode", "solarman", "solarman_mode", "d1", "e1",
                     None, None, None, None, False)
    dev = DeviceSnap("d1", "Deye", "SUN-10K", f"Deye {SN}", "1.0", None, SN, (), ("e1",))
    inv = InverterFinding("solarman", "e1", "Deye", "192.168.1.50", [dev], [ent], "domain")
    r = _report(classification=Classification([inv], [], []), states=states)
    options = r["inverters"][0]["entities"][0]["options"]
    assert isinstance(options, tuple)
    assert SN not in "".join(options)


def test_mask_value_handles_dict_keys_and_sets_directly():
    from custom_components.volcast.core.discovery.report import _mask_value, _serial_pattern

    pattern = _serial_pattern({SN})
    masked = _mask_value({SN: "x", "opts": {f"a{SN}", "b"}}, pattern)
    assert SN not in "".join(masked.keys())
    assert isinstance(masked["opts"], set)
    assert SN not in "".join(masked["opts"])


# --- hostname, znane MAC-i, jednostki, identyfikatory, e-mail ---

def test_hostname_with_serial_masked_in_report_and_compact_attributes():
    # SMA's default hostname embeds the serial.
    dev = DeviceSnap("d1", "SMA", "SB5.0", f"SMA {SN}", "1.0", None, SN, (), ("e1",))
    inv = InverterFinding("sma", "e1", "SMA", f"SMA{SN}.local", [dev], [], "domain")
    r = _report(classification=Classification([inv], [], []))
    assert SN not in r["inverters"][0]["host"]
    assert SN not in json.dumps(compact_attributes(r))


def test_hostname_with_mac_masked():
    # ESP-based loggers name themselves "<brand>-<mac>.local"; device has no serial.
    dev = DeviceSnap("d1", "Deye", "SUN-10K", "Deye logger", "1.0", None, None, (), ("e1",))
    inv = InverterFinding("solarman", "e1", "Deye", "deye-aabbccddeeff.local", [dev], [], "domain")
    r = _report(classification=Classification([inv], [], []))
    assert "DDEEFF" not in r["inverters"][0]["host"].upper()


def test_digit_only_parsed_mac_masked_via_exact_match():
    # A MAC made only of digits 0-9 is not caught by the letter/separator heuristic —
    # it must still be masked because we know it's the parsed reply MAC.
    reply = LoggerReply("192.168.1.50,001122334455,LOGGER", "192.168.1.50", "001122334455", "LOGGER")
    ent = EntitySnap("sensor.001122334455_rssi", "solarman", "001122334455_rssi",
                     "d1", "e1", None, None, None, None, False)
    dev = DeviceSnap("d1", "Deye", "SUN-10K", "Deye logger", "1.0", None, None, (), ("e1",))
    inv = InverterFinding("solarman", "e1", "Deye", "192.168.1.50", [dev], [ent], "domain")
    r = _report(classification=Classification([inv], [], []),
               network=NetworkProbeResult(True, [reply]))
    entity = r["inverters"][0]["entities"][0]
    raw = r["network"]["udp_48899"]["replies"][0]["raw"]
    assert "001122334455" not in entity["unique_id"]
    assert "001122334455" not in raw
    assert entity["unique_id"] == "001122******_rssi"


def test_unit_field_with_serial_masked():
    states = {"sensor.deye_power": StateSnap("sensor.deye_power", "1000",
              {"unit_of_measurement": f"W {SN}"})}
    ent = EntitySnap("sensor.deye_power", "solarman", "solarman_power", "d1", "e1",
                     None, None, None, None, False)
    dev = DeviceSnap("d1", "Deye", "SUN-10K", f"Deye {SN}", "1.0", None, SN, (), ("e1",))
    inv = InverterFinding("solarman", "e1", "Deye", "192.168.1.50", [dev], [ent], "domain")
    r = _report(classification=Classification([inv], [], []), states=states)
    assert SN not in r["inverters"][0]["entities"][0]["unit"]


def test_identifier_value_masked_regardless_of_digits():
    dev = DeviceSnap("d1", "Deye", "SUN-10K", "Deye box", "1.0", None, None,
                     (("cloud_account", "john.doe@example.com"), ("legacy", "abc:extra")),
                     ("e1",))
    inv = InverterFinding("solarman", "e1", "Deye", "192.168.1.50", [dev], [], "domain")
    r = _report(classification=Classification([inv], [], []))
    ids = r["inverters"][0]["devices"][0]["identifiers"]
    assert ids[0] == ["cloud_account", "<SN>"]
    assert ids[1] == ["legacy", "<SN>"]


def test_email_masked_in_config_entry_title():
    dev = DeviceSnap("d1", "Deye", "SUN-10K", "Deye box", "1.0", None, None, (), ("e1",))
    inv = InverterFinding("solarman", "e1", "Contact admin@example.com", "192.168.1.50",
                          [dev], [], "domain")
    r = _report(classification=Classification([inv], [], []))
    title = r["inverters"][0]["config_entry_title"]
    assert "admin@example.com" not in title
    assert "<EMAIL>" in title
