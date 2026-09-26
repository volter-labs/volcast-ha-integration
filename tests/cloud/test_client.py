import asyncio
import logging

import aiohttp
import pytest

from custom_components.volcast.cloud.client import (Backend, CloudAuthError, PairingClient,
                                                    PairingDisabled, PairingError, PairingSession,
                                                    VolcastCloud)

from .fakes import FakeSession

BASE = "https://staging.example.test"
BACKEND = {"base_url": BASE, "forecast": f"{BASE}/functions/v1/get-forecast-api",
           "submit_production": f"{BASE}/functions/v1/submit-production",
           "telemetry": f"{BASE}/functions/v1/device-telemetry",
           "schedule": f"{BASE}/functions/v1/get-schedule",
           "history_import": f"{BASE}/functions/v1/import-consumption-history",
           "pairing": f"{BASE}/functions/v1/pairing-session"}
KEY = "vk_" + "a" * 64
PAIR = f"{BASE}/functions/v1/pairing-session"
# Kształty jak w chmurze: session_id to UUID v4, poll_token `vps_` + 64 hex.
SID = "3f2b8c1e-6a4d-4e2f-9b7a-1c2d3e4f5a6b"
POLL = "vps_" + "b" * 64
USER = "0d9c2f4e-1b3a-4c5d-8e6f-7a8b9c0d1e2f"
EVIL = "http://evil.example/steal"
CONNECT = f"https://volcast.app/connect?s={SID}"
SESSION = PairingSession(SID, POLL, CONNECT, "2026-09-27T10:10:00.000Z")
BEGIN_OK = {"session_id": SID, "poll_token": POLL, "connect_url": CONNECT,
            "expires_at": "2026-09-27T10:10:00.000Z"}


def run(c):
    return asyncio.run(c)


def begin(s, **kw):
    args = {"instance_id": "abcdef123456", "instance_name": "Home", "ha_version": "2026.9.1",
            "client_version": "2.0.0b2", **kw}
    return run(PairingClient(s, PAIR).async_begin(**args))


# --------------------------------------------------------------------- Backend

def test_backend_rejects_http_and_trailing_slash():
    assert Backend.from_dict(BACKEND).schedule == BACKEND["schedule"]
    assert Backend.from_dict({**BACKEND, "telemetry": "http://kong:8000/functions/v1/device-telemetry"}) is None
    assert Backend.from_dict({**BACKEND, "base_url": BASE + "/"}) is None
    assert Backend.from_dict({k: v for k, v in BACKEND.items() if k != "pairing"}) is None
    assert Backend.from_dict({**BACKEND, "schedule": "https://"}) is None
    assert Backend.from_dict({**BACKEND, "schedule": "https://a b/x"}) is None
    assert Backend.from_dict({**BACKEND, "forecast": None}) is None
    assert Backend.from_dict("x") is None


def test_backend_round_trip_ignores_extra_keys():
    b = Backend.from_dict({**BACKEND, "extra": "https://ignored.example"})
    assert b.as_dict() == BACKEND
    assert Backend.from_dict(b.as_dict()) == b


# ----------------------------------------------------------------- VolcastCloud

def test_schedule_url_has_contract_2_and_key_header():
    s = FakeSession()
    s.add("GET", BACKEND["schedule"], 200, {"slots": [], "control_enabled": False})
    out = run(VolcastCloud(s, KEY, Backend.from_dict(BACKEND)).async_get_schedule())
    assert out == {"slots": [], "control_enabled": False}
    assert s.calls[0]["url"] == BACKEND["schedule"] + "?contract=2"
    assert s.calls[0]["headers"] == {"X-API-Key": KEY}


@pytest.mark.parametrize("status,body", [(500, {}), (405, {}), (200, ["x"]), (200, None),
                                         (200, ValueError("bad json"))])
def test_schedule_failures_are_none(status, body):
    s = FakeSession()
    s.add("GET", BACKEND["schedule"], status, body)
    assert run(VolcastCloud(s, KEY, Backend.from_dict(BACKEND)).async_get_schedule()) is None


def test_schedule_401_raises_auth():
    s = FakeSession()
    s.add("GET", BACKEND["schedule"], 401, {"error": "Invalid API key"})
    with pytest.raises(CloudAuthError):
        run(VolcastCloud(s, KEY, Backend.from_dict(BACKEND)).async_get_schedule())


