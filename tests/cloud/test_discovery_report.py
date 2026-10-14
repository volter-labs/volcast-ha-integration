"""Raport rozpoznania na żądanie chmury: maskowanie adresów sieciowych, limit rozmiaru,
wysyłka raz na `request_id` z ograniczonym ponawianiem i pamięcią w Store."""
import asyncio
import json
import logging

import pytest

from custom_components.volcast.cloud import discovery_report as dr
from custom_components.volcast.cloud.client import Backend, CloudAuthError, VolcastCloud
from custom_components.volcast.cloud.discovery_report import (
    DiscoveryReportSender, mask_network, prepare_report)
from custom_components.volcast.cloud.fetcher import ScheduleFetcher
from custom_components.volcast.core.discovery.models import (
    Classification, DeviceSnap, EntitySnap, InverterFinding, StateSnap)
from custom_components.volcast.core.discovery.network import LoggerReply, NetworkProbeResult
from custom_components.volcast.core.discovery.report import build_report

from .fakes import FakeSession

BASE = "https://staging.volcast.app/functions/v1"
REPORT_URL = f"{BASE}/catalog-discovery-report"
KEY = "vk_" + "a" * 40
RID = "0b6f4f3e-2a7c-4c1e-9d5b-1f2e3d4c5b6a"
RID2 = "1c7a5e4f-3b8d-4d2f-8e6c-2a3b4c5d6e7f"

SN = "2712345678"
MAC = "AABBCCDDEEFF"
EMAIL = "owner@example.com"
IPS = ("192.168.1.50", "10.0.0.7", "fe80::1", "192.168.001.077", "2001:db8::42")


def _backend() -> Backend:
    return Backend.from_dict({
        "base_url": BASE, "forecast": f"{BASE}/get-forecast-api",
        "submit_production": f"{BASE}/submit-production", "telemetry": f"{BASE}/device-telemetry",
        "schedule": f"{BASE}/get-schedule", "history_import": f"{BASE}/history-import",
        "pairing": f"{BASE}/pairing-session"})


def _real_report() -> dict:
    """Raport z prawdziwego `build_report` z serialem, MAC-iem, e-mailem i adresami IP w polach
    tekstowych (tytuł wpisu, stan, atrybut, surowa odpowiedź 48899, URL z portem)."""
    dev = DeviceSnap("d1", "Deye", "SUN-10K", f"Deye {SN}", "1.0", None, SN, (("solarman", SN),), ("e1",))
    ents = [
        EntitySnap("sensor.deye_192_168_1_50_power", "solarman", f"solarman_{SN}_p", "d1", "e1",
                   None, "W", None, "Power", False),
        EntitySnap("select.deye_mode", "solarman", f"solarman_{SN}_mode", "d1", "e1",
                   None, None, None, "Mode", False),
    ]
    inv = InverterFinding("solarman", "e1", f"Deye at 192.168.1.50 ({EMAIL})", "192.168.1.50", [dev], ents)
    states = {"select.deye_mode": StateSnap("select.deye_mode", "http://[fe80::1%eth0]:8080/x",
                                            {"options": ["Selling First", "::ffff:10.0.0.7"]})}
    net = NetworkProbeResult(True, [LoggerReply(f"192.168.1.50,{MAC},{SN}", "192.168.1.50", MAC, SN)])
    return build_report(classification=Classification([inv], [], []), states=states, history_days={},
                        network=net, errors=["probe: connect to 10.0.0.7:502 failed"],
                        integration_version="2.0.0", ha_version="2026.10.0",
                        generated_at="2026-10-14T10:00:00+00:00")


def _assert_clean(text: str) -> None:
    for secret in (SN, MAC, "AA:BB:CC:DD:EE:FF", EMAIL, KEY, *IPS, "10.0.0.7", "192_168_1_50", "fe80"):
        assert secret.lower() not in text.lower(), secret


# --- maskowanie adresów sieciowych ---------------------------------------------------------------

