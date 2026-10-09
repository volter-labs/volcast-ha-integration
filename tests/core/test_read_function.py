"""Odczyt rejestrów input (FC 4) obok holding (FC 3): ramki transportów, osobne przestrzenie adresów
w obrazie rejestrów, plan bloków per funkcja, cykl odczytu i identyfikacja.

`fc` jest opcjonalne i domyślnie 3 — profile bez niego czytają dokładnie jak dotąd (te same bloki,
to samo wywołanie transportu bez argumentu `fc`)."""
from __future__ import annotations

import pytest

from custom_components.volcast.core.discovery.identify import _read_identify
from custom_components.volcast.core.modbus.blocks import read_plan, split_block
from custom_components.volcast.core.modbus.client import RegisterClient
from custom_components.volcast.core.modbus.identity import device_fingerprint, identity_info
from custom_components.volcast.core.profile import profile_from_dict
from custom_components.volcast.core.registers import RegisterError, RegisterImage, decode, read_values
from custom_components.volcast.core.transports import v5_frames as v5
from custom_components.volcast.core.transports.base import (
    ModbusException, Stray, TransportConfig, read_req)
from custom_components.volcast.core.transports.factory import make_transport
from custom_components.volcast.core.transports.modbus_frames import mbap, read_request, rtu
from tests.core.profile_fixtures import ms_profile

SALT = bytes(range(16))
V5_SERIAL = 1234567890


# ── ramki transportów ─────────────────────────────────────────────────────


def _transport(kind: str):
    cfg = TransportConfig(kind=kind, host="127.0.0.1", port=1502, unit=1,
                          logger_serial=V5_SERIAL if kind == "solarman_v5" else None)
    return make_transport(cfg, allow_loopback=True)


def _wrap(kind: str, ctx, pdu: bytes) -> bytes:
    """PDU odpowiedzi w ramce danego transportu (MBAP, gołe RTU, RTU w kopercie V5)."""
    if kind == "modbus_tcp":
        return mbap(ctx, 1, pdu)
    if kind == "modbus_rtu":
        return rtu(1, pdu)
    payload = bytes([0x02]) + bytes(13) + rtu(1, pdu)
    return v5._build(v5.CTRL_RESPONSE, bytes([ctx & 0xFF, 0x50]), V5_SERIAL, payload)


def _response(kind: str, ctx, fc: int) -> bytes:
    return _wrap(kind, ctx, bytes([fc, 4]) + bytes.fromhex("00371234"))


def _request_pdu(kind: str, frame: bytes) -> bytes:
    if kind == "modbus_tcp":
        return frame[7:]
    if kind == "modbus_rtu":
        return frame[1:-2]
    return frame[26:-2][1:-2]          # ładunek V5 (15 B nagłówka) → ramka RTU → PDU


KINDS = ("modbus_tcp", "modbus_rtu", "solarman_v5")


@pytest.mark.parametrize("kind", KINDS)
def test_input_read_request_carries_function_4(kind):
    t = _transport(kind)
    frame, _ = t._encode(read_req(13022, 2, fc=4))
    assert _request_pdu(kind, frame) == bytes.fromhex("0432de0002")
    if kind != "modbus_tcp":
        assert read_request(1, 13022, 2, fc=4) in frame


@pytest.mark.parametrize("kind", KINDS)
def test_input_read_response_matches_and_is_delimited(kind):
    t = _transport(kind)
    req = read_req(13022, 2, fc=4)
    _, ctx = t._encode(req)
    resp = _response(kind, ctx, 4)
    assert t._frame_length(resp[:8]) == len(resp)
    assert t._match(resp, req, ctx) == [0x37, 0x1234]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("asked,answered", [(4, 3), (3, 4)])
def test_response_of_other_read_function_is_rejected(kind, asked, answered):
    # FC 3 i FC 4 mają ten sam kształt odpowiedzi — rozróżnia je wyłącznie echo funkcji.
    t = _transport(kind)
    req = read_req(13022, 2, fc=asked)
    _, ctx = t._encode(req)
    with pytest.raises(Stray):
        t._match(_response(kind, ctx, answered), req, ctx)


@pytest.mark.parametrize("kind", KINDS)
def test_exception_reply_to_input_read(kind):
    t = _transport(kind)
    req = read_req(13022, 2, fc=4)
    _, ctx = t._encode(req)
    with pytest.raises(ModbusException) as ei:
        t._match(_wrap(kind, ctx, bytes([0x84, 2])), req, ctx)
    assert ei.value.code == 2
    with pytest.raises(Stray):                       # wyjątek FC 3 nie jest odpowiedzią na FC 4
        t._match(_wrap(kind, ctx, bytes([0x83, 2])), req, ctx)


def test_read_request_default_function_is_holding():
    assert read_req(1, 2).fc == 3 and read_req(1, 2).pdu[0] == 3
    assert read_req(1, 2, fc=4).fc == 4 and read_req(1, 2, fc=4).pdu[0] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS + ("goodwe_udp",))
