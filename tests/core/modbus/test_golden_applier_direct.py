"""Parytet z pisarzem referencyjnym: wektory `applier.json` przez ścieżkę rejestrów i symulator."""
import pytest

from custom_components.volcast.core.modbus.client import RegisterClient
from custom_components.volcast.core.modbus.writer import RegisterWriter
from custom_components.volcast.core.profile import load_builtin
from custom_components.volcast.core.registers import encode_writes
from custom_components.volcast.core.write_sequence import async_run_writes
from tests.core.golden import load_golden, params_from_golden

from .conftest import sim_transport

GW = load_builtin("goodwe-et")
VECTORS = load_golden("applier")["vectors"]


def _collapse(seq):
    """Kolejne powtórzenia tej samej ramki (ponowna wysyłka UDP) liczone raz."""
    out = []
    for item in seq:
        if not out or out[-1] != item:
            out.append(item)
    return out


def _differ(bank, addr: int, value: int) -> None:
    # Stan przed zapisem różny od zapisywanej wartości — inaczej odczyt zwrotny „potwierdziłby” zapis.
    if bank.read(addr, 1) == [value]:
        bank.poke(addr, (value + 1) & 0xFFFF)


@pytest.mark.asyncio
@pytest.mark.parametrize("vec", VECTORS, ids=[str(i) for i in range(len(VECTORS))])
async def test_golden_applier_vectors_over_simulator(vec, goodwe_udp_sim, goodwe_bank, sim_faults):
    goodwe_bank.unreadable.clear()                 # parytet: każdy rejestr ma odczyt zwrotny
    writes = encode_writes(params_from_golden(vec["params"], GW), GW)
    if vec["fail_kind"] == "unsupported":
        goodwe_bank.unsupported.add(vec["fail_reg"])
    elif vec["fail_kind"] == "error":
        sim_faults.mute_write_addrs.add(vec["fail_reg"])
        goodwe_bank.ignore_writes.add(vec["fail_reg"])
        for w in writes:
            if w.addr == vec["fail_reg"]:
                _differ(goodwe_bank, w.addr, w.value)
    elif vec["fail_kind"] == "denied":
        for w in writes:
            goodwe_bank.ignore_writes.add(w.addr)
            _differ(goodwe_bank, w.addr, w.value)
    client = RegisterClient(sim_transport(goodwe_udp_sim, "goodwe_udp", 0xF7, timeout_s=0.1), GW)
    try:
        rep = await async_run_writes(writes, RegisterWriter(client, GW).async_write)
    finally:
        await client.transport.close()
    frames = _collapse([[a, v] for fc, a, v in goodwe_udp_sim.log if fc == 0x06])
    assert frames == vec["writes"]
    assert rep.written == vec["written"]
    assert rep.unsupported == vec["unsupported"]
    assert rep.mode_held == vec["mode_held"]
