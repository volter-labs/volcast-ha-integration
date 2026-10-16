"""Feed profili w warstwie HA: pobranie (`get-profile-feed`), zadanie w tle poza ścieżką sterowania,
magazyn `.storage`, Naprawa „profil marki gotowy” tylko przy dopasowaniu do instalacji."""
from __future__ import annotations

import asyncio
import base64
import json
import re
import types
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from custom_components.volcast import coordinator as coord
from custom_components.volcast import repairs
from custom_components.volcast.cloud.client import Backend, VolcastCloud
from custom_components.volcast.cloud.fetcher import ScheduleFetcher
from custom_components.volcast.const import (
    DOMAIN, ISSUE_BRAND_PROFILE_READY, OPT_PROFILE_ID, PROFILE_FEED_PUBLIC_KEYS,
    PROFILE_FEED_STORAGE_KEY, PROFILE_FEED_STORAGE_VERSION)
from custom_components.volcast.core import profile_feed as pf
from custom_components.volcast.core.profile import PROFILES_DIR, builtin_ids

BASE = "https://staging.volcast.app/functions/v1"
FEED_URL = f"{BASE}/get-profile-feed"
API_KEY = "vk_" + "a" * 40
COMPONENT = Path(__file__).resolve().parents[2] / "custom_components" / "volcast"

KEY_ID = "test-key"
_PRIV = Ed25519PrivateKey.generate()
KEYS = {KEY_ID: _PRIV.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()}


def _backend() -> Backend:
    return Backend.from_dict({
        "base_url": BASE, "forecast": f"{BASE}/get-forecast-api",
        "submit_production": f"{BASE}/submit-production", "telemetry": f"{BASE}/device-telemetry",
        "schedule": f"{BASE}/get-schedule", "history_import": f"{BASE}/history-import",
        "pairing": f"{BASE}/pairing-session"})


def _acme(pid="acme-hybrid") -> dict:
    raw = json.loads((PROFILES_DIR / "sofar-hyd.json").read_text("utf-8"))
    raw["id"], raw["label"] = pid, "Acme Hybrid [click](https://evil.example) <b>x</b>"
    raw["identify"]["model_regex"] = ["^ACME-H[0-9]"]
    for integ in raw["ha"]["integrations"]:
        integ["domain"], integ["model_regex"] = "acme_power", ["(?i)\\bacme"]
    return raw


def _body(profiles=None, generated_at="2026-10-14T08:00:00.000Z") -> bytes:
    feed = {"schema_version": 1, "generated_at": generated_at, "key_id": KEY_ID,
            "profiles": profiles if profiles is not None else [_acme()]}
    sig = base64.b64encode(_PRIV.sign(pf.canonical_bytes(feed))).decode()
    return pf.canonical_bytes({"feed": feed, "key_id": KEY_ID, "signature": sig})


@pytest.fixture(autouse=True)
def _clean_registry():
    pf.set_feed_profiles({})
    yield
    pf.set_feed_profiles({})


# --- klient: adres z backendu, nagłówki, limit rozmiaru w trakcie czytania -------------------------

class _Content:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.read_chunks = 0

    async def iter_chunked(self, n):
        for c in self._chunks:
            self.read_chunks += 1
            yield c


class _Resp:
    def __init__(self, status, chunks=(), content_length=None):
        self.status = status
        self.content = _Content(chunks)
        self.content_length = content_length
        self.headers = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self, resp):
        self.resp = resp
        self.calls: list[dict] = []

    def get(self, url, **kw):
        self.calls.append({"url": url, **kw})
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


def _get(resp, etag=None):
    s = _Session(resp)
    out = asyncio.run(VolcastCloud(s, API_KEY, _backend()).async_get_profile_feed(etag))
    return out, s


def test_url_derived_from_paired_backend_and_key_only_in_header():
    out, s = _get(_Resp(200, [b'{"a":', b"1}"]), etag='"abc"')
    call = s.calls[0]
    assert call["url"] == FEED_URL and "?" not in call["url"]
    assert call["headers"] == {"X-API-Key": API_KEY, "If-None-Match": '"abc"'}
    assert call["allow_redirects"] is False and call["timeout"].total
    assert out == pf.FeedFetch("ok", b'{"a":1}')


def test_no_if_none_match_without_etag():
    _, s = _get(_Resp(200, [b"{}"]))
    assert "If-None-Match" not in s.calls[0]["headers"]