@pytest.mark.parametrize("err", [aiohttp.ClientError("down"), asyncio.TimeoutError()])
def test_network_error_is_none(err):
    s = FakeSession()
    s.add("GET", BACKEND["schedule"], err)
    assert run(VolcastCloud(s, KEY, Backend.from_dict(BACKEND)).async_get_schedule()) is None


def test_telemetry_and_history_bodies():
    s = FakeSession()
    s.add("POST", BACKEND["telemetry"], 200, {"stored": 1})
    s.add("POST", BACKEND["history_import"], 200, {"accepted": 1, "inserted": 1})
    cloud = VolcastCloud(s, KEY, Backend.from_dict(BACKEND))
    assert run(cloud.async_post_telemetry({"timestamp": "t"})) is True
    assert s.calls[0]["json"] == {"readings": [{"timestamp": "t"}]}
    assert s.calls[0]["headers"] == {"X-API-Key": KEY}
    hours = [{"start": "2026-09-01T10:00:00Z", "load_kwh": 0.4}]
    assert run(cloud.async_import_history(hours)) == {"accepted": 1, "inserted": 1}
    assert s.calls[1]["json"] == {"source": "ha_recorder", "hours": hours}
    assert s.calls[1]["headers"] == {"X-API-Key": KEY}


@pytest.mark.parametrize("status", [400, 401, 403, 500, aiohttp.ClientError("down")])
def test_telemetry_failure_is_false_never_raises(status):
    s = FakeSession()
    s.add("POST", BACKEND["telemetry"], status, {"error": "x"})
    assert run(VolcastCloud(s, KEY, Backend.from_dict(BACKEND)).async_post_telemetry({})) is False


@pytest.mark.parametrize("status,body", [(400, {"error": "too_many_hours"}), (500, {"inserted": 3}),
                                         (200, ["x"]), (401, {}), (aiohttp.ClientError("x"), None)])
def test_history_failure_is_none(status, body):
    s = FakeSession()
    s.add("POST", BACKEND["history_import"], status, body)
    assert run(VolcastCloud(s, KEY, Backend.from_dict(BACKEND)).async_import_history([])) is None


# --------------------------------------------------------------- PairingClient

def test_begin_ok_disabled_and_rate_limited():
    s = FakeSession()
    s.add("POST", PAIR, 201, BEGIN_OK)
    got = begin(s)
    assert got == SESSION
    assert s.calls[0]["json"] == {"action": "begin", "kind": "ha", "instance_id": "abcdef123456",
                                  "instance_name": "Home", "ha_version": "2026.9.1",
                                  "client_version": "2.0.0b2"}
    assert "headers" not in s.calls[0] or "X-API-Key" not in (s.calls[0]["headers"] or {})
    s2 = FakeSession()
    s2.add("POST", PAIR, 503, {"error": "pairing_disabled"})
    with pytest.raises(PairingDisabled):
        begin(s2)


@pytest.mark.parametrize("status,body", [
    (429, {"error": "too_many_sessions"}),
    (429, {"error": "too_many_from_origin"}),
    (429, {"error": "pairing_busy"}),
    (400, {"error": "invalid_request"}),
    (500, {"error": "internal_error"}),
    (200, BEGIN_OK),
    (201, ["x"]),
    (201, {k: v for k, v in BEGIN_OK.items() if k != "poll_token"}),
    (aiohttp.ClientError("down"), None),
])
def test_begin_refusals_are_pairing_error(status, body):
    s = FakeSession()
    s.add("POST", PAIR, status, body)
    with pytest.raises(PairingError):
        begin(s)


@pytest.mark.parametrize("field,value", [
    ("connect_url", "http://evil/x"),
    ("connect_url", "https://volcast.app/connect ?s=1"),
    ("session_id", "s1"),
    ("session_id", 7),
    ("poll_token", "vps_short"),
    ("poll_token", "vk_" + "a" * 64),
    ("expires_at", None),
])
def test_begin_rejects_malformed_session(field, value):
    s = FakeSession()
    s.add("POST", PAIR, 201, {**BEGIN_OK, field: value})
    with pytest.raises(PairingError):
        begin(s)