async def test_transport_read_passes_function_to_request(kind):
    t = _transport(kind)
    sent = []

    async def transact(req, on_send=None):
        sent.append(req)
        return [0] * req.count
    t._transact = transact
    await t.read(10, 2)
    await t.read(10, 2, fc=4)
    assert [(r.fc, r.pdu[0]) for r in sent] == [(3, 3), (4, 4)]


# ── obraz rejestrów: osobne przestrzenie holding / input ──────────────────


def test_image_keeps_holding_and_input_apart():
    img = RegisterImage.from_blocks({100: [7, 8], (4, 100): [55, 56], (3, 200): [9]})
    assert img.words(100, 2) == [7, 8]
    assert img.words(100, 2, fc=3) == [7, 8]
    assert img.words(100, 2, fc=4) == [55, 56]
    assert img.words(200, 1) == [9]
    with pytest.raises(RegisterError):
        img.words(200, 1, fc=4)


def test_decode_reads_the_register_space_of_its_function():
    img = RegisterImage.from_blocks({100: [7], (4, 100): [55]})
    assert decode({"addr": 100, "type": "u16"}, img) == 7
    assert decode({"addr": 100, "type": "u16", "fc": 3}, img) == 7
    assert decode({"addr": 100, "type": "u16", "fc": 4}, img) == 55


def test_read_values_mixes_functions_in_sums():
    img = RegisterImage.from_blocks({100: [0xFE0C], (4, 100): [55, 0, 0, 3200]})
    vals = read_values({
        "soc": {"addr": 100, "type": "u16", "fc": 4},
        "active_power_w": {"addr": 100, "type": "i16"},
        "pv_power_w": {"sum": [{"addr": 102, "type": "u32", "fc": 4}, {"addr": 100, "type": "i16"}]},
    }, img)
    assert vals == {"soc": 55, "active_power_w": -500, "pv_power_w": 2700}


# ── profil z rejestrami input ─────────────────────────────────────────────


def _input_profile():
    raw = ms_profile()
    raw["read"]["soc"] = {"addr": 100, "type": "u16", "fc": 4}
    raw["read"]["active_power_w"] = {"addr": 100, "type": "i16"}           # holding, ten sam adres
    raw["read"]["pv_power_w"] = {"sum": [{"addr": 102, "type": "u32", "fc": 4}]}
    raw["identify"]["model_register"]["fc"] = 4
    raw["identify"]["registers"]["rated_power_w"]["fc"] = 4
    raw["modbus"]["identify_reads"] = [{"addr": 35000, "count": 33, "fc": 4}]
    return profile_from_dict(raw)


HOLDING = {100: 0xFE0C, 45356: 10, 47509: 0, 47510: 0, 47511: 1, 47512: 0}
INPUT = {100: 55, 101: 0, 102: 0, 103: 3200}


def _ident_words() -> list[int]:
    words = [0] * 33
    words[1] = 5000                                       # 35001: moc znamionowa
    for i, pair in enumerate(("GW", "5K", "-E", "T ", "  ")):
        words[11 + i] = int.from_bytes(pair.encode(), "big")
    return words


class _Device:
    """Atrapa transportu z dwiema przestrzeniami rejestrów; brak rejestru → wyjątek 2."""
    kind = "modbus_tcp"

    def __init__(self, holding=None, input_=None):
        self.spaces = {3: dict(HOLDING if holding is None else holding),
                       4: dict(INPUT if input_ is None else input_)}
        self.log: list[tuple[int, int, int, bool]] = []      # (fc, adres, liczba, czy podano fc)

    async def read(self, addr, count, *, tries=None, **kw):
        fc = kw.get("fc", 3)
        self.log.append((fc, addr, count, "fc" in kw))
        space = self.spaces[fc]
        if any(a not in space for a in range(addr, addr + count)):
            raise ModbusException(2)
        return [space[a] for a in range(addr, addr + count)]


def test_profile_keeps_read_function_of_identify_reads():
    p = _input_profile()
    assert p.modbus.identify_reads == ((35000, 33, 4),)
    raw = ms_profile()
    raw["modbus"]["identify_reads"] = [{"addr": 35000, "count": 33, "fc": 3}]
    assert profile_from_dict(raw).modbus.identify_reads == ((35000, 33),)


def test_read_plan_groups_blocks_per_function():
    p = _input_profile()
    assert read_plan(p) == [(100, 1), (45356, 1), (47509, 4), (100, 4, 4)]
    assert read_plan(p, include_identify=True)[-1] == (35000, 33, 4)
    assert split_block((100, 4, 4), p) == [(100, 1, 4), (102, 2, 4)]


def test_read_plan_without_fc_is_unchanged():
    p = profile_from_dict(ms_profile())
    assert all(len(b) == 2 for b in read_plan(p, include_identify=True))