def test_not_modified():
    assert _get(_Resp(304))[0] == pf.FeedFetch("not_modified")


@pytest.mark.parametrize("status", [301, 302, 401, 404, 500, 503])
def test_other_status_is_an_error(status):
    assert _get(_Resp(status, [b"{}"]))[0] == pf.FeedFetch("error")


def test_network_error_is_an_error():
    assert _get(asyncio.TimeoutError())[0] == pf.FeedFetch("error")


def test_declared_oversize_not_read():
    resp = _Resp(200, [b"x"], content_length=pf.FEED_MAX_BYTES + 1)
    out, _ = _get(resp)
    assert out == pf.FeedFetch("error") and resp.content.read_chunks == 0


def test_oversize_stopped_while_reading():
    chunk = b"x" * (1024 * 1024)
    resp = _Resp(200, [chunk, chunk, b"x", chunk, chunk])
    out, _ = _get(resp)
    assert out == pf.FeedFetch("error") and resp.content.read_chunks == 3


def test_exactly_the_cap_is_accepted():
    resp = _Resp(200, [b"x" * pf.FEED_MAX_BYTES])
    assert _get(resp)[0].status == "ok"


# --- ScheduleFetcher: sprawdzenie feedu w tle, nigdy nie opóźnia planu ani cyklu --------------------

class _Cloud:
    def __init__(self):
        self.feed_calls = 0
        self.started = asyncio.Event()
        self.hang = True

    async def async_get_schedule(self):
        return {"schedule_id": "a", "slots": [], "fallback": {"mode": "self_consume", "soc_reserve": 10}}

    async def async_get_profile_feed(self, etag):
        self.feed_calls += 1
        self.started.set()
        if self.hang:
            await asyncio.Event().wait()
        return pf.FeedFetch("ok", _body())


class _Store:
    def __init__(self, data=None):
        self.data = data
        self.saves = 0

    async def async_load(self):
        return self.data

    async def async_save(self, data):
        self.saves += 1
        self.data = json.loads(json.dumps(data))


def _fetcher(cloud, feed):
    async def noop(*_):
        return None
    return ScheduleFetcher(cloud, on_plan=noop, on_consent=noop, on_auth_failure=noop, hass=None,
                           profile_feed=feed)


def _due_updater(store=None, keys=KEYS):
    clock = types.SimpleNamespace(t=1_000_000.0)
    upd = pf.ProfileFeedUpdater(store or _Store(), keys=keys, clock=lambda: clock.t)
    clock.t += pf.FEED_FIRST_CHECK_DELAY_S
    return upd, clock


@pytest.mark.asyncio
async def test_hanging_feed_fetch_does_not_delay_refresh_and_one_in_flight():
    cloud = _Cloud()
    upd, clock = _due_updater()
    f = _fetcher(cloud, upd)
    ticks = []
    await asyncio.wait_for(f.async_refresh(), 1)
    ticks.append("tick")                                   # cykl wykonawcy zaraz po odświeżeniu
    await asyncio.wait_for(cloud.started.wait(), 1)
    clock.t += 10 * pf.FEED_CHECK_INTERVAL_S               # nawet „należne” nie startuje drugi raz
    await asyncio.wait_for(f.async_refresh(), 1)
    ticks.append("tick")
    assert ticks == ["tick", "tick"] and cloud.feed_calls == 1
    task = f._feed_task
    await asyncio.wait_for(f.async_stop(), 2)              # rozładunek anuluje sprawdzenie
    assert task.done() and f._feed_task is None
    assert upd.due()                                       # anulowane sprawdzenie nie przepada


@pytest.mark.asyncio
async def test_feed_check_not_started_before_due_or_without_keys():
    cloud = _Cloud()
    clock = types.SimpleNamespace(t=1_000_000.0)
    early = pf.ProfileFeedUpdater(_Store(), keys=KEYS, clock=lambda: clock.t)
    f = _fetcher(cloud, early)
    await asyncio.wait_for(f.async_refresh(), 1)
    inert, _ = _due_updater(keys={})
    f2 = _fetcher(cloud, inert)
    await asyncio.wait_for(f2.async_refresh(), 1)
    await asyncio.sleep(0)
    assert cloud.feed_calls == 0 and f._feed_task is None and f2._feed_task is None