def test_begin_cleans_descriptive_fields_to_cloud_limits():
    s = FakeSession()
    s.add("POST", PAIR, 201, BEGIN_OK)
    begin(s, instance_name="  Dom\n\tna wsi " + "x" * 80, ha_version="2026.9.1\x00" + "9" * 40,
          client_version="\x07")
    body = s.calls[0]["json"]
    assert body["instance_name"].startswith("Dom na wsi ") and len(body["instance_name"]) <= 64
    assert not any(ord(c) < 32 or ord(c) == 127 for c in body["instance_name"])
    assert body["ha_version"].startswith("2026.9.1") and len(body["ha_version"]) <= 32
    assert "\x00" not in body["ha_version"]
    assert "client_version" not in body          # puste po czyszczeniu = pole pominięte


def test_begin_empty_name_falls_back():
    s = FakeSession()
    s.add("POST", PAIR, 201, BEGIN_OK)
    begin(s, instance_name=" \n ")
    assert s.calls[0]["json"]["instance_name"] == "Home Assistant"


@pytest.mark.parametrize("status,body,expected", [
    (202, {"status": "pending", "expires_at": "2026-09-27T10:10:00.000Z"}, "pending"),
    (410, {"status": "expired"}, "expired"),
    (410, {"status": "cancelled"}, "expired"),
    (404, {"error": "not_found"}, "gone"),
    (503, {"error": "pairing_disabled"}, "disabled"),
    (500, {"error": "credentials_unavailable"}, "error"),
    (500, {}, "error"),
    (200, ["x"], "error"),
    (200, {"status": "weird"}, "error"),
    (aiohttp.ClientError("down"), None, "error"),
    (200, {"status": "confirmed", "user_id": USER, "api_key": "bad", "backend": BACKEND}, "error"),
    (200, {"status": "confirmed", "user_id": USER, "api_key": "vk_" + "a" * 20, "backend": BACKEND}, "error"),
    (200, {"status": "confirmed", "user_id": None, "api_key": KEY, "backend": BACKEND}, "error"),
    (200, {"status": "confirmed", "user_id": USER, "api_key": KEY,
           "backend": {**BACKEND, "schedule": "http://x"}}, "error"),
])
def test_poll_statuses(status, body, expected):
    s = FakeSession()
    s.add("POST", PAIR, status, body)
    assert run(PairingClient(s, PAIR).async_poll(SESSION)).status == expected


def test_poll_confirmed_carries_credentials_and_consumed_choices():
    s = FakeSession()
    s.add("POST", PAIR, 200, {"status": "confirmed", "user_id": USER, "api_key": KEY, "backend": BACKEND})
    r = run(PairingClient(s, PAIR).async_poll(SESSION))
    assert (r.status, r.api_key, r.user_id, r.backend.schedule) == ("confirmed", KEY, USER, BACKEND["schedule"])
    assert s.calls[0]["json"] == {"action": "poll", "session_id": SID, "poll_token": POLL}
    s2 = FakeSession()
    s2.add("POST", PAIR, 200, {"status": "consumed", "choices": {"control_mode": "entities"}})
    r2 = run(PairingClient(s2, PAIR).async_poll(SESSION))
    assert (r2.status, r2.choices, r2.api_key) == ("consumed", {"control_mode": "entities"}, None)
    s3 = FakeSession()
    s3.add("POST", PAIR, 200, {"status": "consumed", "choices": None})
    assert run(PairingClient(s3, PAIR).async_poll(SESSION)).choices == {}


def test_progress_body_and_result():
    steps = [{"key": "account", "state": "done"}, {"key": "inverter", "state": "active", "detail": "GoodWe"}]
    s = FakeSession()
    s.add("POST", PAIR, 200, {"ok": True})
    assert run(PairingClient(s, PAIR).async_progress(SESSION, steps)) is True
    assert s.calls[0]["json"] == {"action": "progress", "session_id": SID, "poll_token": POLL, "steps": steps}
    for status in (409, 400, 404, aiohttp.ClientError("x")):
        s2 = FakeSession()
        s2.add("POST", PAIR, status, {"error": "not_live"})
        assert run(PairingClient(s2, PAIR).async_progress(SESSION, steps)) is False


def test_progress_detail_cleaned_to_cloud_limits():
    s = FakeSession()
    s.add("POST", PAIR, 200, {"ok": True})
    steps = [{"key": "inverter", "state": "error", "detail": "zły\nstan\x00 " + "y" * 300}]
    run(PairingClient(s, PAIR).async_progress(SESSION, steps))
    detail = s.calls[0]["json"]["steps"][0]["detail"]
    assert detail.startswith("zły stan") and len(detail) <= 200
    assert not any(ord(c) < 32 or ord(c) == 127 for c in detail)
    assert steps[0]["detail"].startswith("zły\n")        # wejście nietknięte


