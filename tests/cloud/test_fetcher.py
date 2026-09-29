import asyncio
import logging

from custom_components.volcast.cloud.client import CloudAuthError
from custom_components.volcast.cloud.fetcher import ScheduleFetcher

SLOT = {"from": "2026-09-23T10:00:00Z", "to": "2026-09-23T11:00:00Z", "mode": "self_consume"}


class Cloud:
    def __init__(self, *responses):
        self.responses = list(responses)

    async def async_get_schedule(self):
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def make(*responses):
    got = {"plans": [], "consent": [], "auth": []}

    async def on_plan(raw, sched):
        got["plans"].append(raw["schedule_id"])

    async def on_consent(v):
        got["consent"].append(v)

    async def on_auth(n):
        got["auth"].append(n)

    return ScheduleFetcher(Cloud(*responses), on_plan=on_plan, on_consent=on_consent,
                           on_auth_failure=on_auth), got


def doc(sid="a", slots=(SLOT,), control=True, **extra):
    return {"schedule_id": sid, "slots": list(slots), "fallback": {"mode": "self_consume", "soc_reserve": 10},
            "control_enabled": control, **extra}


def test_accept_then_unchanged_then_same_id_new_content():
    f, got = make(doc(), doc(), doc(price_note=1))
    assert asyncio.run(f.async_refresh()) == "accepted"
    assert asyncio.run(f.async_refresh()) == "unchanged"
    assert asyncio.run(f.async_refresh()) == "accepted"
    assert got["plans"] == ["a", "a"]


def test_consent_applied_even_when_plan_rejected():
    f, got = make(doc(slots=[{"mode": "nonsense"}], control=False))
    assert asyncio.run(f.async_refresh()) == "rejected"
    assert got["consent"] == [False] and got["plans"] == []


def test_non_bool_consent_is_ignored():
    f, got = make(doc(control="true"))
    asyncio.run(f.async_refresh())
    assert got["consent"] == []


def test_network_keeps_everything():
    f, got = make(None)
    assert asyncio.run(f.async_refresh()) == "network" and got == {"plans": [], "consent": [], "auth": []}


def test_auth_failures_counted_and_reset():
    f, got = make(CloudAuthError(), CloudAuthError(), doc(), CloudAuthError())
    for _ in range(4):
        asyncio.run(f.async_refresh())
    assert got["auth"] == [1, 2, 1]


def test_empty_plan_with_empty_id_dedup_by_content():
    f, got = make(doc(sid="", slots=[]), doc(sid="", slots=[]))
    assert asyncio.run(f.async_refresh()) == "accepted"
    assert asyncio.run(f.async_refresh()) == "unchanged"


def _failing(stage):
    got = {"plans": [], "consent": []}

    async def on_plan(raw, sched):
        if stage == "plan":
            raise OSError("disk full at /config/.storage")
        got["plans"].append(raw["schedule_id"])

    async def on_consent(v):
        if stage == "consent":
            raise OSError("disk full at /config/.storage")
        got["consent"].append(v)

    async def on_auth(n):
        raise RuntimeError("boom")

    return got, dict(on_plan=on_plan, on_consent=on_consent, on_auth_failure=on_auth)


def test_callback_exceptions_do_not_escape_refresh(caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.volcast.cloud")
    for stage in ("plan", "consent"):
        got, cbs = _failing(stage)
        f = ScheduleFetcher(Cloud(doc()), **cbs)
        assert asyncio.run(f.async_refresh()) == "error"
    got, cbs = _failing(None)
    f = ScheduleFetcher(Cloud(CloudAuthError()), **cbs)
    assert asyncio.run(f.async_refresh()) == "error"
    assert "/config" not in caplog.text and "boom" not in caplog.text


def test_unexpected_client_exception_is_error():
    f, got = make(RecursionError())
    assert asyncio.run(f.async_refresh()) == "error" and got["plans"] == []


def test_failed_plan_callback_is_retried_next_poll():
    calls = []

    async def on_plan(raw, sched):
        calls.append(raw["schedule_id"])
        if len(calls) == 1:
            raise OSError("x")

    async def noop(*_):
        return None

    f = ScheduleFetcher(Cloud(doc(), doc()), on_plan=on_plan, on_consent=noop, on_auth_failure=noop)
    assert asyncio.run(f.async_refresh()) == "error"
    assert asyncio.run(f.async_refresh()) == "accepted" and calls == ["a", "a"]


def test_rejected_plan_warning_logged_once_per_distinct_body(caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.volcast.cloud")
    bad = doc(slots=[{"mode": "nonsense"}])
    bad2 = doc(sid="b", slots=[{"mode": "nonsense"}])
    f, got = make(bad, bad, bad, bad2, doc(), bad)
    results = [asyncio.run(f.async_refresh()) for _ in range(6)]
    assert results == ["rejected", "rejected", "rejected", "rejected", "accepted", "rejected"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "rejected" in r.getMessage()]
    assert len(warnings) == 3                    # bad, bad2, bad po zaakceptowanym planie
    assert got["plans"] == ["a"]
