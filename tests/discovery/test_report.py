import json

import pytest

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


# --- seriale urządzeń spoza znalezisk falownika ---

SNE = "122012345678"


def _envoy():
    """Enphase Envoy: domena spoza listy falowników, serial w entity_id czujnika energii."""
    dev = DeviceSnap("env", "Enphase", "Envoy", f"Envoy {SNE}", "7.0", None, SNE,
                     (("enphase_envoy", SNE),), ("en1",))
    ent = EntitySnap(f"sensor.envoy_{SNE}_lifetime_energy_production", "enphase_envoy",
                     f"{SNE}_lifetime_energy_production", "env", "en1", "energy", "kWh",
                     None, None, False)
    return dev, ent


def test_serial_of_energy_sensor_device_masked():
    dev, ent = _envoy()
    r = _report(classification=Classification([], [], [ent]), devices=[dev])
    assert SNE not in json.dumps(r)
    assert r["energy_sensors"][0]["entity_id"] == "sensor.envoy_<SN>_lifetime_energy_production"


def test_serial_of_price_entity_device_masked():
    dev = DeviceSnap("pd", "Tibber", "Pulse", "Home", None, None, "PULSE998877",
                     (("tibber", "PULSE998877"),), ("t1",))
    ent = EntitySnap("sensor.pulse998877_price", "tibber", "pulse998877_price", "pd", "t1",
                     None, None, None, None, False)
    r = _report(classification=Classification([], [ent], []), devices=[dev])
    assert "pulse998877" not in json.dumps(r).lower()


def test_device_not_behind_reported_entity_does_not_add_serials():
    dev, ent = _envoy()
    other = DeviceSnap("x", "Hue", "Bridge", "Bridge", None, None, "BRIDGE12345678", (), ("h1",))
    r = _report(classification=Classification([], [], [ent]), devices=[dev, other],
                errors=["note BRIDGE12345678"])
    assert r["errors"] == ["note BRIDGE12345678"]


# --- ograniczony koszt maskowania ---

import time  # noqa: E402

ADVERSARIAL = [
    "a" * 50_000,
    "a@" + "a" * 50_000,
    "a" * 25_000 + "@" + "a" * 25_000,
    "a@a." * 12_500,
    "-" * 50_000 + "@x.com",
    ("ab:" * 17_000)[:50_000],
]


def test_masking_of_huge_strings_is_bounded_and_still_masks():
    head = f"owner john.doe@example.com sn {SN} "
    errors = [head + tail for tail in ADVERSARIAL]
    t0 = time.perf_counter()
    r = _report(errors=errors)
    elapsed = time.perf_counter() - t0
    # Maskowanie biegnie teraz po CAŁYM tekście przed cięciem (nie tylko po buforze
    # przed _MAX_TEXT), więc te sześć 50 000-znakowych ciągów kosztuje realnie więcej
    # niż poprzednio (~0,1 s zamiast ułamka) — to jest oczekiwany, liniowy koszt, nie
    # katastrofalny nawrót; próg zostaje z dużym zapasem, bo nawrót kwadratowy/wykładniczy
    # i tak zajmuje sekundy, nie ułamki sekundy.
    assert elapsed < 1.0, elapsed
    for e in r["errors"]:
        assert len(e) <= 2049 and e.endswith("…")
        assert "john.doe@example.com" not in e and "<EMAIL>" in e
        assert SN not in e


def test_short_strings_are_not_truncated():
    r = _report(errors=["x" * 2048])
    assert r["errors"] == ["x" * 2048]


def test_email_with_long_parts_still_masked():
    # część lokalna dłuższa niż 64 znaki: maskowane jest co najmniej ostatnie 64 + domena
    local, host = "l" * 80, "h" * 60
    r = _report(errors=[f"{local}@{host}.example.com"])
    assert "@" not in r["errors"][0] and host not in r["errors"][0]


def test_known_mac_masking_scales_to_many_macs_and_entities():
    macs = [f"0011223344{i:02X}" for i in range(32)]
    replies = [LoggerReply(f"192.168.1.{i},{m},LOGGER", f"192.168.1.{i}", m, "LOGGER")
               for i, m in enumerate(macs)]
    dev = DeviceSnap("d1", "Deye", "SUN-10K", "Deye logger", "1.0", None, None, (), ("e1",))
    ents = [EntitySnap(f"sensor.logger_{macs[i % 32].lower()}_s{i}", "solarman",
                       f"{macs[i % 32]}_s{i}", "d1", "e1", None, None, None, f"Sensor {i}", False)
            for i in range(800)]
    inv = InverterFinding("solarman", "e1", "Deye", "192.168.1.50", [dev], ents, "domain")
    t0 = time.perf_counter()
    r = _report(classification=Classification([inv], [], []),
                network=NetworkProbeResult(True, replies))
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.2, elapsed
    dumped = json.dumps(r).upper()
    assert not any(m in dumped for m in macs)
    assert r["inverters"][0]["entities"][0]["unique_id"] == "001122******_s0"


