import asyncio

from custom_components.volcast.cloud.fetcher import ScheduleFetcher

from .test_fetcher import Cloud, doc

AT1, AT2 = "2026-10-10T07:58:00Z", "2026-10-10T08:30:00Z"


def make(*responses, on_control=None):
    got = {"plans": [], "control": []}

    async def on_plan(raw, sched):
        got["plans"].append(raw)

    async def noop(*a):
        return None

    async def default_control(block):
        got["control"].append(block)

    f = ScheduleFetcher(Cloud(*responses), on_plan=on_plan, on_consent=noop, on_auth_failure=noop,
                        on_control=on_control or default_control)
    return f, got


def test_control_block_does_not_change_signature_and_is_not_stored():
    f, got = make(doc(), doc() | {"control": {"box_active": True}},
                  doc() | {"control": {"path": "plan_only", "at": AT1}})
    assert asyncio.run(f.async_refresh()) == "accepted"
    assert asyncio.run(f.async_refresh()) == "unchanged"
    assert asyncio.run(f.async_refresh()) == "unchanged"
    assert len(got["plans"]) == 1 and "control" not in got["plans"][0]
    assert got["control"] == [None, {"box_active": True}, {"path": "plan_only", "at": AT1}]


def test_plan_without_control_key_behaves_as_before():
    f, got = make(doc())
    assert asyncio.run(f.async_refresh()) == "accepted" and got["control"] == [None]


def test_control_applied_even_when_plan_rejected():
    f, got = make(doc(slots=[{"mode": "nonsense"}]) | {"control": {"box_active": False}})
    assert asyncio.run(f.async_refresh()) == "rejected"
    assert got["control"] == [{"box_active": False}]


def test_control_callback_failure_does_not_stop_the_plan():
    async def boom(block):
        raise RuntimeError("x")

    f, got = make(doc() | {"control": {"box_active": True}}, on_control=boom)
    assert asyncio.run(f.async_refresh()) == "accepted"
