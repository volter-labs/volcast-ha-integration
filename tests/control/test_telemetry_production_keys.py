"""Czy telemetria sterowania niesie produkcję PV — znane przy złożeniu, przed pierwszą wysyłką.

Od tego zależy jedno źródło produkcji konta: gdy telemetria niesie moc PV, serwer kalibruje z niej
i lokalny tracker nie działa. Sam licznik energii PV nie wystarcza — serwer bez mocy PV nie wyliczy
z niego produkcji, więc wtedy tracker zostaje.
"""
from collections.abc import Mapping
from types import SimpleNamespace

import pytest

from custom_components.volcast.control.runtime import ControlRuntime
from custom_components.volcast.control.telemetry import TelemetrySender, telemetry_carries_pv_power
from custom_components.volcast.core.control.select import ProfileChoice
from custom_components.volcast.core.profile import load_builtin

GW = ProfileChoice(load_builtin("goodwe-et"), "goodwe", "GW8KN-ET")
GW_READ_ONLY = ProfileChoice(load_builtin("goodwe-et"), None, "GW8KN-ET")
SOLAREDGE = ProfileChoice(load_builtin("solaredge-storedge"), "solaredge_modbus", "SE")
# Profil, którego rejestry niosą tylko licznik energii PV (bez mocy PV).
COUNTER_ONLY = ProfileChoice(SimpleNamespace(raw={"read": {"pv_energy_total_kwh": {}, "soc": {}}}), None, "X")


def _sender(*, choice, profile_map=None, manual=None, direct=None) -> TelemetrySender:
    entry = SimpleNamespace(entry_id="e1", options={})
    return TelemetrySender(SimpleNamespace(), entry, None, None, choice=choice,
                           profile_map=profile_map or {}, manual_map=manual or {}, grid_negate=False,
                           limits=None, direct=direct)


CASES = [
    # tryb encji: liczą się encje faktycznie zmapowane, nie wszystko, co opisuje profil
    ({"choice": GW, "profile_map": {"pv_power_w": "sensor.gw_pv_power"}}, True),
    ({"choice": GW, "profile_map": {"soc": "sensor.gw_soc"}}, False),
    ({"choice": SOLAREDGE, "profile_map": {"soc": "sensor.se_soc"}}, False),
    # profil tylko do odczytu (bez integracji): mapa profilu jest pusta, zostaje ręczna
    ({"choice": GW_READ_ONLY, "profile_map": {"pv_power_w": "sensor.x"}}, False),
    ({"choice": GW_READ_ONLY, "manual": {"pv_power_w": "sensor.pv_power"}}, True),
    # sam licznik energii PV nie niesie produkcji (serwer potrzebuje mocy PV)
    ({"choice": GW_READ_ONLY, "manual": {"pv_energy_total_kwh": "sensor.pv_total"}}, False),
    ({"choice": GW, "profile_map": {"pv_energy_total_kwh": "sensor.gw_pv_total"}}, False),
    ({"choice": COUNTER_ONLY, "direct": SimpleNamespace()}, False),
    # tryb bezpośredni: rejestry profilu (`read`) niosą moc i licznik PV
    ({"choice": GW, "direct": SimpleNamespace()}, True),
    # bez falownika — tylko mapa ręczna
    ({"choice": None}, False),
    ({"choice": None, "manual": {"pv_power_w": "sensor.pv_power"}}, True),
    ({"choice": None, "manual": {"pv_power_w": "sensor.p", "pv_energy_total_kwh": "sensor.e"}}, True),
    ({"choice": None, "manual": {"soc": "sensor.soc"}}, False),
]


@pytest.mark.parametrize("kwargs, expected", CASES)
def test_sender_reports_pv_production_from_its_read_sources(kwargs, expected):
    assert _sender(**kwargs).reports_pv_production is expected


@pytest.mark.parametrize("kwargs, expected", CASES)
def test_prediction_before_composing_uses_the_same_rule(kwargs, expected):
    # Opcje (przed złożeniem sterowania) przewidują to samo, co nadajnik powie po złożeniu.
    assert telemetry_carries_pv_power(kwargs["choice"], profile_map=kwargs.get("profile_map") or {},
                                      manual_map=kwargs.get("manual") or {},
                                      direct=kwargs.get("direct") is not None) is expected


def _runtime(telemetry) -> ControlRuntime:
    return ControlRuntime(executor=None, fetcher=None, telemetry=telemetry, cloud=None, choice=None,
                          mapped={}, rated_power_w=None)


def test_runtime_asks_its_telemetry_sender():
    assert _runtime(_sender(choice=None, manual={"pv_power_w": "sensor.p"})).telemetry_reports_production
    assert not _runtime(_sender(choice=None)).telemetry_reports_production


def test_runtime_without_telemetry_reports_no_production():
    assert _runtime(None).telemetry_reports_production is False


def _thaw(v):
    if isinstance(v, Mapping):
        return {k: _thaw(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_thaw(x) for x in v]
    return v


def _addrs(spec) -> list[int]:
    terms = spec["sum"] if isinstance(spec, dict) and "sum" in spec else [spec]
    return [t["addr"] for t in terms if "addr" in t]


def test_direct_pv_power_register_is_always_polled():
    """Bezpośrednio rejestr `pv_power_w` zawsze jest w planie odczytu klienta, więc reguła może go liczyć
    bez celu sondy: klient pomija tylko klucze bez odczytu (`unreadable` = `echo_only` sondy) i bez rejestru
    (`unsupported` = możliwości False), a oba zbiory pochodzą z `modbus.probe_keys`, które schemat ogranicza
    do kluczy zapisu — `pv_power_w` nim nie jest i nie może być."""
    import copy

    from custom_components.volcast.core.modbus.blocks import read_plan
    from custom_components.volcast.core.params import WRITE_PARAMS
    from custom_components.volcast.core.profile import builtin_ids
    from custom_components.volcast.core.profile_schema import validate_profile
    assert "pv_power_w" not in WRITE_PARAMS
    checked = 0
    for pid in builtin_ids():
        p = load_builtin(pid)
        if "pv_power_w" not in p.raw["read"]:
            continue
        skipped = set(p.modbus.probe_keys) | set(p.modbus.echo_only)
        polled = {a for b in read_plan(p, exclude=skipped) for a in range(b[0], b[0] + b[1])}
        assert set(_addrs(p.raw["read"]["pv_power_w"])) <= polled, pid
        checked += 1
        raw = _thaw(p.raw)
        for field in ("probe_keys", "echo_only"):
            bad = copy.deepcopy(raw)
            bad["modbus"][field] = [*bad["modbus"].get(field, ()), "pv_power_w"]
            assert any(f"$.modbus.{field}" in e for e in validate_profile(bad)), (pid, field)
    assert checked >= 5
