"""Pisarz: zapis 32-bitowy (FC 16, kolejność słów z profilu), skala i adres urządzenia per rejestr."""
import pytest

from custom_components.volcast.core.modbus import writer as writer_mod
from custom_components.volcast.core.modbus.client import RegisterClient
from custom_components.volcast.core.modbus.writer import RegisterWriter
from custom_components.volcast.core.params import Params
from custom_components.volcast.core.registers import RegisterWrite, encode_writes
from custom_components.volcast.core.write_sequence import ERROR, OK, OK_ADJUSTED, AdjustedOutcome

from .engine_profile import PLANT, directional_profile
from .unit_link import LINK, PlantLink as _Plant, StrictLink as _StrictLink


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _now(_s):
        return None
    monkeypatch.setattr(writer_mod, "_sleep", _now)


def _writer(transport, profile=None):
    profile = profile or directional_profile()
    return RegisterWriter(RegisterClient(transport, profile), profile), profile


def _one(params, profile):
    w, = encode_writes(params, profile)
    return w


@pytest.mark.asyncio
async def test_u32_set_point_two_words_high_first_fc16_on_its_unit():
    """2,5 kW na 40032 jako dwa słowa (starsze pierwsze), adres zakładu 247 — połączenie ma inny adres."""
    t = _Plant({PLANT: {40032: 0, 40033: 0}})
    writer, prof = _writer(t)
    assert await writer.async_write(_one(Params(charge_power_w=2500), prof)) == OK
    assert [t.banks[PLANT][40032], t.banks[PLANT][40033]] == [0, 2500]
    assert ("write", 40032, [0, 2500], 16, PLANT) in t.calls          # FC 16 mimo write_function 6
    reads = [c for c in t.calls if c[0] == "read"]
    assert reads and all(c[1:] == (40032, 2, 3, PLANT) for c in reads)  # przed i zwrotny: oba słowa, ten adres
    assert t.banks[LINK] == {}                                         # nic na adresie połączenia


@pytest.mark.asyncio
async def test_u32_low_word_first_and_scale():
    t = _Plant({PLANT: {40034: 0, 40035: 0}})
    writer, prof = _writer(t)
    assert await writer.async_write(_one(Params(discharge_power_w=70000), prof)) == OK
    assert ("write", 40034, [7000, 0], 16, PLANT) in t.calls             # surowa = 70000 W / 10


@pytest.mark.asyncio
async def test_readback_with_one_word_wrong_is_a_mismatch():
    """Starsze słowo nie przyjęte (zostało 1): odczyt zwrotny obu słów ≠ zamówiona, w stronę groźną → ERROR."""
    t = _Plant({PLANT: {40032: 1, 40033: 0}}, apply=lambda unit, addr, w: 1 if addr == 40032 else w)
    writer, prof = _writer(t)
    assert await writer.async_write(_one(Params(charge_power_w=2500), prof)) == ERROR


@pytest.mark.asyncio
async def test_readback_lower_value_is_adjusted_in_value_units():
    """Urządzenie przycina młodsze słowo: wynik OK_ADJUSTED z wartością rzeczywistą w W (surowa × skala)."""
    t = _Plant({PLANT: {40034: 0, 40035: 0}}, apply=lambda unit, addr, w: 650 if addr == 40034 else w)
    writer, prof = _writer(t)
    out = await writer.async_write(_one(Params(discharge_power_w=70000), prof))
    assert out == OK_ADJUSTED and isinstance(out, AdjustedOutcome) and out.actual == 6500.0


@pytest.mark.parametrize("bad", [
    RegisterWrite("charge_power_w", 40032, 2500),                                   # jedno słowo zamiast dwóch
    RegisterWrite("charge_power_w", 40032, 2500, count=2),                          # adres połączenia
    RegisterWrite("charge_power_w", 40032, 2500, count=2, word_order="lo_hi", unit_id=PLANT),
    RegisterWrite("charge_power_w", 40032, 1 << 32, count=2, unit_id=PLANT),        # poza zakresem 2 słów
    RegisterWrite("mode", 40031, 1),                                                # tryb zakładu bez adresu
    RegisterWrite("export_limit_w", 47510, 100, unit_id=PLANT),                     # klucz połączenia z adresem
])
@pytest.mark.asyncio
async def test_write_not_matching_the_profile_layout_sends_nothing(bad):
    t = _Plant({PLANT: {40031: 1, 40032: 0, 40033: 0}})
    writer, _ = _writer(t)
    assert await writer.async_write(bad) == ERROR
    assert t.calls == []


@pytest.mark.asyncio
async def test_u16_on_own_unit_uses_profile_function():
    t = _Plant({PLANT: {40031: 2, 40029: 0}})
    writer, prof = _writer(t)
    enable, mode = encode_writes(Params(enable=True, mode="charge_battery"), prof)
    assert await writer.async_write(enable) == OK and await writer.async_write(mode) == OK
    assert [c for c in t.calls if c[0] == "write"] == [("write", 40029, [1], 6, PLANT),
                                                      ("write", 40031, [11], 6, PLANT)]
    assert all(c[4] == PLANT for c in t.calls)


@pytest.mark.asyncio
async def test_key_without_new_fields_calls_transport_exactly_as_before():
    """Regresja: klucz bez u32/skali/unit_id — transport bez parametru `unit` (atrapa go nie zna)."""
    t = _StrictLink({LINK: {47510: 0}})
    writer, prof = _writer(t)
    assert await writer.async_write(_one(Params(export_limit_w=4000), prof)) == OK
    assert ("write", 47510, [4000], 6, None) in t.calls


@pytest.mark.asyncio
async def test_restore_of_u32_no_limit_value_exact():
    """Wartość 0xFFFFFFFF („bez limitu”) przywracana dokładnie, oba słowa."""
    t = _Plant({PLANT: {40032: 0, 40033: 3000}})
    writer, prof = _writer(t)
    w = _one(Params(charge_power_w=0xFFFFFFFF), prof)
    assert await writer.async_write_restore(w) == OK
    assert ("write", 40032, [0xFFFF, 0xFFFF], 16, PLANT) in t.calls
    # już równe (świeży odczyt obu słów) — bez ramki
    t.calls.clear()
    assert await writer.async_write_restore(w) == OK
    assert [c[0] for c in t.calls] == ["read"]


@pytest.mark.asyncio
async def test_uncorrelated_link_reads_views_on_the_key_unit():
    """Łącze bez korelacji odpowiedzi: widoki rejestru (bloki weryfikacyjne i sam rejestr) czytane z adresu
    urządzenia klucza, oba słowa w widoku; blok rozdzielający — z adresu połączenia."""
    def mutate(raw):
        raw["modbus"]["verify_blocks"] = [{"addr": 40029, "count": 7}]
    prof = directional_profile(mutate)
    t = _Plant({PLANT: {40032: 0, 40033: 0}}, kind="goodwe_udp")
    writer, _ = _writer(t, prof)
    assert await writer.async_write(_one(Params(charge_power_w=2500), prof)) == OK
    assert [t.banks[PLANT][40032], t.banks[PLANT][40033]] == [0, 2500]
    plant_reads = {c[1:3] for c in t.calls if c[0] == "read" and c[4] == PLANT}
    assert plant_reads <= {(40029, 7), (40032, 2)} and plant_reads
    assert all(c[4] is None for c in t.calls if c[0] == "read" and c[1:3] not in plant_reads)
    assert ("write", 40032, [0, 2500], 16, PLANT) in t.calls
