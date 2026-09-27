"""Diagnostyka trybu bezpośredniego: stan połączenia, budżet, próba; ramki tylko w próbie i zamaskowane."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from custom_components.volcast.const import DOMAIN
from custom_components.volcast.control.direct import target_fingerprint
from custom_components.volcast.core.control.select import ProfileChoice
from custom_components.volcast.core.transports.modbus_frames import parse_aa55_read, parse_rtu_read
from custom_components.volcast.core.transports.v5_frames import _checked
from custom_components.volcast.diagnostics import async_get_config_entry_diagnostics
from tests.control.test_executor_direct import (  # noqa: F401 — fixture `issues`
    DEYE_V, GW_V, SALT, Harness, deye_target, gw_target, issues)
from tests.sim.fixtures import V5_LOGGER_SERIAL, deye_words, goodwe_words
from tests.test_options_direct import report

GW_SERIAL = bytes(b for a in range(35003, 35011) for b in goodwe_words()[a].to_bytes(2, "big"))
DEYE_SERIAL = bytes(b for a in range(3, 8) for b in deye_words()[a].to_bytes(2, "big"))


async def _diag(h, *, probe=()):
    rt = SimpleNamespace(executor=h.ex, choice=ProfileChoice(h.profile, None, None), mapped={}, direct=h.conn,
                         last_probe=list(probe))
    h.hass.data.setdefault(DOMAIN, {})[h.entry.entry_id] = {"control": rt}
    entry = SimpleNamespace(entry_id=h.entry.entry_id, options=h.options, data={"api_key": "vk_x"})
    return await async_get_config_entry_diagnostics(h.hass, entry)


def _trial(make_hass, profile, target):
    return Harness(make_hass, profile, target, options={"direct_trial": True, "direct_target": target}, trial=True)


@pytest.mark.asyncio
async def test_diagnostics_direct_redacts_serial_and_host(make_hass, goodwe_udp_sim, issues):
    target = gw_target(goodwe_udp_sim)
    h = await _trial(make_hass, GW_V, target).start()
    try:
        await h.ex.async_tick()
        out = await _diag(h, probe=[report()])
        d = out["control"]["direct"]
        blob = json.dumps(out)
        for secret in (goodwe_udp_sim.host, target["device_fp"], SALT.hex(), target_fingerprint(target, SALT),
                       GW_SERIAL.decode("ascii", "replace"), GW_SERIAL.hex(), "192.168.77.9"):
            assert secret not in blob
        assert d["profile"] == "goodwe-et" and d["transport"] == "goodwe_udp" and d["status"] == "trial"
        assert d["refused"] is None and d["identity"] == "confirmed" and d["echo_only"] == ["soc_max"]
        assert d["stats"]["requests"] > 0 and d["monitor"] == {"state": "ok", "reason": None}
        assert d["probe"]["identity"]["profile_id"] == "goodwe-et" and "device_fp" not in json.dumps(d["probe"])
        assert set(d["nvm"]) >= {"keys", "total", "hit", "safety_offs", "restore_ineffective"}
        assert d["last_decision"]["status"] == "dry_run"
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_budget_hits_and_safety_offs_visible(make_hass, goodwe_udp_sim, issues):
    h = await Harness(make_hass, GW_V, gw_target(goodwe_udp_sim)).start()
    try:
        await h.ex.async_tick()
        mem = h.ex._memory
        for _ in range(mem.budget.per_key):
            mem.budget.note("power_w", h.utc().timestamp())
        mem.budget.exhausted({"power_w"}, h.utc().timestamp())
        mem.tou_safety_offs.append(h.clock())
        d = (await _diag(h))["control"]["direct"]
        assert d["nvm"]["keys"]["power_w"] >= mem.budget.per_key and d["nvm"]["hit"] is True
        assert d["nvm"]["total"] == sum(d["nvm"]["keys"].values()) and d["nvm"]["safety_offs"] == 1
        assert "frames" not in d
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_frames_only_in_trial(make_hass, goodwe_udp_sim, issues):
    h = await Harness(make_hass, GW_V, gw_target(goodwe_udp_sim)).start()
    try:
        assert "frames" not in (await _diag(h))["control"]["direct"]
        assert h.conn.client.record is False
    finally:
        await h.close()
    t = await _trial(make_hass, GW_V, gw_target(goodwe_udp_sim)).start()
    try:
        frames = (await _diag(t))["control"]["direct"]["frames"]
        assert frames and all(set(f) == {"offset", "count", "request", "response"} for f in frames)
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_frames_serial_registers_zeroed_crc_valid(make_hass, goodwe_udp_sim, issues):
    h = await _trial(make_hass, GW_V, gw_target(goodwe_udp_sim)).start()
    try:
        frames = (await _diag(h))["control"]["direct"]["frames"]
        ident = next(f for f in frames if f["offset"] <= 35003 < f["offset"] + f["count"])
        words = parse_aa55_read(bytes.fromhex(ident["response"]), 0xF7, ident["count"])
        assert words[35003 - ident["offset"]:35011 - ident["offset"]] == [0] * 8
        other = next(f for f in frames if f["offset"] > 35011)
        assert parse_aa55_read(bytes.fromhex(other["response"]), 0xF7, other["count"])     # reszta nietknięta
        for f in frames:
            assert GW_SERIAL not in bytes.fromhex(f["response"] or "")
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_v5_frames_logger_serial_redacted_checksum_valid(make_hass, v5_sim, issues):
    target = deye_target(v5_sim, transport="solarman_v5", logger_serial=V5_LOGGER_SERIAL)
    h = Harness(make_hass, DEYE_V, target, options={"direct_trial": True, "direct_target": target}, trial=True,
                rated=10000.0)
    await h.start()
    try:
        out = await _diag(h)
        assert str(V5_LOGGER_SERIAL) not in json.dumps(out)
        frames = out["control"]["direct"]["frames"]
        serial_le = V5_LOGGER_SERIAL.to_bytes(4, "little")
        for f in frames:
            for field in ("request", "response"):
                raw = _checked(bytes.fromhex(f[field]))                  # suma kontrolna V5 poprawna
                assert raw[7:11] == bytes(4) and serial_le not in raw
        ident = next(f for f in frames if f["offset"] <= 3 < f["offset"] + f["count"])
        rtu = _checked(bytes.fromhex(ident["response"]))[11 + 14:-2]
        words = parse_rtu_read(rtu, 1, ident["count"])                    # CRC RTU poprawne
        assert words[3 - ident["offset"]:8 - ident["offset"]] == [0] * 5
        assert all(DEYE_SERIAL not in bytes.fromhex(f["response"]) for f in frames)
    finally:
        await h.close()


def test_diagnostics_without_direct_unchanged():
    import asyncio
    ex = SimpleNamespace(exec_summary=lambda: {"decision": None}, tou_preview=None, foreign_changes=[])
    rt = SimpleNamespace(executor=ex, choice=None, mapped={})
    entry = SimpleNamespace(entry_id="e1", options={}, data={"api_key": "vk_x"})
    hass = SimpleNamespace(data={DOMAIN: {"e1": {"control": rt}},
                                 "device_registry": SimpleNamespace(devices={})})
    out = asyncio.run(async_get_config_entry_diagnostics(hass, entry))
    assert set(out["control"]) == {"profile", "integration_domain", "mapped", "exec", "tou_preview",
                                   "foreign_changes"}


@pytest.mark.asyncio
async def test_recorded_trial_frames_pass_the_import_guard(make_hass, goodwe_udp_sim, tmp_path, issues):
    from tools.golden import import_direct_frames as imp
    h = await _trial(make_hass, GW_V, gw_target(goodwe_udp_sim)).start()
    try:
        src = tmp_path / "diag.json"
        src.write_text(json.dumps(await _diag(h)))
        out = tmp_path / "frames.json"
        imp.main([str(src), "--profile", "goodwe-et", "--out", str(out)])
        doc = json.loads(out.read_text())
        assert doc and all(v["valid"] for v in doc.values())
    finally:
        await h.close()
