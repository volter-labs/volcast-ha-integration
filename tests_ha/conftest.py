"""Wspólne przygotowanie zestawu dymnego na prawdziwym rdzeniu Home Assistant.

Główny zestaw (`tests/`) stoi na atrapach modułów HA; tutaj ładuje się prawdziwy rdzeń
(pytest-homeassistant-custom-component), więc sprawdzamy nazwy API, szynę usług,
przepływy konfiguracji i cykl życia wpisu tak, jak zachowują się w bieżącym HA.
"""
from __future__ import annotations

import asyncio
import re
from unittest.mock import AsyncMock

import aiohttp
import pytest

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.volcast.const import DOMAIN

API_KEY = "vk_" + "0123456789abcdef" * 4
BASE = "https://cloud.example.test"
BACKEND = {"base_url": BASE, **{k: f"{BASE}/functions/v1/{k}" for k in (
    "forecast", "submit_production", "telemetry", "schedule", "history_import", "pairing")}}
PAIRED_DATA = {"api_key": API_KEY, "api_url": BACKEND["forecast"], "backend": BACKEND, "user_id": "u1"}

# Encje falownika jak z integracji GoodWe (identyfikatory testowe, bez numerów seryjnych).
SN = "TESTSN0001"
GOODWE = {
    # klucz: (domena, fragment unique_id, object_id, stan, atrybuty)
    "soc": ("sensor", "battery_soc", "goodwe_battery_soc", "60", {"unit_of_measurement": "%"}),
    "battery_temp_c": ("sensor", "battery_temperature", "goodwe_battery_temperature", "25",
                       {"unit_of_measurement": "°C"}),
    "mode": ("select", "ems_mode", "goodwe_ems_mode", "auto",
             {"options": ["auto", "charge_pv", "battery_standby", "sell_power", "charge_battery",
                          "discharge_battery"]}),
    "power_w": ("number", "ems_power_limit", "goodwe_ems_power_limit", "0",
                {"min": 0, "max": 10000, "step": 1, "unit_of_measurement": "W"}),
    "soc_min": ("number", "battery_discharge_depth", "goodwe_depth_of_discharge", "85",
                {"min": 0, "max": 99, "step": 1, "unit_of_measurement": "%"}),
    "soc_max": ("number", "soc_upper_limit", "goodwe_soc_upper_limit", "100",
                {"min": 10, "max": 100, "step": 1, "unit_of_measurement": "%"}),
    "export_limit_w": ("number", "grid_export_limit", "goodwe_grid_export_limit", "4000",
                       {"min": 0, "max": 10000, "step": 1, "unit_of_measurement": "W"}),
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def no_udp_probe(monkeypatch):
    """Wykrywanie w teście nie wysyła pakietów w sieć (harness HA i tak blokuje gniazda)."""
    from custom_components.volcast import discovery_runner

    probe = AsyncMock(return_value=None)
    monkeypatch.setattr(discovery_runner, "probe_udp_48899", probe)
    return probe


class _NoWaitAsyncio:
    """`asyncio` dla modułu ponowień bez realnych przerw między próbami (5/15/45 s)."""

    def __getattr__(self, name):
        return getattr(asyncio, name)

    @staticmethod
    async def sleep(_delay, result=None):
        await asyncio.sleep(0)
        return result


@pytest.fixture
def network_down(aioclient_mock, monkeypatch):
    """Każde żądanie HTTP kończy się błędem połączenia (brak sieci przy starcie)."""
    from custom_components.volcast import http_retry

    monkeypatch.setattr(http_retry, "asyncio", _NoWaitAsyncio())
    anything = re.compile(r".*")
    for method in ("get", "post", "put", "delete"):
        getattr(aioclient_mock, method)(anything, exc=aiohttp.ClientConnectionError())
    return aioclient_mock


class InverterServices:
    """Usługi `select/number/switch` jak integracja falownika: zapis zmienia stan encji
    z kontekstem wywołania (tak robią integracje odświeżające stan po zapisie)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.calls = []
        for domain, service in (("select", "select_option"), ("number", "set_value"),
                                ("switch", "turn_on"), ("switch", "turn_off")):
            hass.services.async_register(domain, service, self._handle)

    async def _handle(self, call) -> None:
        self.calls.append(call)
        eids = call.data["entity_id"]
        for eid in eids if isinstance(eids, list) else [eids]:
            st = self.hass.states.get(eid)
            attrs = dict(st.attributes) if st else {}
            if call.service == "select_option":
                value = call.data["option"]
            elif call.service == "set_value":
                value = str(call.data["value"])
            else:
                value = "on" if call.service == "turn_on" else "off"
            self.hass.states.async_set(eid, value, attrs, context=call.context)

    def written(self) -> dict:
        out = {}
        for c in self.calls:
            out[c.data["entity_id"]] = c.data.get("option", c.data.get("value", c.service))
        return out


def add_goodwe(hass: HomeAssistant, *, switch_on: bool = True, **states) -> dict[str, str]:
    """Encje falownika w rejestrze (platforma `goodwe`) i ich stany; zwraca klucz → entity_id."""
    reg = er.async_get(hass)
    out = {}
    for key, (domain, uid, obj, state, attrs) in GOODWE.items():
        ent = reg.async_get_or_create(domain, "goodwe", f"goodwe-{uid}-{SN}", suggested_object_id=obj)
        hass.states.async_set(ent.entity_id, states.get(key, state), attrs)
        out[key] = ent.entity_id
    sw = reg.async_get_or_create("switch", "goodwe", f"grid_export_limit_switch-{SN}",
                                 suggested_object_id="goodwe_grid_export_limit_switch")
    hass.states.async_set(sw.entity_id, "on" if switch_on else "off", {})
    out["export_limit_enabled"] = sw.entity_id
    return out


ENTITY_OPTIONS = {"control_mode": "entities", "profile_id": "goodwe-et", "inverter_domain": "goodwe"}
# Zapis magazynu sterowania: to my zmieniliśmy nastawy (sell_power, eksport 0 W), migawka sprzed zapisu.
OWNED = {"owned": True, "consent": True, "local_switch": True,
         "snapshot": {"soc_min": 15.0, "soc_max": 100.0, "export_limit_w": 4000.0, "export_limit_enabled": 1.0},
         "owner": {"profile": "goodwe-et", "domain": "goodwe", "mode_entity": "select.goodwe_ems_mode"},
         "restore_keys": None, "taken_over": []}


def make_entry(hass, *, options=None, entry_id="paired01") -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="Volcast — Home", data=dict(PAIRED_DATA),
                            options=dict(options or {}), entry_id=entry_id, unique_id="account_test")
    entry.add_to_hass(hass)
    return entry


def control_of(hass, entry):
    return (hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}).get("control")


def store_state(hass_storage, entry_id: str, data: dict) -> None:
    hass_storage[f"volcast.control.{entry_id}"] = {"version": 1, "minor_version": 1,
                                                   "key": f"volcast.control.{entry_id}", "data": data}


async def setup_entry(hass, entry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


# ── połączenie bezpośrednie: symulator falownika na pętli zwrotnej ──────────
# Gniazda tylko do 127.0.0.1 (lista dozwolonych hostów harnessu HA); pętla zwrotna włączana
# wyłącznie stałą testową `direct_search.ALLOW_LOOPBACK` na czas testu.

SALT = bytes(range(16))


@pytest.fixture
async def goodwe_sim(socket_enabled, monkeypatch):
    from custom_components.volcast.control import direct_search as ds
    from tests.sim.device import Faults, RegisterBank
    from tests.sim.fixtures import GOODWE_UNREADABLE, goodwe_words
    from tests.sim.servers import goodwe_udp_server

    monkeypatch.setattr(ds, "ALLOW_LOOPBACK", True)
    bank = RegisterBank(goodwe_words(), unreadable=GOODWE_UNREADABLE)
    faults = Faults()
    server = await goodwe_udp_server(bank, faults)
    server.bank, server.faults = bank, faults
    yield server
    await server.close()


def seed_salt(hass_storage) -> None:
    hass_storage["volcast.installation"] = {"version": 1, "minor_version": 1, "key": "volcast.installation",
                                            "data": {"salt": SALT.hex()}}


def make_poll_due(conn) -> None:
    """Następny tik zegara HA odpyta falownik: zegar monotoniczny połączenia nie przesuwa się razem
    z `async_fire_time_changed`, więc ostatni odczyt oznaczamy jako dawno temu (tylko testy)."""
    last = conn._last_poll
    if last is not None:
        conn._last_poll = last - 2 * max(conn.poll_s, 60.0)