@pytest.mark.asyncio
async def test_read_state_reads_each_function_separately_and_merges():
    dev = _Device()
    client = RegisterClient(dev, _input_profile())
    r = await client.read_state()
    assert {(fc, a, n) for fc, a, n, _ in dev.log} == {(3, 100, 1), (3, 45356, 1), (3, 47509, 4), (4, 100, 4)}
    # FC 3 bez argumentu `fc` (zgodność z transportami i atrapami sprzed FC 4), FC 4 z nim
    assert all(given == (fc != 3) for fc, _, _, given in dev.log)
    assert r.values["soc"] == 55 and r.values["active_power_w"] == -500
    assert r.values["pv_power_w"] == 3200 and r.values["load_power_w"] == 3700
    assert r.device["soc_min"] == 10.0                         # klucz zapisu: odczyt z holding
    assert {"addr": 100, "count": 4, "fc": 4, "ok": True} in client.last_frames
    assert {"addr": 100, "count": 1, "ok": True} in client.last_frames


@pytest.mark.asyncio
async def test_input_block_with_hole_is_split_and_read_with_function_4():
    dev = _Device(input_={100: 55, 102: 0, 103: 3200})          # 101 nie istnieje → wyjątek 2 bloku
    r = await RegisterClient(dev, _input_profile()).read_state()
    assert [(fc, a, n) for fc, a, n, _ in dev.log if fc == 4] == [(4, 100, 4), (4, 100, 1), (4, 102, 2)]
    assert r.values["soc"] == 55 and r.values["pv_power_w"] == 3200


# ── identyfikacja z rejestrów input ───────────────────────────────────────


def test_identity_uses_input_register_space():
    p = _input_profile()
    info = identity_info(p, RegisterImage.from_blocks({(4, 35000): _ident_words()}))
    assert info == {"matched": True, "model": "GW5K-ET", "rated_power_w": 5000.0}
    # te same słowa w rejestrach holding to nie to urządzenie
    assert identity_info(p, RegisterImage.from_blocks({35000: _ident_words()}))["matched"] is False


@pytest.mark.asyncio
async def test_read_identity_reads_with_function_4():
    words = _ident_words()
    dev = _Device(input_={35000 + i: w for i, w in enumerate(words)})
    p = _input_profile()
    fp = await RegisterClient(dev, p, salt=SALT).read_identity()
    assert fp is not None
    assert fp == device_fingerprint(SALT, p, RegisterImage.from_blocks({(4, 35000): words}))
    assert dev.log == [(4, 35000, 33, True)]


@pytest.mark.asyncio
async def test_discovery_identify_reads_with_function_4():
    words = _ident_words()
    dev = _Device(input_={35000 + i: w for i, w in enumerate(words)})
    p = _input_profile()
    image, state = await _read_identify(dev, p, lambda: 10, [])
    assert state == "ok" and dev.log == [(4, 35000, 33, True)]
    assert identity_info(p, image)["matched"] is True


# ── blok rozdzielający (łącze bez korelacji, GoodWe UDP): wyłącznie bloki holding ──


def _goodwe_with_input_identify():
    from dataclasses import replace

    from custom_components.volcast.core.profile import load_builtin
    gw = load_builtin("goodwe-et")
    return replace(gw, modbus=replace(gw.modbus, verify_blocks=(),
                                      identify_reads=((35000, 33, 4), (36000, 7))))


def test_separator_candidates_are_holding_blocks_only():
    from custom_components.volcast.core.modbus.views import separator_blocks, separators_for
    p = _goodwe_with_input_identify()
    # blok input spełnia warunki długości i rozłączności, ale odczyt FC 3 pod jego adresem to inny rejestr
    assert separator_blocks(p, 47760) == ((36000, 7),)
    assert separators_for(p, (47509, 4)) == ((36000, 7),)


class _UdpDevice:
    kind = "goodwe_udp"

    def __init__(self):
        self.log: list[tuple] = []

        class _Stats:
            requests = 0
            timeouts = 0
        self.stats = _Stats()

    async def read(self, addr, count, *, tries=None, **kw):
        self.stats.requests += 1
        self.log.append((kw.get("fc", 3), addr, count))
        return [0] * count

    async def reset_channel(self):
        pass


@pytest.mark.asyncio
async def test_probe_separator_never_reads_an_input_block():
    from dataclasses import replace

    from custom_components.volcast.core.discovery.probe import _OK, _Prober
    p = _goodwe_with_input_identify()
    dev = _UdpDevice()
    prober = _Prober(RegisterClient(dev, p), 50, p)
    assert await prober._separate(47760) == _OK
    assert dev.log == [(3, 36000, 7)]
    only_input = replace(p, modbus=replace(p.modbus, identify_reads=((35000, 33, 4),)))
    dev = _UdpDevice()
    prober = _Prober(RegisterClient(dev, only_input), 50, only_input)
    assert await prober._separate(47760) != _OK               # brak kandydata — bez TypeError
    assert dev.log == [] and "unconfirmed" in prober.errors
