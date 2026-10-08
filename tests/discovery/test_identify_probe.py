"""Wykrywanie warstw 3–4: identyfikacja falownika i próba możliwości — wyłącznie odczyt (FC 3),
wyłącznie symulatory na pętli zwrotnej."""
import json
from dataclasses import replace

import pytest

from custom_components.volcast.core.discovery.identify import Candidate, candidates_from, identify
from custom_components.volcast.core.discovery.network import LoggerReply
from custom_components.volcast.core.discovery.probe import ProbeReport, discover, probe
from custom_components.volcast.core.profile import load_builtin
from custom_components.volcast.core.transports.base import LinkDown, TransportStats
from custom_components.volcast.core.transports.factory import make_transport
from tests.sim.fixtures import V5_LOGGER_SERIAL, goodwe_words

SALT = bytes(range(16))
OTHER_SALT = bytes(range(1, 17))
LOCAL = "127.0.0.1"


@pytest.fixture
def profiles():
    return [load_builtin("goodwe-et"), load_builtin("deye-sg")]


class _Dead:
    """Transport bez urządzenia po drugiej stronie."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.kind = cfg.kind
        self.stats = TransportStats()
        self.closed = False

    async def read(self, addr, count, *, tries=None):
        raise LinkDown("refused")

    async def close(self):
        self.closed = True


class Factory:
    """Fabryka transportów: rodzaj → symulator (port podmieniony, krótki czas oczekiwania)."""

    def __init__(self, sims, *, timeout_s=0.1, wrap=None):
        self.sims = sims
        self.timeout_s = timeout_s
        self.wrap = wrap
        self.created = []

    def __call__(self, cfg):
        sim = self.sims.get(cfg.kind)
        if sim is None:
            t = _Dead(cfg)
        else:
            t = make_transport(replace(cfg, port=sim.port, timeout_s=self.timeout_s, gap_s=0.0),
                               allow_loopback=True)
            if self.wrap is not None:
                t = self.wrap(t)
        self.created.append(t)
        return t

    def closed(self):
        return [t.closed if hasattr(t, "closed") else t._closed for t in self.created]


def _ascii(words, addr, n):
    return b"".join(words[a].to_bytes(2, "big") for a in range(addr, addr + n)).decode("ascii").strip()


async def _close(client):
    if client is not None:
        await client.transport.close()


# ── kandydaci ─────────────────────────────────────────────────────────────


def _reply(ip, serial=None):
    r = LoggerReply(raw="", ip=ip, mac=None, name=None)
    r.logger_serial = serial
    return r


def test_candidates_reject_public_and_hostnames():
    out = candidates_from([_reply("8.8.8.8"), _reply("192.168.1.20")], ["inverter.local", "203.0.113.5", "10.0.0.7"],
                          "fe80::1")
    assert [c.host for c in out] == ["10.0.0.7", "192.168.1.20"]
    assert [c.source for c in out] == ["ha_entry", "udp_48899"]
    assert candidates_from([], [LOCAL], None) == []                 # pętla tylko dla testów
    assert [c.host for c in candidates_from([], [LOCAL], None, allow_loopback=True)] == [LOCAL]


def test_candidates_dedup_and_cap_four():
    replies = [_reply("192.168.1.20", 2712345678), _reply("192.168.1.21"), _reply("192.168.1.22"),
               _reply("192.168.1.23"), _reply("192.168.1.24")]
    out = candidates_from(replies, ["192.168.1.20", "::ffff:192.168.1.21"], "192.168.1.20")
    assert [c.host for c in out] == ["192.168.1.20", "192.168.1.21", "192.168.1.22", "192.168.1.23"]
    assert out[0].source == "manual" and out[0].logger_serial == 2712345678    # numer loggera dołączony


# ── identyfikacja ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_goodwe_identified_over_udp_sim(goodwe_udp_sim, profiles):
    words = goodwe_words()
    f = Factory({"goodwe_udp": goodwe_udp_sim})
    ident, client, used = await identify(Candidate(LOCAL, "manual"), profiles, transport_factory=f, salt=SALT)
    try:
        assert ident is not None and client is not None
        assert (ident.profile_id, ident.transport, ident.port, ident.unit_id) == ("goodwe-et", "goodwe_udp", 8899, 247)
        assert ident.model == _ascii(words, 35011, 5) and ident.rated_power_w == float(words[35001])
        assert len(ident.device_fp) == 16 and used == 1
    finally:
        await _close(client)


@pytest.mark.asyncio
async def test_goodwe_identified_over_modbus_tcp_when_udp_is_dead(modbus_tcp_sim, profiles):
    f = Factory({"modbus_tcp": modbus_tcp_sim})
    ident, client, _ = await identify(Candidate(LOCAL, "manual"), profiles, transport_factory=f, salt=SALT)
    try:
        assert ident is not None and (ident.profile_id, ident.transport, ident.port) == ("goodwe-et", "modbus_tcp", 502)
        assert f.created[0].closed is True                          # UDP bez urządzenia zamknięty
    finally:
        await _close(client)


@pytest.mark.asyncio
async def test_deye_identified_by_device_type_not_serial_block(v5_sim, deye_bank, profiles):
    for a, w in zip(range(3, 8), (0x3231, 0x3036, 0x3132, 0x3334, 0x3536)):      # "2106123456"
        deye_bank.poke(a, w)
    f = Factory({"solarman_v5": v5_sim})
    cand = Candidate(LOCAL, "udp_48899", logger_serial=V5_LOGGER_SERIAL)
    ident, client, _ = await identify(cand, profiles, transport_factory=f, salt=SALT)
    try:
        assert ident is not None and (ident.profile_id, ident.transport) == ("deye-sg", "solarman_v5")
        assert ident.model == "1280" and ident.rated_power_w == 10000.0
        assert "2106123456" not in repr(ident)
    finally:
        await _close(client)
    deye_bank.poke(0, 3)                                            # kod spoza listy profilu
    ident, client, _ = await identify(cand, profiles, transport_factory=f, salt=SALT)
    assert ident is None and client is None


@pytest.mark.asyncio
async def test_unknown_device_gives_no_identity(goodwe_udp_sim, goodwe_bank, profiles):
    for a in range(35011, 35016):
        goodwe_bank.poke(a, 0x5858)                                 # model "XXXXXXXXXX"
    f = Factory({"goodwe_udp": goodwe_udp_sim})
    ident, client, used = await identify(Candidate(LOCAL, "manual"), profiles, transport_factory=f, salt=SALT)
    assert ident is None and client is None and used >= 1
    assert all(f.closed())


@pytest.mark.asyncio
async def test_device_fp_salted_and_stable(goodwe_udp_sim, profiles):
    serial = _ascii(goodwe_words(), 35003, 8)
    f = Factory({"goodwe_udp": goodwe_udp_sim})
    fps = []
    for salt in (SALT, SALT, OTHER_SALT):
        ident, client, _ = await identify(Candidate(LOCAL, "manual"), profiles, transport_factory=f, salt=salt)
        await _close(client)
        fps.append(ident.device_fp)
    assert fps[0] == fps[1] != fps[2]
    assert serial not in fps[0] and all(c in "0123456789abcdef" for c in fps[0])
    with pytest.raises(ValueError):
        await identify(Candidate(LOCAL, "manual"), profiles, transport_factory=f, salt=b"short")


# ── próba możliwości ──────────────────────────────────────────────────────


async def _discover_goodwe(sim, profiles, **kw):
    f = Factory({"goodwe_udp": sim}, **{k: kw.pop(k) for k in ("wrap",) if k in kw})
    reports = await discover([Candidate(LOCAL, "manual")], profiles, transport_factory=f,
                             conflicts=kw.pop("conflicts", lambda host: ()), salt=SALT, **kw)
    assert len(reports) == 1
    return reports[0], f


@pytest.mark.asyncio
async def test_probe_marks_unsupported_and_echo_only(goodwe_udp_sim, goodwe_bank, profiles):
    goodwe_bank.unsupported.add(47509)
    rep, f = await _discover_goodwe(goodwe_udp_sim, profiles)
    assert rep.identity is not None and rep.modbus_status == "verified"
    assert rep.echo_only == ("soc_max",)
    assert rep.capabilities["export_limit_enabled"] is False
    assert rep.capabilities["mode"] is True and rep.capabilities["power_w"] is True
    assert rep.direct_available is True and rep.tou_readable is None
    assert all(f.closed())


@pytest.mark.asyncio
async def test_single_garbled_frame_does_not_mark_echo_only(goodwe_udp_sim, goodwe_bank, goodwe_profile,
                                                            profiles, sim_faults):
    goodwe_bank.unreadable.clear()
    f = Factory({"goodwe_udp": goodwe_udp_sim})
    ident, client, used = await identify(Candidate(LOCAL, "manual"), profiles, transport_factory=f, salt=SALT)
    try:
        sim_faults.garbage_next = 1                                 # pierwsza odpowiedź sondy to śmieci
        rep = await probe(client, goodwe_profile, ident, budget=24 - used)
    finally:
        await _close(client)
    assert rep.echo_only == () and rep.capabilities["mode"] is True


@pytest.fixture
def goodwe_profile():
    return load_builtin("goodwe-et")


@pytest.mark.asyncio
async def test_mode_unsupported_means_direct_unavailable(goodwe_udp_sim, goodwe_bank, profiles):
    goodwe_bank.unsupported.add(47511)
    rep, _ = await _discover_goodwe(goodwe_udp_sim, profiles)
    assert rep.identity is not None and rep.capabilities["mode"] is False and rep.direct_available is False


@pytest.mark.asyncio
async def test_unreadable_power_means_direct_unavailable(goodwe_udp_sim, goodwe_bank, profiles):
    goodwe_bank.unreadable.add(47512)
    rep, _ = await _discover_goodwe(goodwe_udp_sim, profiles)
    assert "power_w" in rep.echo_only and rep.direct_available is False


@pytest.mark.asyncio
async def test_deye_three_phase_tou_readable(v5_sim, profiles):
    f = Factory({"solarman_v5": v5_sim})
    reports = await discover([Candidate(LOCAL, "udp_48899", logger_serial=V5_LOGGER_SERIAL)], profiles,
                             transport_factory=f, conflicts=lambda h: (), salt=SALT)
    rep = reports[0]
    assert rep.identity.profile_id == "deye-sg" and rep.tou_readable is True
    assert rep.capabilities == {"tou": True} and rep.direct_available is True
    assert (146, 32) in {(a, n) for fc, a, n in v5_sim.log if fc == 3}


@pytest.mark.asyncio
async def test_deye_single_phase_device_type_has_no_tou(rtu_tcp_sim, deye_bank, profiles):
    deye = profiles[1]
    f = Factory({"modbus_rtu": rtu_tcp_sim})
    ident, client, used = await identify(Candidate(LOCAL, "manual", transports=("modbus_rtu",)), profiles,
                                         transport_factory=f, salt=SALT)
    try:
        single = replace(ident, model="3")                          # kod jednofazowy (inna mapa)
        before = len(rtu_tcp_sim.log)
        rep = await probe(client, deye, single, budget=24 - used)
    finally:
        await _close(client)
    assert rep.capabilities == {"tou": False} and rep.tou_readable is False and rep.direct_available is False
    assert len(rtu_tcp_sim.log) == before                           # bloku TOU nawet nie czytano


@pytest.mark.asyncio
async def test_probe_uses_only_function_3(goodwe_udp_sim, v5_sim, profiles):
    await _discover_goodwe(goodwe_udp_sim, profiles)
    f = Factory({"solarman_v5": v5_sim})
    await discover([Candidate(LOCAL, "udp_48899", logger_serial=V5_LOGGER_SERIAL)], profiles,
                   transport_factory=f, conflicts=lambda h: (), salt=SALT)
    for sim in (goodwe_udp_sim, v5_sim):
        assert sim.log and all(fc == 3 for fc, _, _ in sim.log)


@pytest.mark.asyncio
async def test_probe_respects_request_budget(goodwe_udp_sim, profiles):
    rep, _ = await _discover_goodwe(goodwe_udp_sim, profiles, budget=4)
    assert goodwe_udp_sim.requests <= 4 and rep.requests == goodwe_udp_sim.requests
    assert "budget" in rep.errors and "soc_max" not in rep.echo_only     # bez pełnych prób nie ma werdyktu
    rep, _ = await _discover_goodwe(goodwe_udp_sim, profiles, budget=0)
    assert rep.identity is None and goodwe_udp_sim.requests <= 4


@pytest.mark.asyncio
async def test_incomplete_probe_is_not_available(goodwe_udp_sim, profiles):
    rep, _ = await _discover_goodwe(goodwe_udp_sim, profiles, budget=3)
    assert rep.capabilities.get("mode") is True and rep.capabilities.get("power_w") is True
    assert rep.direct_available is False and "budget" in rep.errors


class _DropsAfter:
    """Transport, który po N odczytach traci łącze."""

    def __init__(self, inner, n=3):
        self.inner, self.n, self.reads = inner, n, 0
        self.kind, self.cfg, self.stats = inner.kind, inner.cfg, inner.stats
        self.closed = False

    async def read(self, addr, count, *, tries=None):
        self.reads += 1
        if self.reads > self.n:
            raise LinkDown("gone")
        return await self.inner.read(addr, count, tries=tries)

    async def reset_channel(self):
        await self.inner.reset_channel()

    async def close(self):
        self.closed = True
        await self.inner.close()


@pytest.mark.asyncio
async def test_link_drop_mid_probe_is_not_available(goodwe_udp_sim, profiles):
    rep, _ = await _discover_goodwe(goodwe_udp_sim, profiles, wrap=_DropsAfter)
    assert rep.identity is not None and rep.direct_available is False and "LinkDown" in rep.errors


@pytest.mark.asyncio
async def test_default_budget_is_24_frames(goodwe_udp_sim, goodwe_bank, profiles):
    goodwe_bank.unreadable.update({47509, 47510, 47511, 47512, 45356})   # każdy klucz: pełne próby
    rep, _ = await _discover_goodwe(goodwe_udp_sim, profiles)
    assert goodwe_udp_sim.requests <= 24 and rep.requests <= 24


@pytest.mark.asyncio
async def test_probe_report_has_no_serial(goodwe_udp_sim, v5_sim, profiles):
    serial = _ascii(goodwe_words(), 35003, 8)
    rep, _ = await _discover_goodwe(goodwe_udp_sim, profiles)
    f = Factory({"solarman_v5": v5_sim})
    deye = (await discover([Candidate(LOCAL, "udp_48899", logger_serial=V5_LOGGER_SERIAL)], profiles,
                           transport_factory=f, conflicts=lambda h: (), salt=SALT))[0]
    for r, secrets in ((rep, (serial, LOCAL, rep.identity.device_fp)),
                       (deye, ("SYNTHSER01", LOCAL, str(V5_LOGGER_SERIAL), deye.identity.device_fp))):
        text = json.dumps(r.to_dict())
        assert r.identity is not None
        for s in secrets:
            assert s not in text
        assert LOCAL not in repr(r) and str(V5_LOGGER_SERIAL) not in repr(r)
        assert r.identity.device_fp not in repr(r)


@pytest.mark.asyncio
async def test_conflicting_candidate_skipped(goodwe_udp_sim, profiles):
    rep, f = await _discover_goodwe(goodwe_udp_sim, profiles, conflicts=lambda host: ("goodwe",))
    assert rep.identity is None and rep.errors == ("conflict",) and rep.direct_available is False
    assert goodwe_udp_sim.requests == 0 and f.created == []


@pytest.mark.asyncio
async def test_conflict_check_failure_is_a_conflict(goodwe_udp_sim, profiles):
    def broken(host):
        raise RuntimeError("x")
    rep, f = await _discover_goodwe(goodwe_udp_sim, profiles, conflicts=broken)
    assert rep.errors == ("conflict",) and f.created == []


class _Exploding:
    """Transport, który po identyfikacji zaczyna rzucać obcym wyjątkiem."""

    def __init__(self, inner):
        self.inner = inner
        self.kind = inner.kind
        self.cfg = inner.cfg
        self.stats = inner.stats
        self.reads = 0
        self.closed = False

    async def read(self, addr, count, *, tries=None):
        self.reads += 1
        if self.reads > 1:
            raise RuntimeError("boom")
        return await self.inner.read(addr, count, tries=tries)

    async def reset_channel(self):
        await self.inner.reset_channel()

    async def close(self):
        self.closed = True
        await self.inner.close()


@pytest.mark.asyncio
async def test_transports_closed_after_probe_even_on_error(goodwe_udp_sim, profiles):
    rep, f = await _discover_goodwe(goodwe_udp_sim, profiles, wrap=_Exploding)
    assert "RuntimeError" in rep.errors and rep.direct_available is False
    assert f.created and all(t.closed for t in f.created)
    assert isinstance(rep, ProbeReport)