@pytest.mark.parametrize("text, leaked", [
    ("Inverter at 192.168.1.50 (living room)", "192.168.1.50"),
    ("host 192.168.001.077 answered", "192.168.001.077"),
    ("leading zeros 0192.0168.0001.0010 too", "0192.0168.0001.0010"),
    ("mapped ::ffff:192.168.1.50 address", "192.168.1.50"),
    ("mapped hex ::ffff:c0a8:0132 address", "c0a8:0132"),
    ("link-local fe80::1%eth0 with zone", "fe80::1"),
    ("global 2001:db8:0:0:0:0:0:42 full", "2001:db8"),
    ("compressed 2001:db8::42.", "2001:db8::42"),
    ("url http://192.168.1.50:8080/api?x=1", "192.168.1.50"),
    ("url http://[2001:db8::42]:8080/api", "2001:db8::42"),
    ("url https://inverter.fritz.box:443/status", "inverter.fritz.box"),
    ("url with creds http://admin:pw@inverter.local/x", "admin:pw"),
    ("tcp 10.0.0.7:502 refused", "10.0.0.7"),
    ("slugified sensor.goodwe_192_168_1_50_power", "192_168_1_50"),
    ("fullwidth １９２．１６８．１．５０ digits", "192.168.1.50"),
    ("zero width 192.168​.1.50 split", "192.168"),
])
def test_ip_and_url_host_masked_in_free_text(text, leaked):
    out = mask_network(text)
    assert leaked not in out
    assert leaked.lower() not in out.lower()


def test_ip_as_dict_key_and_nested_values_masked():
    out = mask_network({"192.168.1.50": {"peers": [("10.0.0.7", {"deep": ["fe80::1%2"]})]},
                        "title": "x"})
    text = json.dumps(out)
    assert "192.168" not in text and "10.0.0.7" not in text and "fe80" not in text
    assert out["title"] == "x"


def test_host_and_ip_keys_masked_whole():
    out = mask_network({"host": "my-inverter.home", "ip": "inverter", "Host": "solar.lan"})
    assert out == {"host": "<HOST>", "ip": "<HOST>", "Host": "<HOST>"}


def test_ordinary_text_untouched():
    for s in ("Selling First", "2026-10-14T10:00:00+00:00", "12:30:45", "SUN-10K-SG04LP3-EU",
              "v1.2.3", "2026.10.0", "<SN>", "AABBCC******", "Zero Export To Load"):
        assert mask_network(s) == s


def test_non_string_scalars_kept_and_tuples_become_lists():
    assert mask_network({"a": 1, "b": 2.5, "c": None, "d": True, "e": ("x", 1)}) == \
        {"a": 1, "b": 2.5, "c": None, "d": True, "e": ["x", 1]}


def test_depth_and_size_bounded():
    deep: dict = {"v": "192.168.1.50"}
    for _ in range(500):
        deep = {"n": deep}
    out = mask_network(deep)          # bez RecursionError
    assert "192.168" not in json.dumps(out)
    wide = {"k": ["10.0.0.7"] * 60_000}
    out = mask_network(wide)
    assert "10.0.0.7" not in json.dumps(out)


# --- przygotowanie ładunku (maskowanie + rozmiar) ----------------------------------------------

def test_prepare_masks_full_real_report():
    out = prepare_report(_real_report())
    _assert_clean(json.dumps(out))
    assert out["schema"] == 1 and out["inverters"][0]["domain"] == "solarman"


def test_prepare_drops_largest_optional_section_when_too_big():
    report = _real_report()
    report["energy_sensors"] = [{"entity_id": f"sensor.e{i}", "unit": "kWh", "pad": "x" * 2000}
                                for i in range(150)]
    out = prepare_report(report)
    assert out is not None
    assert len(json.dumps(out).encode()) <= dr.MAX_REPORT_BYTES
    assert "energy_sensors" not in out and out["truncated"] == ["energy_sensors"]
    assert out["inverters"]


def test_prepare_returns_none_when_core_still_too_big():
    report = _real_report()
    report["inverters"][0]["entities"] = [{"entity_id": f"sensor.e{i}", "pad": "y" * 2000}
                                          for i in range(200)]
    assert prepare_report(report) is None


def test_prepare_refuses_non_dict():
    assert prepare_report(None) is None
    assert prepare_report(["x"]) is None


# --- klient ------------------------------------------------------------------------------------

def test_client_posts_request_id_and_report_with_api_key_only_in_header():
    s = FakeSession()
    s.add("POST", REPORT_URL, 200, {"ok": True})
    cloud = VolcastCloud(s, KEY, _backend())
    status = asyncio.run(cloud.async_post_discovery_report(RID, {"schema": 1}))
    assert status == 200
    call = s.calls[0]
    assert call["url"] == REPORT_URL and call["allow_redirects"] is False
    assert call["headers"] == {"X-API-Key": KEY}
    assert call["json"] == {"request_id": RID, "report": {"schema": 1}}


