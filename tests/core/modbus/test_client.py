"""Klient rejestrów: bloki odczytu, podział bloku po wyjątku 2, tożsamość urządzenia."""
import json

import pytest

from custom_components.volcast.core.modbus.blocks import read_plan, split_block
from custom_components.volcast.core.modbus.client import RegisterClient
from custom_components.volcast.core.modbus.identity import device_fingerprint, identity_fields
from custom_components.volcast.core.registers import RegisterImage
from custom_components.volcast.core.transports.base import LinkDown, RequestTimeout

from .conftest import SALT, sim_transport


@pytest.mark.asyncio
async def test_read_state_decodes_goodwe(goodwe_client):
    r = await goodwe_client.read_state()
    assert r.values["soc"] == 83
    assert r.device["mode"] == "charge_battery" and r.device["soc_min"] == 5.0
    assert "serial" not in r.values
    assert r.at_mono > 0 and r.at_utc.tzinfo is not None


@pytest.mark.asyncio
async def test_read_state_never_reads_identify_or_serial(goodwe_client, goodwe_udp_sim):
    await goodwe_client.read_state()
    reads = [(a, n) for fc, a, n in goodwe_udp_sim.log if fc == 0x03]
    assert all(a + n <= 35000 or a > 35032 for a, n in reads)


@pytest.mark.asyncio
async def test_block_exception_2_split_per_key(goodwe_client, goodwe_profile, goodwe_bank):
    block = next(b for b in read_plan(goodwe_profile) if b[0] == 35105)
    covered = {a for s, n in split_block(block, goodwe_profile) for a in range(s, s + n)}
    hole = next(a for a in range(block[0], block[0] + block[1]) if a not in covered)
    goodwe_bank.unsupported.add(hole)
    r = await goodwe_client.read_state()
    assert r.values["pv_power_w"] == 828
    frames = goodwe_client.last_frames
    assert {"addr": block[0], "count": block[1], "ok": False} in frames
    assert all(f["ok"] for f in frames if f["addr"] in {s for s, _ in split_block(block, goodwe_profile)}
               and f["count"] != block[1])


@pytest.mark.asyncio
async def test_client_block_failure_marks_only_its_keys_none(goodwe_client):
    # 47760 odpowiada ramką złej długości (nagranie) — tylko jego klucz bez odczytu.
    r = await goodwe_client.read_state()
    assert "soc_max" not in r.device
    assert r.device["power_w"] == 8846.0
    assert {"addr": 47760, "count": 1, "ok": False} in goodwe_client.last_frames
    assert all(set(f) == {"addr", "count", "ok"} for f in goodwe_client.last_frames)


@pytest.mark.asyncio
async def test_read_state_raises_when_nothing_read(goodwe_udp_sim, goodwe_profile, sim_faults):
    sim_faults.drop_next = 1000
    client = RegisterClient(sim_transport(goodwe_udp_sim, "goodwe_udp", 0xF7, timeout_s=0.05, read_tries=1),
                            goodwe_profile)
    try:
        with pytest.raises(RequestTimeout):
            await client.read_state()
        assert client.last_frames and not any(f["ok"] for f in client.last_frames)
    finally:
        await client.transport.close()


class _Fake:
    kind = "modbus_tcp"

    def __init__(self, fail_after: int):
        self.calls = 0
        self.fail_after = fail_after

    async def read(self, addr, count):
        self.calls += 1
        if self.calls > self.fail_after:
            raise LinkDown("down")
        return [0] * count


@pytest.mark.asyncio
async def test_link_down_aborts_remaining_blocks(goodwe_profile):
    fake = _Fake(fail_after=2)
    client = RegisterClient(fake, goodwe_profile)
    r = await client.read_state()
    assert fake.calls == 3                            # po LinkDown kolejne bloki nie są już próbowane
    assert [f["ok"] for f in client.last_frames][:3] == [True, True, False]
    assert len(client.last_frames) == len(read_plan(goodwe_profile))
    assert r.values["soc"] is None


@pytest.mark.asyncio
async def test_read_register_and_block(deye_client):
    assert await deye_client.read_register(146) == 255
    assert await deye_client.read_block(148, 3) == [0, 500, 1000]


@pytest.mark.asyncio
async def test_deye_state_has_programs(deye_client):
    r = await deye_client.read_state()
    assert r.tou_enabled is True and len(r.programs) == 6
    assert r.device["tou.1.power_w"] == 3000.0


# ── tożsamość (odcisk urządzenia) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_identity_salted_and_stable(goodwe_client, goodwe_udp_sim, goodwe_profile):
    fp = await goodwe_client.read_identity()
    assert isinstance(fp, str) and len(fp) == 16
    assert await goodwe_client.read_identity() == fp
    other = RegisterClient(sim_transport(goodwe_udp_sim, "goodwe_udp", 0xF7), goodwe_profile, salt=b"\x01" * 16)
    try:
        assert await other.read_identity() != fp
    finally:
        await other.transport.close()


@pytest.mark.asyncio
async def test_identity_changes_with_serial(goodwe_client, goodwe_bank):
    fp = await goodwe_client.read_identity()
    goodwe_bank.poke(35005, goodwe_bank.read(35005, 1)[0] ^ 0x0101)
    assert await goodwe_client.read_identity() != fp


@pytest.mark.asyncio
async def test_read_identity_none_when_unreadable(goodwe_client, sim_faults):
    sim_faults.drop_next = 1000
    assert await goodwe_client.read_identity() is None


@pytest.mark.asyncio
async def test_read_identity_requires_salt(goodwe_udp_sim, goodwe_profile):
    client = RegisterClient(sim_transport(goodwe_udp_sim, "goodwe_udp", 0xF7), goodwe_profile)
    try:
        with pytest.raises(ValueError):
            await client.read_identity()
    finally:
        await client.transport.close()


@pytest.mark.asyncio
async def test_deye_identity_from_device_type_and_power(deye_client, deye_profile, deye_bank):
    fp = await deye_client.read_identity()
    image = RegisterImage({0: 1280, 20: 34464, 21: 1})
    fields = identity_fields(deye_profile, image)
    assert fields == {"serial": None, "model": "1280", "rated_power_w": 10000.0}
    assert fp == device_fingerprint(SALT, deye_profile.id, fields)
    deye_bank.poke(3, 0x4141)                          # blok 3–7 (serial) nie wpływa na odcisk
    assert await deye_client.read_identity() == fp


def test_fingerprint_is_not_a_plain_hash_of_serial(goodwe_profile):
    fields = {"serial": "SERIAL01", "model": "GW10K-ET", "rated_power_w": 10000.0}
    a = device_fingerprint(SALT, goodwe_profile.id, fields)
    assert a == device_fingerprint(SALT, goodwe_profile.id, dict(fields, model="other"))   # serial wygrywa
    assert a != device_fingerprint(b"\x02" * 16, goodwe_profile.id, fields)
    assert "SERIAL01" not in a and len(a) == 16
    with pytest.raises(ValueError):
        device_fingerprint(b"", goodwe_profile.id, fields)


def test_rated_power_out_of_range_is_none(deye_profile):
    fields = identity_fields(deye_profile, RegisterImage({0: 1280, 20: 0, 21: 0}))
    assert fields["rated_power_w"] is None


@pytest.mark.asyncio
async def test_last_frames_json_serialisable_without_bytes(goodwe_client):
    await goodwe_client.read_state()
    text = json.dumps(goodwe_client.last_frames)
    assert "request" not in text and "response" not in text