@pytest.mark.asyncio
async def test_feed_check_runs_after_refresh_and_stores_verified_feed():
    cloud = _Cloud()
    cloud.hang = False
    store = _Store()
    upd, _ = _due_updater(store)
    f = _fetcher(cloud, upd)
    assert await asyncio.wait_for(f.async_refresh(), 1) in ("accepted", "rejected")
    await asyncio.wait_for(f._feed_task, 1)
    assert store.saves == 1 and "acme-hybrid" in builtin_ids()


@pytest.mark.asyncio
async def test_feed_check_also_runs_when_the_plan_fetch_fails():
    class Down(_Cloud):
        async def async_get_schedule(self):
            raise RuntimeError("boom")
    cloud = Down()
    upd, _ = _due_updater()
    f = _fetcher(cloud, upd)
    assert await asyncio.wait_for(f.async_refresh(), 1) == "error"
    await asyncio.wait_for(cloud.started.wait(), 1)
    await asyncio.wait_for(f.async_stop(), 2)


@pytest.mark.asyncio
async def test_feed_start_error_never_breaks_refresh():
    class Broken:
        def due(self):
            raise RuntimeError("x")
    f = _fetcher(_Cloud(), Broken())
    assert await asyncio.wait_for(f.async_refresh(), 1) in ("accepted", "rejected")


@pytest.mark.asyncio
async def test_no_feed_check_after_stop():
    cloud = _Cloud()
    upd, _ = _due_updater()
    f = _fetcher(cloud, upd)
    await f.async_stop()
    await asyncio.wait_for(f.async_refresh(), 1)
    await asyncio.sleep(0)
    assert cloud.feed_calls == 0


# --- warstwa HA: magazyn, klucze, Naprawa -------------------------------------------------------

class _Entries:
    def __init__(self, entries=()):
        self._entries = list(entries)

    def async_entries(self, domain=None):
        return list(self._entries) if domain == DOMAIN else []


class _Hass:
    def __init__(self, entries=(), reports=()):
        self.data: dict = {DOMAIN: {}}
        for i, report in enumerate(reports):
            self.data[DOMAIN][f"e{i}"] = {"discovery": types.SimpleNamespace(report=report)}
        self.config_entries = _Entries(entries)
        self.jobs = 0

    async def async_add_executor_job(self, fn, *args):
        self.jobs += 1
        return fn(*args)


@pytest.fixture
def issues(monkeypatch):
    made: list[dict] = []

    def create(hass, domain, issue_id, **kw):
        made.append({"domain": domain, "issue_id": issue_id, **kw})
    monkeypatch.setattr(coord.ir, "async_create_issue", create)
    return made


def test_public_key_is_shipped_and_storage_names():
    assert list(PROFILE_FEED_PUBLIC_KEYS) == ["feed-2026-10"]
    for key_id, hexkey in PROFILE_FEED_PUBLIC_KEYS.items():
        assert re.fullmatch(r"feed-\d{4}-\d{2}", key_id)
        assert re.fullmatch(r"[0-9a-f]{64}", hexkey)          # raw Ed25519, 32 bajty
        assert len(bytes.fromhex(hexkey)) == 32
    assert (PROFILE_FEED_STORAGE_KEY, PROFILE_FEED_STORAGE_VERSION) == ("volcast_profiles", 1)


def test_manifest_does_not_list_core_dependencies():
    # cryptography i aiohttp dostarcza sam Home Assistant — hassfest zabrania ich w manifeście
    manifest = json.loads((COMPONENT / "manifest.json").read_text("utf-8"))
    assert manifest["requirements"] == []


@pytest.mark.asyncio
async def test_glue_is_active_with_the_shipped_key_and_rejects_foreign_signature(monkeypatch):
    stores = []
    monkeypatch.setattr(coord, "Store", lambda hass, version, key: stores.append(key) or _Store(
        {"document": json.loads(_body()), "checked_at": 0}))
    hass = _Hass()
    feed = await coord.async_get_profile_feed(hass)
    # klucz z paczki aktywuje feed; dokument podpisany obcym (testowym) kluczem się nie wczytuje
    assert feed.active and "acme-hybrid" not in builtin_ids()


@pytest.mark.asyncio
async def test_glue_loads_stored_feed_once_and_is_shared(monkeypatch):
    made = []

    def store(hass, version, key):
        made.append((version, key))
        return _Store({"document": json.loads(_body()), "checked_at": 0})
    monkeypatch.setattr(coord, "Store", store)
    hass = _Hass()
    a = await coord.async_get_profile_feed(hass, keys=KEYS)
    b = await coord.async_get_profile_feed(hass, keys=KEYS)
    assert a is b and made == [(1, "volcast_profiles")]
    assert "acme-hybrid" in builtin_ids() and DOMAIN in hass.data and set(hass.data[DOMAIN]) == set()


