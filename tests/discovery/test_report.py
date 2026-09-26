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
