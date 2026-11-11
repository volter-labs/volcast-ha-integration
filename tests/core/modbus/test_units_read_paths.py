"""Odczyty z adresu urządzenia rejestru (cykl odpytywania, próba możliwości) i u32 ze skalą w odczycie."""
import pytest

from custom_components.volcast.core.discovery.identify import Identity
from custom_components.volcast.core.discovery.probe import probe
from custom_components.volcast.core.modbus.client import RegisterClient

from .engine_profile import PLANT, directional_profile
from .unit_link import LINK, PlantLink


def _identity(profile) -> Identity:
    return Identity(profile.id, "modbus_tcp", 502, 1, "X", None, device_fp="fp")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["modbus_tcp", "goodwe_udp"])
async def test_read_cycle_reads_plant_registers_on_their_unit(kind):
    prof = directional_profile()
    t = PlantLink({PLANT: {40029: 1, 40031: 11, 40032: 0, 40033: 2500, 40034: 300, 40035: 0, 40046: 155},
                   LINK: {37007: 64, 40031: 1, 40032: 9}}, kind=kind)
    reading = await RegisterClient(t, prof).read_state()
    assert reading.device["mode"] == "charge_battery"
    assert reading.device["charge_power_w"] == 2500.0
    assert reading.device["discharge_power_w"] == 3000.0
    assert reading.device["soc_min"] == pytest.approx(15.5)
    assert reading.device["enable"] == 1.0
    assert reading.values["soc"] == 64
    plant = [c for c in t.calls if c[4] == PLANT]
    assert plant and all(40029 <= c[1] and c[1] + c[2] <= 40047 for c in plant)
    assert not [c for c in t.calls if c[4] is None and c[1] <= 40031 < c[1] + c[2]]


@pytest.mark.asyncio
async def test_probe_reads_key_registers_on_their_unit_and_needs_both_set_points():
    prof = directional_profile()
    t = PlantLink({PLANT: {40031: 2, 40032: 0, 40033: 0, 40034: 0, 40035: 0}})
    rep = await probe(RegisterClient(t, prof), prof, _identity(prof), budget=20)
    assert rep.capabilities == {"mode": True, "charge_power_w": True, "discharge_power_w": True}
    assert rep.direct_available is True
    reads = [c[1:] for c in t.calls]
    assert reads == [(40031, 1, 3, PLANT), (40032, 2, 3, PLANT), (40034, 2, 3, PLANT)]


@pytest.mark.asyncio
async def test_probe_on_uncorrelated_link_reads_single_register_views_on_its_unit():
    """Łącze bez korelacji: pojedynczy rejestr klucza (tryb) i jego blok weryfikacyjny z adresu zakładu;
    blok rozdzielający (identyfikacja) z adresu połączenia."""
    def verify(raw):
        raw["modbus"]["verify_blocks"] = [{"addr": 40029, "count": 7}]
    prof = directional_profile(verify)
    t = PlantLink({PLANT: {40031: 2}}, kind="goodwe_udp")
    rep = await probe(RegisterClient(t, prof), prof, _identity(prof), budget=20)
    assert rep.direct_available is True
    for _, addr, count, _, unit in t.calls:
        assert unit == (PLANT if 40029 <= addr <= 40035 else None), (addr, count, unit)


@pytest.mark.asyncio
async def test_probe_without_direction_set_points_is_not_available():
    """Profil z mocą wg kierunku nie ma `power_w`: grupa sterowania to tryb i OBIE nastawy kierunku."""
    def only_mode(raw):
        raw["modbus"]["probe_keys"] = ["mode"]
    prof = directional_profile(only_mode)
    t = PlantLink({PLANT: {40031: 2}})
    rep = await probe(RegisterClient(t, prof), prof, _identity(prof), budget=20)
    assert rep.capabilities == {"mode": True} and rep.direct_available is False