def test_client_network_error_is_status_zero():
    s = FakeSession()
    s.add("POST", REPORT_URL, asyncio.TimeoutError())
    assert asyncio.run(VolcastCloud(s, KEY, _backend()).async_post_discovery_report(RID, {})) == 0


# --- wysyłka z planu ---------------------------------------------------------------------------

SLOT = {"from": "2026-10-14T10:00:00Z", "to": "2026-10-14T11:00:00Z", "mode": "self_consume"}


def _plan(rid=RID, **extra):
    body = {"schedule_id": "a", "slots": [SLOT], "fallback": {"mode": "self_consume", "soc_reserve": 10},
            "control_enabled": True, **extra}
    if rid is not None:
        body["discovery_request"] = {"request_id": rid, "requested_at": "2026-10-14T09:00:00Z"}
    return body


class Cloud:
    def __init__(self, plans, statuses=()):
        self.plans = list(plans)
        self.statuses = list(statuses)
        self.posts: list[tuple[str, dict]] = []

    async def async_get_schedule(self):
        return self.plans.pop(0) if len(self.plans) > 1 else self.plans[0]

    async def async_post_discovery_report(self, request_id, report):
        self.posts.append((request_id, report))
        s = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if isinstance(s, Exception):
            raise s
        return s


class MemStore:
    def __init__(self, data=None, *, fail_load=False, fail_save=False):
        self.data = data
        self.fail_load, self.fail_save = fail_load, fail_save
        self.saves = 0

    async def async_load(self):
        if self.fail_load:
            raise OSError("disk")
        return self.data

    async def async_save(self, data):
        if self.fail_save:
            raise OSError("disk")
        self.saves += 1
        self.data = json.loads(json.dumps(data))


def _fetcher(cloud, store, report=_real_report, plans=None):
    got = {"plans": []}

    async def on_plan(raw, sched):
        got["plans"].append(raw)

    async def noop(*_):
        return None

    f = ScheduleFetcher(cloud, on_plan=on_plan, on_consent=noop, on_auth_failure=noop,
                        hass=None, discovery_report=report)
    f._discovery = DiscoveryReportSender(None, cloud, report, store=store)
    return f, got


def _run(f, n=1):
    return [asyncio.run(f.async_refresh()) for _ in range(n)]


def test_flag_present_sends_exactly_one_clean_report():
    cloud, store = Cloud([_plan()], [200]), MemStore()
    f, _ = _fetcher(cloud, store)
    assert _run(f) == ["accepted"]
    assert len(cloud.posts) == 1
    rid, report = cloud.posts[0]
    assert rid == RID
    _assert_clean(json.dumps(report))
    assert store.data["sent"] == [RID]


def test_same_id_second_time_no_post_also_after_restart():
    cloud, store = Cloud([_plan()], [200]), MemStore()
    f, _ = _fetcher(cloud, store)
    _run(f, 3)
    assert len(cloud.posts) == 1
    f2, _ = _fetcher(cloud, store)          # nowa instancja = restart HA, ta sama pamięć
    _run(f2, 2)
    assert len(cloud.posts) == 1


def test_no_flag_does_nothing():
    cloud, store = Cloud([_plan(rid=None)], [200]), MemStore()
    f, _ = _fetcher(cloud, store)
    _run(f, 2)
    assert cloud.posts == [] and store.saves == 0


@pytest.mark.parametrize("status", [500, 503, 0, 401, 429])
def test_retriable_failure_retries_then_gives_up_after_three(status):
    cloud, store = Cloud([_plan()], [status]), MemStore()
    f, _ = _fetcher(cloud, store)
    _run(f, 2)
    assert len(cloud.posts) == 2
    assert store.data["attempts"][RID] == 2 and RID not in store.data["sent"]
    f2, _ = _fetcher(cloud, store)          # licznik przeżywa restart
    _run(f2, 3)
    assert len(cloud.posts) == 3
    assert RID in store.data["sent"] and RID not in store.data["attempts"]


def test_retry_then_success():
    cloud, store = Cloud([_plan()], [500, 200]), MemStore()
    f, _ = _fetcher(cloud, store)
    _run(f, 3)
    assert len(cloud.posts) == 2 and store.data["sent"] == [RID] and store.data["attempts"] == {}


@pytest.mark.parametrize("status", [409, 404])
def test_conflict_and_not_found_stored_as_done(status):
    cloud, store = Cloud([_plan()], [status]), MemStore()
    f, _ = _fetcher(cloud, store)
    _run(f, 2)
    assert len(cloud.posts) == 1 and store.data["sent"] == [RID]


