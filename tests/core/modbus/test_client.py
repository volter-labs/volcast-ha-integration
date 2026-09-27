"""Klient rejestrów: bloki odczytu, podział bloku po wyjątku 2, tożsamość urządzenia."""
import json

import pytest

from custom_components.volcast.core.modbus.blocks import read_plan, split_block
from custom_components.volcast.core.modbus.client import RegisterClient
from custom_components.volcast.core.modbus.identity import device_fingerprint, identity_info
from custom_components.volcast.core.registers import RegisterImage
from custom_components.volcast.core.transports.base import LinkDown, RequestTimeout, TransportError

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

    async def read(self, addr, count, *, tries=None):
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
async def test_deye_identity_includes_serial(deye_client, deye_profile, deye_bank):
    fp = await deye_client.read_identity()
    assert fp is not None and len(fp) == 16
    deye_bank.poke(3, 0x4142)                          # inny serial (blok 3–7) → inny odcisk
    assert await deye_client.read_identity() != fp


def _goodwe_identity_image(over=None):
    from tests.sim.fixtures import goodwe_words
    words = {a: w for a, w in goodwe_words().items() if 35000 <= a < 35033}
    words.update(over or {})
    return RegisterImage(words)


def test_identity_info_has_no_serial(goodwe_profile):
    info = identity_info(goodwe_profile, _goodwe_identity_image())
    assert info == {"matched": True, "model": "GW8KN-ET", "rated_power_w": 8000.0}
    assert "SERIAL" not in json.dumps(info)


def test_degenerate_identity_is_unknown(goodwe_profile, deye_profile):
    zeros = RegisterImage({a: 0 for a in range(35000, 35033)})
    ones = RegisterImage({a: 0xFFFF for a in range(35000, 35033)})
    assert device_fingerprint(SALT, goodwe_profile, zeros) is None
    assert device_fingerprint(SALT, goodwe_profile, ones) is None
    deye_zero = RegisterImage({0: 0, 3: 0, 4: 0, 5: 0, 6: 0, 7: 0, 20: 0, 21: 0})
    assert device_fingerprint(SALT, deye_profile, deye_zero) is None


def test_invalid_serial_is_unknown_not_a_different_device(goodwe_profile):
    # Profil deklaruje serial, odczyt dał śmieci (start falownika) → tożsamość nieznana, nie
    # odcisk z modelu (ten różniłby się od zapisanego i wyglądał jak „inne urządzenie”).
    for junk in (0xFFFF, 0x0000, 0x0102):
        img = _goodwe_identity_image({a: junk for a in range(35003, 35011)})
        assert device_fingerprint(SALT, goodwe_profile, img) is None


def test_profile_without_serial_uses_model_and_power(deye_profile):
    import copy
    from custom_components.volcast.core.profile import profile_from_dict
    raw = copy.deepcopy(dict(deye_profile.raw))
    raw = __import__("json").loads(__import__("json").dumps(raw, default=dict))
    del raw["identify"]["registers"]["serial"]
    raw["modbus"]["identify_reads"] = [r for r in raw["modbus"]["identify_reads"] if r["addr"] != 3]
    no_serial = profile_from_dict(raw)
    img = RegisterImage({0: 1280, 20: 34464, 21: 1})
    fp = device_fingerprint(SALT, no_serial, img)
    assert fp is not None and fp == device_fingerprint(SALT, no_serial, RegisterImage({0: 1280, 20: 34464, 21: 1}))


def test_wrong_device_is_unknown(goodwe_profile, deye_profile):
    other_model = _goodwe_identity_image({35011: 0x5858})         # „XX…” — nie pasuje do model_regex
    assert device_fingerprint(SALT, goodwe_profile, other_model) is None
    words = {0: 9999, 3: 0x4142, 4: 0x4344, 5: 0x4546, 6: 0x4748, 7: 0x4950, 20: 34464, 21: 1}
    assert device_fingerprint(SALT, deye_profile, RegisterImage(words)) is None      # device_type spoza listy


def test_fingerprint_salted_and_serial_wins(goodwe_profile):
    img = _goodwe_identity_image()
    a = device_fingerprint(SALT, goodwe_profile, img)
    assert a != device_fingerprint(b"\x02" * 16, goodwe_profile, img)
    assert a == device_fingerprint(SALT, goodwe_profile, _goodwe_identity_image({35001: 10000}))
    for bad in (b"", b"\x01" * 15, "salt" * 8):
        with pytest.raises(ValueError):
            device_fingerprint(bad, goodwe_profile, img)


def test_rated_power_out_of_range_is_none(deye_profile):
    info = identity_info(deye_profile, RegisterImage({0: 1280, 20: 0, 21: 0}))
    assert info["rated_power_w"] is None and info["matched"] is True


# ── limity odczytu ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unreadable_keys_not_polled(goodwe_udp_sim, goodwe_profile):
    client = RegisterClient(sim_transport(goodwe_udp_sim, "goodwe_udp", 0xF7), goodwe_profile,
                            unreadable={"soc_max"})
    try:
        r = await client.read_state()
        t = client.transport
        assert t.stats.stray == 0 and t.stats.timeouts == 0 and t.stats.consecutive_timeouts == 0
        assert all(a != 47760 for fc, a, _ in goodwe_udp_sim.log)
        assert "soc_max" not in r.device and r.device["power_w"] == 8846.0
    finally:
        await client.transport.close()