def test_device_class_and_state_class_values_are_masked():
    states = {"sensor.deye_energy": StateSnap("sensor.deye_energy", "1", {
        "device_class": f"energy {SN}", "state_class": f"total {SN}"})}
    ent = EntitySnap("sensor.deye_energy", "solarman", "solarman_energy", "d1", "e1",
                     None, None, None, None, False)
    dev = DeviceSnap("d1", "Deye", "SUN-10K", f"Deye {SN}", "1.0", None, SN, (), ("e1",))
    inv = InverterFinding("solarman", "e1", "Deye", "192.168.1.50", [dev], [ent], "domain")
    r = _report(classification=Classification([inv], [], [ent]), states=states)
    e = r["inverters"][0]["entities"][0]
    assert SN not in e["device_class"] and SN not in e["state_class"]
    assert SN not in r["energy_sensors"][0]["state_class"]


def test_ha_vocabulary_in_device_class_and_state_class_unchanged():
    states = {"sensor.deye_energy": StateSnap("sensor.deye_energy", "1", {
        "device_class": "energy", "state_class": "total_increasing"})}
    ent = EntitySnap("sensor.deye_energy", "solarman", "solarman_energy", "d1", "e1",
                     None, None, None, None, False)
    dev = DeviceSnap("d1", "Deye", "SUN-10K", f"Deye {SN}", "1.0", None, SN, (), ("e1",))
    inv = InverterFinding("solarman", "e1", "Deye", "192.168.1.50", [dev], [ent], "domain")
    r = _report(classification=Classification([inv], [], [ent]), states=states)
    e = r["inverters"][0]["entities"][0]
    assert (e["device_class"], e["state_class"]) == ("energy", "total_increasing")
    assert r["energy_sensors"][0]["state_class"] == "total_increasing"


# --- atrybuty sensora zawsze <= 4096 bajtów ---

def _big_report(n_inv=5, n_models=30, text=200, n_errors=5, n_prices=50):
    devs = [{"model": f"M{j}-" + "x" * text} for j in range(n_models)]
    inv = {"domain": "d" * text, "brand_hint": "b" * text, "host": "h" * text,
           "devices": devs, "entities": [{}] * 3}
    return {
        "schema": 1, "generated_at": "g" * text, "inverters": [dict(inv) for _ in range(n_inv)],
        "price_entities": [{"platform": f"p{i}" + "y" * text} for i in range(n_prices)],
        "energy_sensors": [{"days_of_statistics": 7}],
        "network": {"udp_48899": {"sent": True, "replies": []}},
        "errors": ["e" * 2049] * n_errors,
    }


@pytest.mark.parametrize("kw", [
    {}, {"text": 5000}, {"n_inv": 50, "n_models": 500}, {"n_errors": 500},
    {"n_prices": 5000, "text": 2000}, {"text": 20000, "n_models": 100},
])
def test_compact_attributes_never_exceed_4096_bytes(kw):
    attrs = compact_attributes(_big_report(**kw))
    assert len(json.dumps(attrs).encode()) <= 4096
    assert attrs["schema"] == 1


def test_compact_attributes_caps_models_and_text():
    attrs = compact_attributes(_big_report())
    inv = attrs["inverters"][0]
    assert len(inv["models"]) <= 5
    assert all(len(m) <= 64 for m in inv["models"])
    assert len(inv["host"]) <= 64 and len(inv["brand_hint"]) <= 64


def test_compact_attributes_keep_everything_for_normal_report():
    r = _report()
    attrs = compact_attributes(r)
    assert attrs["inverters"][0]["models"] == ["SUN-10K-SG04LP3-EU"]
    assert attrs["inverters"][0]["host"] == "192.168.1.50"
    assert "truncated" not in attrs


# --- podsumowanie przy nieudanym przebiegu ---

def _empty(errors):
    return _report(classification=Classification([], [], []), states={}, network=None,
                   errors=errors)


def test_summary_reports_timeout_as_failure():
    assert summarize(_empty(["timeout"])) == "Discovery failed: timeout"


def test_summary_reports_runner_failure():
    s = summarize(_empty(["runner: RuntimeError: boom"]))
    assert s == "Discovery failed — see diagnostics"
    assert summarize(_empty(["runner: report build failed"])) == s


def test_summary_step_errors_do_not_hide_findings():
    r = _report(errors=["history: recorder not loaded"])
    assert summarize(r).startswith("solarman (SUN-10K-SG04LP3-EU)")


def _solarman_cls(manufacturer, model):
    dev = DeviceSnap("d1", manufacturer, model, "Inverter", "1.0", None, None, (), ("e1",))
    return Classification([InverterFinding("solarman", "e1", "Inverter", None, [dev], [], "domain")], [], [])


def test_ambiguous_profile_surfaces_in_report_and_sensor_attributes():
    from custom_components.volcast.core.profile import builtin_ids, load_builtin
    profiles = [load_builtin(i) for i in builtin_ids()]
    r = _report(classification=_solarman_cls("Solarman", None), profiles=profiles)
    assert r["inverters"][0]["profile_candidates"] == ["sofar-hyd", "solis-hybrid"]
    assert compact_attributes(r)["inverters"][0]["profile_candidates"] == ["sofar-hyd", "solis-hybrid"]
    # Tekst urządzenia rozstrzyga — wtedy nie ma czego zgłaszać.
    clear = _report(classification=_solarman_cls("Ginlong", "S6-EH1P"), profiles=profiles)
    assert "profile_candidates" not in clear["inverters"][0]
    assert "profile_candidates" not in compact_attributes(clear)["inverters"][0]
    # Bez profili (np. pusty raport awaryjny) raport wygląda jak dotąd.
    assert "profile_candidates" not in _report(classification=_solarman_cls("Solarman", None))["inverters"][0]
