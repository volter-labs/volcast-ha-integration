"""Profil z podpisanego feedu jest tylko do odczytu: żadna ścieżka (drabina, bramka planu, zgoda konta,
profil udający `verified`) nie prowadzi do zapisu na falownik. Wynik drabiny jest związany z treścią
profilu (odcisk) — każda zmiana profilu unieważnia przejście; pakietowy `verified` bez zmian."""
from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest

from custom_components.volcast.core.control.cycle import WRITE
from custom_components.volcast.core.control.ladder import VERIFIED, Ladder, device_key
from custom_components.volcast.core.control.select import control_verified
from custom_components.volcast.core.profile import (
    ORIGIN_FEED, ORIGIN_PACKAGE, direct_verified, load_builtin, profile_from_dict, writes_allowed)
from custom_components.volcast.control.verification import start_rung_for
from tests.control.test_executor_direct import (  # noqa: F401 — `issues` to fixture
    GW_DRAFT, GW_NOW, GW_V, SALT, Harness, _thaw, gw_target, issues)
from tests.control.test_verification import P, Env, FakeExecutor, FakeIO, Gate

GW_FEED = dataclasses.replace(GW_V, origin=ORIGIN_FEED)            # treść zweryfikowana, pochodzenie: feed


def _changed(profile, label="Changed label"):
    raw = _thaw(profile.raw)
    raw["label"] = label
    return dataclasses.replace(profile_from_dict(raw), origin=profile.origin)


# --- pochodzenie i odcisk ---------------------------------------------------------------------------

def test_packaged_profiles_have_package_origin_and_a_stable_fingerprint():
    a, b = load_builtin("goodwe-et"), load_builtin("goodwe-et")
    assert a.origin == ORIGIN_PACKAGE and writes_allowed(a)
    assert len(a.fingerprint) == 64 and a.fingerprint == b.fingerprint
    assert _changed(a).fingerprint != a.fingerprint


def test_feed_origin_never_counts_as_verified():
    assert direct_verified(GW_V) and not direct_verified(GW_FEED)
    assert not writes_allowed(GW_FEED)
    for integ in GW_FEED.raw["ha"]["integrations"]:
        assert not control_verified(GW_FEED, integ["domain"])
    assert start_rung_for(GW_FEED, None, direct=True) == 1


# --- wykonawca: zgoda + przejście drabiny + profil z feedu = brak zapisu ----------------------------