def test_auth_error_from_client_counts_as_retry_not_crash():
    cloud, store = Cloud([_plan()], [CloudAuthError()]), MemStore()
    f, _ = _fetcher(cloud, store)
    assert _run(f) == ["accepted"]
    assert store.data["attempts"][RID] == 1


def test_sent_list_capped_at_twenty():
    store = MemStore({"sent": [f"00000000-0000-4000-8000-{i:012d}" for i in range(20)], "attempts": {}})
    cloud = Cloud([_plan()], [200])
    f, _ = _fetcher(cloud, store)
    _run(f)
    assert len(store.data["sent"]) == 20 and store.data["sent"][-1] == RID


def test_store_load_and_save_failures_do_not_break_refresh():
    for store in (MemStore(fail_load=True), MemStore(fail_save=True)):
        cloud = Cloud([_plan()], [200])
        f, got = _fetcher(cloud, store)
        assert _run(f, 2) == ["accepted", "unchanged"]
        assert len(cloud.posts) == 1          # pamięć w procesie nadal chroni przed powtórką
        assert len(got["plans"]) == 1


def test_report_not_ready_waits_without_counting_attempt():
    state = {"r": None}
    cloud, store = Cloud([_plan()], [200]), MemStore()
    f, _ = _fetcher(cloud, store, report=lambda: state["r"])
    _run(f)
    assert cloud.posts == [] and store.saves == 0
    state["r"] = _real_report()
    _run(f)
    assert len(cloud.posts) == 1


def test_report_too_big_is_not_sent_and_gives_up():
    big = _real_report()
    big["inverters"][0]["entities"] = [{"entity_id": f"sensor.e{i}", "pad": "y" * 2000} for i in range(200)]
    cloud, store = Cloud([_plan()], [200]), MemStore()
    f, _ = _fetcher(cloud, store, report=lambda: big)
    _run(f, 2)
    assert cloud.posts == [] and store.data["sent"] == [RID]


def test_report_callback_raising_does_not_break_refresh():
    def boom():
        raise RuntimeError("x")
    cloud, store = Cloud([_plan()], [200]), MemStore()
    f, _ = _fetcher(cloud, store, report=boom)
    assert _run(f) == ["accepted"] and cloud.posts == []


@pytest.mark.parametrize("block", [
    "x", {"request_id": 5}, {"request_id": "not-a-uuid"}, {"request_id": "../" + RID}, {},
])
def test_malformed_request_ignored(block):
    plan = _plan(rid=None)
    plan["discovery_request"] = block
    cloud, store = Cloud([plan], [200]), MemStore()
    f, _ = _fetcher(cloud, store)
    assert _run(f) == ["accepted"] and cloud.posts == []


def test_discovery_request_not_in_plan_signature_nor_stored_plan():
    cloud, store = Cloud([_plan(rid=None), _plan(), _plan(rid=RID2)], [200]), MemStore()
    f, got = _fetcher(cloud, store)
    assert _run(f, 3) == ["accepted", "unchanged", "unchanged"]
    assert all("discovery_request" not in p for p in got["plans"])
    assert [r for r, _ in cloud.posts] == [RID, RID2]


def test_report_content_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    cloud, store = Cloud([_plan()], [500]), MemStore()
    f, _ = _fetcher(cloud, store)
    _run(f, 3)
    text = caplog.text
    assert "solarman" not in text and "Deye" not in text and "<SN>" not in text


def test_fetcher_without_report_source_ignores_flag():
    cloud = Cloud([_plan()], [200])

    async def noop(*_):
        return None

    f = ScheduleFetcher(cloud, on_plan=noop, on_consent=noop, on_auth_failure=noop)
    assert asyncio.run(f.async_refresh()) == "accepted" and cloud.posts == []


@pytest.mark.asyncio
async def test_control_setup_wires_last_discovery_report_into_fetcher(monkeypatch):
    """Regresja okablowania: fetcher sterowania dostaje ostatni raport `DiscoveryRunner`."""
    from custom_components.volcast.control import runtime as rt_mod
    from tests.control.test_runtime import _entry, _patch, _setup_hass
    from tests.setup_harness import drain
    _patch(monkeypatch)
    hass, entry = _setup_hass(), _entry()
    marker = {"schema": 1}
    rt = await rt_mod.async_setup_control(hass, entry, report=lambda: marker)
    await drain(hass)
    assert rt.fetcher._discovery is not None and rt.fetcher._discovery._report() is marker
    await rt_mod.async_unload_control(hass, rt)