def _report(*, candidates=(), manufacturer=None):
    inv = {"domain": "solarman", "devices": [{"manufacturer": manufacturer, "model": "X"}]}
    if candidates:
        inv["profile_candidates"] = list(candidates)
    return {"inverters": [inv]}


def _entry(profile_id):
    return types.SimpleNamespace(entry_id="x", options={OPT_PROFILE_ID: profile_id}, data={})


@pytest.mark.parametrize("hass_kw", [
    {"reports": [_report(candidates=["acme-hybrid", "sofar-hyd"])]},
    {"reports": [_report(manufacturer="ACME Power GmbH")]},
    {"entries": [_entry("acme-other")]},
], ids=["discovery-candidate", "discovery-brand", "entry-brand"])
def test_repair_created_on_a_match(issues, hass_kw):
    hass = _Hass(**hass_kw)
    profiles = {"acme-hybrid": _acme()}
    assert coord.async_profile_ready_issues(hass, frozenset(profiles), profiles) == \
        [f"{ISSUE_BRAND_PROFILE_READY}_acme-hybrid"]
    issue = issues[0]
    assert issue["domain"] == DOMAIN and issue["translation_key"] == ISSUE_BRAND_PROFILE_READY
    assert issue["is_fixable"] is True and issue["data"] == {"profile_id": "acme-hybrid"}
    label = issue["translation_placeholders"]["label"]
    assert issue["translation_placeholders"]["profile"] == "acme-hybrid"
    # Etykieta z feedu w tekście Napraw (markdown): bez znaków formatowania i adresów, krótka.
    assert label.startswith("Acme Hybrid") and len(label) <= 64
    assert all(c.isalnum() or c in " -" for c in label)


@pytest.mark.parametrize("hass_kw", [
    {},
    {"reports": [_report(candidates=["sofar-hyd", "deye-sg"], manufacturer="Sofar")]},
    {"reports": [_report(manufacturer="Acmetronics Ltd")]},                    # tylko całe słowo marki
    {"entries": [_entry("acme-other")], "verified": {"acme-other"}},            # wpis na profilu verified
    {"reports": [_report(manufacturer="ACME")], "entries": [_entry("acme-other")], "verified": {"acme-other"}},
    {"reports": [None, {"inverters": "nope"}, {"inverters": [{"devices": None}]}]},
    {"entries": [_entry("sofar-hyd"), _entry(None)]},
], ids=["nothing", "other-brand", "brand-not-a-whole-word", "entry-on-verified-profile",
        "brand-of-a-verified-entry", "malformed-reports", "other-entry-brand"])
def test_no_repair_without_a_match(issues, hass_kw):
    hass_kw = dict(hass_kw)
    verified = hass_kw.pop("verified", frozenset())
    hass = _Hass(**hass_kw)
    profiles = {"acme-hybrid": _acme()}
    assert coord.async_profile_ready_issues(hass, frozenset(profiles), profiles, verified_ids=verified) == []
    assert issues == []


@pytest.mark.asyncio
async def test_repair_only_after_a_new_verified_feed_never_on_rejection(monkeypatch, issues):
    store = _Store()
    monkeypatch.setattr(coord, "Store", lambda hass, version, key: store)
    hass = _Hass(reports=[_report(candidates=["acme-hybrid"])])
    feed = await coord.async_get_profile_feed(hass, keys=KEYS)
    bad = bytearray(_body(generated_at="2026-10-13T08:00:00.000Z"))
    bad[-5] ^= 1                                                   # zepsuty podpis
    assert await feed.async_check(_const(pf.FeedFetch("ok", bytes(bad)))) == "rejected"
    assert issues == [] and store.saves == 0
    assert await feed.async_check(_const(pf.FeedFetch("ok", _body()))) == "updated"
    assert [i["issue_id"] for i in issues] == [f"{ISSUE_BRAND_PROFILE_READY}_acme-hybrid"]
    assert await feed.async_check(_const(pf.FeedFetch("ok", _body()))) == "unchanged"
    assert len(issues) == 1