@pytest.mark.asyncio
async def test_silent_device_gives_up_after_one_block(goodwe_udp_sim, goodwe_profile, sim_faults):
    sim_faults.drop_next = 1000
    client = RegisterClient(sim_transport(goodwe_udp_sim, "goodwe_udp", 0xF7, timeout_s=0.05, read_tries=3),
                            goodwe_profile)
    try:
        with pytest.raises(RequestTimeout):
            await client.read_state()
        assert goodwe_udp_sim.requests == 3                   # jeden blok, wszystkie jego próby
        assert [f["ok"] for f in client.last_frames] == [False] * len(read_plan(goodwe_profile))
    finally:
        await client.transport.close()


class _SlowFake:
    """Transport, który po każdym odczycie „zużywa” czas na zegarze atrapie."""

    def __init__(self, clock, fail_addrs=()):
        from custom_components.volcast.core.transports.base import TransportConfig
        self.cfg = TransportConfig(kind="modbus_tcp", host="127.0.0.1", port=502, unit=1,
                                   timeout_s=2.0, read_tries=3)
        self.clock = clock
        self.fail = set(fail_addrs)
        self.tries_seen = []

    async def read(self, addr, count, *, tries=None):
        self.tries_seen.append(tries)
        if addr in self.fail:
            self.clock.now += 2.0 * tries
            raise RequestTimeout(silent=False)
        return [0] * count


@pytest.mark.asyncio
async def test_cycle_time_budget_caps_tries_and_stops(goodwe_profile):
    from tests.core.transports.helpers import FakeClock
    clock = FakeClock()
    plan = read_plan(goodwe_profile)
    fake = _SlowFake(clock, fail_addrs={plan[0][0], plan[1][0]})
    client = RegisterClient(fake, goodwe_profile, clock=clock)
    with pytest.raises(TransportError):
        await client.read_state()
    # Limit = max(5 s, 3 próby × 2 s) = 6 s straty: pierwszy blok ma pełne 3 próby (6 s straty),
    # kolejne bloki nie idą już wcale — cykl nie traci na przekroczeniach więcej niż limit.
    assert fake.tries_seen == [3]
    assert [f["ok"] for f in client.last_frames] == [False] * len(plan)


@pytest.mark.asyncio
async def test_slow_healthy_link_reads_everything_and_identifies(rtu_tcp_sim, deye_profile, sim_faults):
    # Każda odpowiedź spóźniona o 0,15 s przy limicie cyklu 0,5 s: odpowiedzi nie zjadają limitu.
    sim_faults.delay_s = 0.15
    client = RegisterClient(sim_transport(rtu_tcp_sim, "modbus_rtu", 1, timeout_s=0.5, read_tries=3),
                            deye_profile, salt=SALT, cycle_budget_s=0.5)
    try:
        r = await client.read_state()
        assert all(f["ok"] for f in client.last_frames) and r.values["soc"] is not None
        assert await client.read_identity() is not None
    finally:
        await client.transport.close()


@pytest.mark.asyncio
async def test_long_profile_timeout_still_reads(rtu_tcp_sim, deye_profile):
    client = RegisterClient(sim_transport(rtu_tcp_sim, "modbus_rtu", 1, timeout_s=6.0), deye_profile, salt=SALT)
    try:
        await client.read_state()
        assert all(f["ok"] for f in client.last_frames)
        assert await client.read_identity() is not None
    finally:
        await client.transport.close()


@pytest.mark.asyncio
async def test_reading_stamped_at_cycle_start(goodwe_profile):
    from tests.core.transports.helpers import FakeClock
    clock = FakeClock()
    fake = _SlowFake(clock, fail_addrs={read_plan(goodwe_profile)[0][0]})
    client = RegisterClient(fake, goodwe_profile, clock=clock, cycle_budget_s=100.0)
    start = clock.now
    r = await client.read_state()
    assert r.at_mono == start and clock.now > start


@pytest.mark.asyncio
async def test_last_frames_json_serialisable_without_bytes(goodwe_client):
    await goodwe_client.read_state()
    text = json.dumps(goodwe_client.last_frames)
    assert "request" not in text and "response" not in text


class _LossyFake(_SlowFake):
    """Każdy blok przechodzi dopiero w OSTATNIEJ próbie — wcześniejsze kończą się przekroczeniem czasu."""

    def __init__(self, clock):
        super().__init__(clock)
        from custom_components.volcast.core.transports.base import TransportStats
        self.stats = TransportStats()

    async def read(self, addr, count, *, tries=None):
        self.tries_seen.append(tries)
        lost = (tries or 1) - 1
        self.clock.now += 2.0 * lost
        self.stats.timeouts += lost
        return [0] * count


@pytest.mark.asyncio
async def test_time_lost_before_a_successful_retry_counts_toward_budget(goodwe_profile):
    from tests.core.transports.helpers import FakeClock
    clock = FakeClock()
    fake = _LossyFake(clock)
    client = RegisterClient(fake, goodwe_profile, clock=clock)
    await client.read_state()
    # Limit 6 s: pierwszy blok traci 4 s na dwóch próbach, więc kolejne mają już tylko jedną.
    assert fake.tries_seen[0] == 3 and set(fake.tries_seen[1:]) == {1}
