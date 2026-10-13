"""Narzędzie do odczytu rejestrów na żywo: tylko FC 3, ramki w formacie `frames.json`. Test na symulatorze."""
from __future__ import annotations

import json

import pytest

from tools.live import read_registers as rr

ADDR, COUNT = 35100, 4


def _argv(sim, addr=ADDR, count=COUNT, *extra):
    return ["--host", sim.host, "--port", str(sim.port), "--addr", str(addr), "--count", str(count), *extra]


def _words(entry):
    raw = bytes.fromhex(entry["response"])[2:]                 # bez AA55
    n = raw[2]
    return [int.from_bytes(raw[3 + i:5 + i], "big") for i in range(0, n, 2)]


@pytest.mark.asyncio
async def test_read_returns_bank_words_and_frame_hex(goodwe_udp_sim, goodwe_bank, capsys):
    rc = await rr.amain(_argv(goodwe_udp_sim, ADDR, COUNT, "--timeout", "1"))
    assert rc == 0
    out = capsys.readouterr().out
    expected = list(goodwe_bank.read(ADDR, COUNT))
    entry = json.loads(out[out.index("{"):])
    assert set(entry) == {"offset", "count", "request", "response", "valid"}
    assert entry["offset"] == ADDR and entry["count"] == COUNT and entry["valid"] is True
    assert entry["request"].startswith("f703")                 # unit 247, FC 3
    assert entry["response"].startswith("aa55f703")            # AA55 + RTU, jak w frames.json
    assert _words(entry) == expected
    assert f"{expected[0]} (0x{expected[0]:04x})" in out       # słowa dziesiętnie i hex


@pytest.mark.asyncio
async def test_timeout_is_clear_nonzero_exit(goodwe_udp_sim, sim_faults, capsys):
    sim_faults.drop_next = 100
    rc = await rr.amain(_argv(goodwe_udp_sim, ADDR, COUNT, "--timeout", "0.5", "--tries", "1"))
    assert rc != 0
    assert "no response" in capsys.readouterr().err.lower()


@pytest.mark.asyncio
async def test_bad_arguments_exit_nonzero():
    assert await rr.amain(["--host", "127.0.0.1", "--addr", "70000", "--count", "1"]) != 0


@pytest.mark.parametrize("fc", [6, 16, 4, 5, 15, 0])
def test_guard_refuses_everything_but_fc3(fc):
    with pytest.raises(rr.WriteRefused):
        rr.ensure_read_only(fc)


def test_guard_allows_fc3():
    rr.ensure_read_only(3)


def test_module_has_no_write_path():
    src = open(rr.__file__, encoding="utf-8").read()
    assert ".write(" not in src and "write_req" not in src and "pdu_write" not in src


@pytest.mark.parametrize("flag,value", [("--tries", "0"), ("--tries", "20"), ("--timeout", "0.1"),
                                        ("--timeout", "60")])
def test_out_of_range_limits_rejected(flag, value):
    with pytest.raises(SystemExit) as exc:
        rr._parser().parse_args(["--host", "h", "--addr", "1", "--count", "1", flag, value])
    assert exc.value.code == 2


@pytest.mark.asyncio
async def test_consecutive_attempts_are_spaced_by_gap(goodwe_udp_sim, sim_faults):
    sim_faults.drop_next = 1                                   # pierwsza próba bez odpowiedzi, druga z odpowiedzią
    waits = []

    async def fake_sleep(s):
        waits.append(s)

    words, entry = await rr.read_registers(goodwe_udp_sim.host, ADDR, COUNT, port=goodwe_udp_sim.port,
                                           timeout_s=0.5, tries=3, clock=lambda: 0.0, sleep=fake_sleep)
    assert entry["valid"] is True
    assert waits and all(w >= rr.GAP_S for w in waits) and rr.GAP_S >= 0.3