@pytest.mark.asyncio
async def test_profile_withdrawn_by_a_newer_feed_disappears_from_the_registry(monkeypatch):
    store = _Store()
    monkeypatch.setattr(coord, "Store", lambda hass, version, key: store)
    feed = await coord.async_get_profile_feed(_Hass(), keys=KEYS)
    both = _body([_acme(), _acme("acme-other")], generated_at="2026-10-14T08:00:00.000Z")
    assert await feed.async_check(_const(pf.FeedFetch("ok", both))) == "updated"
    assert {"acme-hybrid", "acme-other"} <= set(builtin_ids())
    only = _body([_acme("acme-other")], generated_at="2026-10-15T08:00:00.000Z")
    assert await feed.async_check(_const(pf.FeedFetch("ok", only))) == "updated"
    assert "acme-hybrid" not in builtin_ids() and "acme-other" in builtin_ids()
    assert pf.feed_profile("acme-hybrid") is None
    # po restarcie HA z magazynu wraca już tylko zestaw bez wycofanego profilu
    again = await coord.async_get_profile_feed(_Hass(), keys=KEYS)
    assert again.held is not None and set(again.held.profiles) == {"acme-other"}
    assert "acme-hybrid" not in builtin_ids()


def _const(result):
    async def fetch(etag):
        return result
    return fetch


@pytest.mark.asyncio
async def test_repair_fix_flow_is_a_plain_confirmation():
    flow = await repairs.async_create_fix_flow(None, f"{ISSUE_BRAND_PROFILE_READY}_acme-hybrid",
                                               {"profile_id": "acme-hybrid"})
    assert type(flow) is repairs.ConfirmRepairFlow


@pytest.mark.parametrize("path", [COMPONENT / "strings.json", *sorted((COMPONENT / "translations").glob("*.json"))],
                         ids=lambda p: p.name)
def test_repair_texts_present_in_every_language(path):
    issue = json.loads(path.read_text("utf-8"))["issues"][ISSUE_BRAND_PROFILE_READY]
    assert issue["title"] and "{label}" in issue["description"]
    if path.name in ("strings.json", "en.json"):
        # Profil z feedu jest tylko do odczytu — tekst nie może obiecywać sterowania po wyborze.
        assert "until you choose" not in issue["description"]
        assert "never controls" in issue["description"]
    confirm = issue["fix_flow"]["step"]["confirm"]
    assert confirm["title"] and confirm["description"]


# --- złożenie sterowania: feed z magazynu przed odczytem profili, sprawdzenie w fetcherze ----------

@pytest.mark.asyncio
async def test_setup_control_loads_stored_feed_before_profiles_and_wires_fetcher(monkeypatch):
    from custom_components.volcast.control import runtime as rt_mod
    from tests.control.test_runtime import _entry as rt_entry, _patch, _setup_hass
    from tests.setup_harness import drain

    _patch(monkeypatch)
    store = _Store({"document": json.loads(_body()), "checked_at": 0})
    monkeypatch.setattr(coord, "Store", lambda hass, version, key: store)
    monkeypatch.setattr(coord, "PROFILE_FEED_PUBLIC_KEYS", KEYS)
    seen = {}
    real = rt_mod.ds.load_profiles

    def load_profiles():
        seen["ids"] = {p.id for p in real()}
        return real()
    monkeypatch.setattr(rt_mod.ds, "load_profiles", load_profiles)
    hass, entry = _setup_hass(), rt_entry()
    rt = await rt_mod.async_setup_control(hass, entry, report=lambda: None)
    await drain(hass)
    assert "acme-hybrid" in seen["ids"]
    assert rt.fetcher._feed is coord.async_profile_feed_if_loaded(hass)
    await rt_mod.async_unload_control(hass, rt)


@pytest.mark.asyncio
async def test_setup_control_survives_a_broken_feed_store(monkeypatch):
    from custom_components.volcast.control import runtime as rt_mod
    from tests.control.test_runtime import _entry as rt_entry, _patch, _setup_hass
    from tests.setup_harness import drain

    class Boom:
        async def async_load(self):
            raise OSError("disk")

    _patch(monkeypatch)
    monkeypatch.setattr(coord, "Store", lambda hass, version, key: Boom())
    monkeypatch.setattr(coord, "PROFILE_FEED_PUBLIC_KEYS", KEYS)
    hass, entry = _setup_hass(), rt_entry()
    rt = await rt_mod.async_setup_control(hass, entry, report=lambda: None)
    await drain(hass)
    assert rt is not None and "acme-hybrid" not in builtin_ids()
    await rt_mod.async_unload_control(hass, rt)
