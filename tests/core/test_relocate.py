"""Decyzja przepięcia celu bezpośredniego na nowy adres (czysta funkcja, fail-closed).

Adresy wyłącznie z bloków dokumentacyjnych (RFC 5737).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.volcast.core.discovery.identify import Candidate, Identity
from custom_components.volcast.core.discovery.probe import ProbeReport
from custom_components.volcast.core.discovery.relocate import (
    FP_MODEL, FP_SERIAL, Relocation, decide_relocation, fingerprint_kind)

OLD = "192.0.2.10"
NEW = "192.0.2.42"
OTHER = "198.51.100.7"
FP = "a1b2c3d4e5f60718"
FP_FOREIGN = "ffeeddccbbaa9988"


def _target(**over) -> dict:
    t = {"profile_id": "goodwe-et", "transport": "goodwe_udp", "host": OLD, "port": 8899, "unit_id": 247,
         "device_fp": FP, "fp_kind": FP_SERIAL, "unreadable": [], "capabilities": {}, "rated_power_w": 10000.0}
    t.update(over)
    return t


def _report(host: str, *, fp: str | None = FP, profile_id: str = "goodwe-et", transport: str = "goodwe_udp",
            port: int = 8899, unit_id: int = 247, rated: float | None = 10000.0, logger_serial: int | None = None,
            identified: bool = True, errors: tuple[str, ...] = ()) -> ProbeReport:
    ident = Identity(profile_id, transport, port, unit_id, "GW10K-ET", rated, fp) if identified else None
    return ProbeReport(ident, {}, (), identified, None, None, 4, errors, Candidate(host, "udp_48899", logger_serial))


def _no_clash(*reports: ProbeReport) -> dict[str, tuple[str, ...]]:
    return {r.candidate.host: () for r in reports}


def _decide(target, reports, clashes=None) -> Relocation:
    return decide_relocation(target, reports, clashes=_no_clash(*reports) if clashes is None else clashes)


def test_ip_change_relocates_to_the_single_matching_host():
    r = _decide(_target(), [_report(NEW)])
    assert r == Relocation("relocated", NEW, None)


def test_same_host_means_link_came_back_without_write():
    r = _decide(_target(), [_report(OLD)])
    assert r == Relocation("same_host", None, None)


def test_only_foreign_inverter_is_never_adopted():
    r = _decide(_target(), [_report(NEW, fp=FP_FOREIGN)])
    assert r == Relocation("foreign_only", None, None)


def test_two_hosts_with_the_same_fingerprint_are_ambiguous():
    r = _decide(_target(), [_report(NEW), _report(OTHER)])
    assert r == Relocation("ambiguous", None, None)


def test_two_hosts_including_the_current_one_are_ambiguous():
    r = _decide(_target(), [_report(OLD), _report(NEW)])
    assert r.outcome == "ambiguous" and r.host is None


def test_same_host_reported_twice_counts_once():
    r = _decide(_target(), [_report(NEW), _report(NEW)])
    assert r == Relocation("relocated", NEW, None)


def test_model_fingerprint_twin_at_new_host_is_never_adopted():
    # Oryginał wyłączony (nie ma go w wyniku), bliźniak tego samego modelu i mocy żyje pod innym adresem:
    # odcisk model|moc jest identyczny, więc nie da się udowodnić, że to ten sam egzemplarz.
    t = _target(profile_id="foxess-h", fp_kind=FP_MODEL)
    r = _decide(t, [_report(NEW, profile_id="foxess-h")])
    assert r == Relocation("not_found", None, None)


def test_model_fingerprint_with_single_perfect_candidate_is_not_found():
    t = _target(profile_id="foxess-h", fp_kind=FP_MODEL)
    r = _decide(t, [_report(NEW, profile_id="foxess-h"), _report(OTHER, fp=FP_FOREIGN)])
    assert r == Relocation("not_found", None, None)


def test_model_fingerprint_on_the_current_host_is_not_found():
    t = _target(profile_id="foxess-h", fp_kind=FP_MODEL)
    assert _decide(t, [_report(OLD, profile_id="foxess-h")]) == Relocation("not_found", None, None)


def test_serial_fingerprint_tolerates_unidentified_and_same_profile_devices():
    r = _decide(_target(), [_report(NEW), _report(OTHER, fp=FP_FOREIGN),
                            _report("198.51.100.8", identified=False, errors=("timeout",))])
    assert r == Relocation("relocated", NEW, None)


@pytest.mark.parametrize("fp_kind", [None, "", "unknown", FP_MODEL])
def test_non_serial_fingerprint_kind_never_relocates(fp_kind):
    t = _target(fp_kind=fp_kind)
    if fp_kind is None:
        del t["fp_kind"]
    assert _decide(t, [_report(NEW)]) == Relocation("not_found", None, None)


def test_ipv4_mapped_target_host_is_the_same_host():
    assert _decide(_target(host="::ffff:" + NEW), [_report(NEW)]) == Relocation("same_host", None, None)


@pytest.mark.parametrize("over", [{"profile_id": "goodwe-dt"}, {"transport": "modbus_tcp"}, {"port": 502},
                                  {"unit_id": 1}])
def test_matching_fingerprint_with_different_profile_transport_port_or_unit_is_not_adopted(over):
    r = _decide(_target(), [_report(NEW, **over)])
    assert r == Relocation("foreign_only", None, None)


def test_address_used_by_another_integration_is_a_clash():
    reports = [_report(NEW)]
    r = decide_relocation(_target(), reports, clashes={NEW: ("goodwe",)})
    assert r == Relocation("clash", None, None)


def test_address_without_clash_check_is_treated_as_clash():
    r = decide_relocation(_target(), [_report(NEW)], clashes={})
    assert r == Relocation("clash", None, None)


def test_clash_on_other_hosts_does_not_block_relocation():
    reports = [_report(NEW), _report(OTHER, fp=FP_FOREIGN)]
    r = decide_relocation(_target(), reports, clashes={NEW: (), OTHER: ("solarman",)})
    assert r == Relocation("relocated", NEW, None)


def test_empty_result_is_not_found():
    assert _decide(_target(), []) == Relocation("not_found", None, None)


def test_only_unidentified_devices_are_not_found():
    r = _decide(_target(), [_report(NEW, identified=False, errors=("conflict",))])
    assert r == Relocation("not_found", None, None)


@pytest.mark.parametrize("fp", [None, "", 0])
def test_target_without_device_fingerprint_never_relocates(fp):
    t = _target(device_fp=fp)
    if fp is None:
        del t["device_fp"]
    # Raport bez odcisku (pusty) też nie może „pasować” do celu bez odcisku.
    r = _decide(t, [_report(NEW, fp=""), _report(OTHER)])
    assert r == Relocation("not_found", None, None)


@pytest.mark.parametrize("key", ["profile_id", "transport"])
def test_target_without_profile_or_transport_never_relocates(key):
    t = _target()
    del t[key]
    assert _decide(t, [_report(NEW)]) == Relocation("not_found", None, None)


def test_logger_serial_carried_over_for_solarman_v5():
    t = _target(profile_id="deye-sun", transport="solarman_v5", port=8899, unit_id=1, logger_serial=1234567890)
    r = _decide(t, [_report(NEW, profile_id="deye-sun", transport="solarman_v5", unit_id=1,
                            logger_serial=2345678901)])
    assert r == Relocation("relocated", NEW, 2345678901)


def test_solarman_v5_without_logger_serial_in_report_leaves_it_unchanged():
    t = _target(profile_id="deye-sun", transport="solarman_v5", unit_id=1, logger_serial=1234567890)
    r = _decide(t, [_report(NEW, profile_id="deye-sun", transport="solarman_v5", unit_id=1)])
    assert r == Relocation("relocated", NEW, None)


def test_conflicting_logger_serials_for_the_same_host_are_ambiguous():
    t = _target(profile_id="deye-sun", transport="solarman_v5", unit_id=1)
    kw = {"profile_id": "deye-sun", "transport": "solarman_v5", "unit_id": 1}
    r = _decide(t, [_report(NEW, logger_serial=1111111111, **kw), _report(NEW, logger_serial=2222222222, **kw)])
    assert r == Relocation("ambiguous", None, None)


def test_logger_serial_ignored_for_other_transports():
    r = _decide(_target(), [_report(NEW, logger_serial=1234567890)])
    assert r == Relocation("relocated", NEW, None)


def test_relocation_repr_hides_address_and_logger_serial():
    text = repr(Relocation("relocated", NEW, 2345678901))
    assert NEW not in text and "2345678901" not in text and "relocated" in text


def test_fingerprint_kind_follows_profile_serial_register():
    with_serial = SimpleNamespace(raw={"identify": {"registers": {"serial": {}, "model": {}}}})
    without = SimpleNamespace(raw={"identify": {"registers": {"model": {}}}})
    assert fingerprint_kind(with_serial) == FP_SERIAL
    assert fingerprint_kind(without) == FP_MODEL
    assert fingerprint_kind(SimpleNamespace(raw={})) == FP_MODEL


def test_fingerprint_kind_matches_packaged_profiles():
    from custom_components.volcast.core.profile import load_builtin
    assert fingerprint_kind(load_builtin("goodwe-et")) == FP_SERIAL
    assert fingerprint_kind(load_builtin("foxess-h")) == FP_MODEL
