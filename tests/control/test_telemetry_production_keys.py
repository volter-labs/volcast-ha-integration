"""Czy telemetria sterowania niesie produkcję PV — znane przy złożeniu, przed pierwszą wysyłką.

Od tego zależy jedno źródło produkcji konta: gdy telemetria niesie moc PV, serwer kalibruje z niej
i lokalny tracker nie działa. Sam licznik energii PV nie wystarcza — serwer bez mocy PV nie wyliczy
z niego produkcji, więc wtedy tracker zostaje.
"""
from types import SimpleNamespace

import pytest

from custom_components.volcast.control.runtime import ControlRuntime
from custom_components.volcast.control.telemetry import TelemetrySender
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


@pytest.mark.parametrize("kwargs, expected", [
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
])
def test_sender_reports_pv_production_from_its_read_sources(kwargs, expected):
    assert _sender(**kwargs).reports_pv_production is expected


def _runtime(telemetry) -> ControlRuntime:
    return ControlRuntime(executor=None, fetcher=None, telemetry=telemetry, cloud=None, choice=None,
                          mapped={}, rated_power_w=None)


def test_runtime_asks_its_telemetry_sender():
    assert _runtime(_sender(choice=None, manual={"pv_power_w": "sensor.p"})).telemetry_reports_production
    assert not _runtime(_sender(choice=None)).telemetry_reports_production


def test_runtime_without_telemetry_reports_no_production():
    assert _runtime(None).telemetry_reports_production is False