@pytest.mark.asyncio
async def test_feed_profile_with_consent_and_a_passed_ladder_never_writes(
        make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await Harness(make_hass, GW_FEED, gw_target(goodwe_udp_sim)).start()
    try:
        h.ex.verification = Gate(True)                              # drabina „przeszła”
        await h.ex.async_tick()
        await h.cycle()
        assert goodwe_bank.writes == [] and h.ex.last_decision.status != WRITE
        assert h.ex.verification_can_write() is False and not h.ex.owned
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_feed_profile_without_a_ladder_gate_never_writes(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await Harness(make_hass, GW_FEED, gw_target(goodwe_udp_sim)).start()
    try:
        assert h.ex.verification is None
        await h.ex.async_tick()
        assert goodwe_bank.writes == [] and h.ex.last_decision.status != WRITE
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_packaged_verified_profile_still_writes(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await Harness(make_hass, GW_V, gw_target(goodwe_udp_sim)).start()
    try:
        h.ex.verification = Gate(True)
        await h.ex.async_tick()
        assert h.ex.last_decision.status == WRITE and h.ex.verification_can_write() is True
    finally:
        await h.close()


# --- drabina związana z treścią profilu ------------------------------------------------------------

DIRECT_ID = "direct|goodwe-et|fp"


def _verified_record(profile_hash=None, start=4):
    lad = Ladder(start, P, device_key=device_key(SALT, DIRECT_ID))
    lad.state.state, lad.state.since = VERIFIED, GW_NOW
    rec = lad.to_record()
    if profile_hash is not None:
        rec["profile_hash"] = profile_hash
    return rec


def _env(monkeypatch, profile, record, start):
    io = FakeIO(kind="direct", identity=DIRECT_ID)
    return Env(monkeypatch, FakeExecutor(io, record=record, profile=profile), start=start)


@pytest.mark.asyncio
async def test_feed_profile_ladder_never_allows_the_plan(monkeypatch):
    env = _env(monkeypatch, GW_FEED, _verified_record(GW_FEED.fingerprint, start=1), start=1)
    await env.runner.async_start()
    assert env.runner.plan_allowed() is False


@pytest.mark.asyncio
async def test_profile_content_change_after_the_ladder_invalidates_it(monkeypatch):
    env = _env(monkeypatch, GW_DRAFT, _verified_record(GW_DRAFT.fingerprint, start=1), start=1)
    await env.runner.async_start()
    assert env.runner.plan_allowed() is True                       # ten sam profil — przejście ważne
    changed = _changed(GW_DRAFT)
    env2 = _env(monkeypatch, changed, env.ex.verification_record, start=1)
    await env2.runner.async_start()
    assert env2.runner.plan_allowed() is False and env2.runner.ladder.state.state != VERIFIED
    assert env2.ex.verification_record.get("profile_hash") == changed.fingerprint


@pytest.mark.asyncio
async def test_packaged_verified_profile_unaffected_by_a_content_change(monkeypatch):
    changed = _changed(GW_V)
    env = _env(monkeypatch, changed, _verified_record(GW_V.fingerprint), start=4)
    await env.runner.async_start()
    assert env.runner.plan_allowed() is True and env.runner.ladder.state.state == VERIFIED
    assert env.ex.verification_record.get("profile_hash") == changed.fingerprint


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", [GW_V, GW_DRAFT], ids=["verified", "draft"])
async def test_old_record_without_a_hash_kept_for_a_packaged_profile(monkeypatch, profile):
    start = 4 if direct_verified(profile) else 1
    env = _env(monkeypatch, profile, _verified_record(start=start), start=start)
    await env.runner.async_start()
    assert env.runner.plan_allowed() is True                       # nikt nie traci sterowania po aktualizacji
    assert env.ex.verification_record.get("profile_hash") == profile.fingerprint   # rekord już związany


@pytest.mark.asyncio
async def test_old_record_without_a_hash_reset_for_a_feed_profile(monkeypatch):
    env = _env(monkeypatch, GW_FEED, _verified_record(start=1), start=1)
    await env.runner.async_start()
    assert env.runner.plan_allowed() is False and env.runner.ladder.state.state != VERIFIED
    assert env.ex.verification_record.get("profile_hash") == GW_FEED.fingerprint


@pytest.mark.asyncio
async def test_new_ladder_is_bound_to_the_profile(monkeypatch):
    env = _env(monkeypatch, GW_DRAFT, None, start=1)
    await env.runner.async_start()
    await env.step(minutes=1)
    assert env.ex.verification_record.get("profile_hash") == GW_DRAFT.fingerprint


@pytest.mark.asyncio
async def test_malformed_stored_hash_is_treated_as_unbound(monkeypatch):
    env = _env(monkeypatch, GW_FEED, _verified_record("not-a-hash", start=1), start=1)
    await env.runner.async_start()
    assert env.runner.plan_allowed() is False


def test_ladder_record_round_trips_the_profile_hash():
    lad = Ladder(1, P, device_key=device_key(SALT, DIRECT_ID), profile_hash="ab" * 32)
    again = Ladder.from_record(lad.to_record(), P)
    assert again.profile_hash == "ab" * 32
    lad.profile_changed("cd" * 32, GW_NOW + timedelta(minutes=1))
    assert lad.profile_hash == "cd" * 32 and lad.state.rung == lad.start_rung