def test_request_plan_cooldown_is_skip_not_error():
    s = FakeSession()
    s.add("POST", PAIR, 429, {"error": "plan_recently_requested"})
    assert run(PairingClient(s, PAIR).async_request_plan(SESSION)) == {"skipped": "cooldown"}
    assert s.calls[0]["json"] == {"action": "request_plan", "session_id": SID, "poll_token": POLL}


def test_request_plan_result_and_failures():
    s = FakeSession()
    s.add("POST", PAIR, 200, {"success": True, "skipped": None, "slots_count": 24})
    assert run(PairingClient(s, PAIR).async_request_plan(SESSION)) == {
        "success": True, "skipped": None, "slots_count": 24}
    for status in (409, 404, 503, aiohttp.ClientError("x")):
        s2 = FakeSession()
        s2.add("POST", PAIR, status, {"error": "not_live"})
        assert run(PairingClient(s2, PAIR).async_request_plan(SESSION)) is None


@pytest.mark.parametrize("status", [200, 409, 404, aiohttp.ClientError("x")])
def test_cancel_never_raises(status):
    s = FakeSession()
    s.add("POST", PAIR, status, {})
    assert run(PairingClient(s, PAIR).async_cancel(SESSION)) is None
    assert s.calls[0]["json"] == {"action": "cancel", "session_id": SID, "poll_token": POLL}


def test_secrets_never_logged(caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.volcast.cloud")
    s = FakeSession()
    s.add("GET", BACKEND["schedule"], aiohttp.ClientError(f"boom {KEY}"))
    s.add("POST", BACKEND["telemetry"], 500, {"error": KEY})
    s.add("POST", PAIR, aiohttp.ClientError(f"boom {POLL}"))
    cloud = VolcastCloud(s, KEY, Backend.from_dict(BACKEND))
    run(cloud.async_get_schedule())
    run(cloud.async_post_telemetry({}))
    run(PairingClient(s, PAIR).async_poll(SESSION))
    del s.routes[("POST", PAIR)]
    s.add("POST", PAIR, 200, {"status": "confirmed", "user_id": USER, "api_key": "vk_bad", "backend": BACKEND})
    run(PairingClient(s, PAIR).async_poll(SESSION))
    assert caplog.records                                   # coś zalogowano…
    assert KEY not in caplog.text and POLL not in caplog.text and "vk_bad" not in caplog.text


# ------------------------------------------------ przekierowania i czasy (poprawki)

def _all_calls_safe(s):
    return all(c.get("allow_redirects") is False and c.get("timeout") is not None for c in s.calls)


@pytest.mark.parametrize("code", [301, 302, 307, 308])
def test_schedule_redirect_is_refused_and_key_stays_home(code):
    s = FakeSession()
    s.add("GET", BACKEND["schedule"], code, None, headers={"Location": EVIL})
    s.add("GET", EVIL, 200, {"slots": []})
    assert run(VolcastCloud(s, KEY, Backend.from_dict(BACKEND)).async_get_schedule()) is None
    assert [c["url"] for c in s.calls] == [BACKEND["schedule"] + "?contract=2"]
    assert _all_calls_safe(s)


@pytest.mark.parametrize("code", [302, 307, 308])
def test_post_redirects_never_forward_key_or_body(code):
    s = FakeSession()
    for url in (BACKEND["telemetry"], BACKEND["history_import"], PAIR):
        s.add("POST", url, code, None, headers={"Location": EVIL})
    s.add("POST", EVIL, 200, {"ok": True})
    cloud = VolcastCloud(s, KEY, Backend.from_dict(BACKEND))
    assert run(cloud.async_post_telemetry({"timestamp": "t"})) is False
    assert run(cloud.async_import_history([{"start": "x"}])) is None
    client = PairingClient(s, PAIR)
    assert run(client.async_poll(SESSION)).status == "error"
    assert run(client.async_progress(SESSION, [])) is False
    assert run(client.async_request_plan(SESSION)) is None
    assert run(client.async_cancel(SESSION)) is None
    with pytest.raises(PairingError):
        begin(s)
    assert not any(c["url"] == EVIL for c in s.calls)
    assert _all_calls_safe(s)


def test_request_plan_waits_longer_than_cloud_planner_budget():
    s = FakeSession()
    s.add("POST", PAIR, 200, {"success": True, "skipped": None, "slots_count": 24}, delay_s=30)
    assert run(PairingClient(s, PAIR).async_request_plan(SESSION)) == {
        "success": True, "skipped": None, "slots_count": 24}
    assert s.calls[0]["timeout"].total >= 50           # chmura daje planerowi 45 s
    s2 = FakeSession()
    s2.add("POST", PAIR, 200, {"ok": True}, delay_s=30)
    assert run(PairingClient(s2, PAIR).async_progress(SESSION, [])) is False   # reszta: krótki czas


def test_history_import_gets_longer_timeout_than_telemetry():
    s = FakeSession()
    s.add("POST", BACKEND["history_import"], 200, {"accepted": 1}, delay_s=25)
    s.add("POST", BACKEND["telemetry"], 200, {}, delay_s=25)
    cloud = VolcastCloud(s, KEY, Backend.from_dict(BACKEND))
    assert run(cloud.async_import_history([])) == {"accepted": 1}
    assert run(cloud.async_post_telemetry({})) is False


@pytest.mark.parametrize("bad", [
    {"schedule": f"{BASE}/functions/v1/get-schedule?x=1"},
    {"schedule": f"{BASE}/functions/v1/get-schedule#f"},
    {"schedule": "https://u@staging.example.test/functions/v1/get-schedule"},
    {"base_url": "https://u:p@staging.example.test"},
    {"telemetry": "https://other.example/functions/v1/device-telemetry"},
    {"telemetry": BASE + "evil.example/functions/v1/device-telemetry"},
    {"schedule": BASE},
])
def test_backend_endpoints_must_be_plain_https_under_base(bad):
    assert Backend.from_dict({**BACKEND, **bad}) is None


def test_backend_base_with_path_prefix_is_fine():
    base = "https://example.test/volcast"
    raw = {k: (base if k == "base_url" else v.replace(BASE, base)) for k, v in BACKEND.items()}
    assert Backend.from_dict(raw).telemetry == f"{base}/functions/v1/device-telemetry"


@pytest.mark.parametrize("user_id", ["u1", "x" * 5000, 7], ids=["short", "huge", "int"])
def test_poll_confirmed_requires_uuid_user(user_id):
    s = FakeSession()
    s.add("POST", PAIR, 200, {"status": "confirmed", "user_id": user_id, "api_key": KEY, "backend": BACKEND})
    assert run(PairingClient(s, PAIR).async_poll(SESSION)).status == "error"


@pytest.mark.parametrize("expires", ["t", "2026-09-27T10:10:00.000Z" + " " * 40, "", 5],
                         ids=["word", "padded", "empty", "int"])
def test_begin_requires_iso_expiry(expires):
    s = FakeSession()
    s.add("POST", PAIR, 201, {**BEGIN_OK, "expires_at": expires})
    with pytest.raises(PairingError):
        begin(s)


def test_progress_detail_cut_by_utf16_units_like_cloud():
    s = FakeSession()
    s.add("POST", PAIR, 200, {"ok": True})
    run(PairingClient(s, PAIR).async_progress(SESSION, [{"key": "inverter", "state": "done",
                                                          "detail": "🔋" * 150}]))
    detail = s.calls[0]["json"]["steps"][0]["detail"]
    assert len(detail.encode("utf-16-le")) // 2 <= 200 and detail == "🔋" * 100


def test_consumed_choices_keep_only_short_strings():
    s = FakeSession()
    s.add("POST", PAIR, 200, {"status": "consumed", "choices": {
        "control_mode": "entities", "price_source": "x" * 65, "n": 3, "l": ["a"]}})
    assert run(PairingClient(s, PAIR).async_poll(SESSION)).choices == {"control_mode": "entities"}


def test_oversized_body_is_a_failure():
    s = FakeSession()
    s.add("GET", BACKEND["schedule"], 200, {"slots": []}, content_length=10_000_000)
    assert run(VolcastCloud(s, KEY, Backend.from_dict(BACKEND)).async_get_schedule()) is None
    s.add("POST", PAIR, 200, {"status": "consumed", "choices": {}}, content_length=10_000_000)
    assert run(PairingClient(s, PAIR).async_poll(SESSION)).status == "error"
