import asyncio
from types import SimpleNamespace

from custom_components.volcast.cloud import control_choice
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


# ── cloud/control_choice.apply ──

class Exec:
    def __init__(self):
        self.control_meta = {}
        self.saved = 0

    async def async_save_control_meta(self):
        self.saved += 1


class Rt:
    def __init__(self, controller_result="applied"):
        self.executor = Exec()
        self.calls = []
        self.sent = 0
        self.controller_result = controller_result
        self.verification = SimpleNamespace(async_abort=self._rec("abort"), async_retry=self._rec("retry"))

    def _rec(self, name):
        async def f():
            self.calls.append(name)
        return f

    async def async_set_box_active(self, active):
        self.calls.append(("box", active))

    async def async_apply_path_choice(self, path):
        self.calls.append(("path", path))
        return "applied"

    async def async_apply_controller_choice(self, controller):
        self.calls.append(("controller", controller))
        return self.controller_result

    def notify_control_state(self):
        self.sent += 1


def run(rt, block):
    asyncio.run(control_choice.apply(block, rt))


def test_box_active_only():
    rt = Rt()
    run(rt, {"box_active": True})
    assert rt.calls == [("box", True)] and rt.executor.control_meta == {}


def test_none_block_is_noop():
    rt = Rt()
    run(rt, None)
    assert rt.calls == []


def test_decision_applied_then_acked():
    rt = Rt()
    run(rt, {"path": "entities", "controller": "volcast", "verification": "retry", "at": AT1,
             "box_active": False})
    assert rt.calls == [("box", False), ("path", "entities"), ("controller", "volcast"), "retry"]
    assert rt.executor.control_meta["at"] == AT1
    assert rt.executor.control_meta["ack"] == {"path": "entities", "controller": "volcast", "at": AT1}
    assert rt.sent == 1 and rt.executor.saved >= 1


def test_older_or_equal_at_ignored_and_verification_once_per_at():
    rt = Rt()
    run(rt, {"controller": "volcast", "verification": "abort", "at": AT2})
    rt.calls.clear()
    run(rt, {"controller": "volcast", "verification": "abort", "at": AT2})     # ten sam at
    run(rt, {"path": "plan_only", "at": AT1})                                   # starszy at
    assert rt.calls == [] and rt.executor.control_meta["at"] == AT2


def test_newer_at_applies_again():
    rt = Rt()
    run(rt, {"verification": "abort", "at": AT1})
    run(rt, {"verification": "abort", "at": AT2})
    assert rt.calls.count("abort") == 2


def test_invalid_values_and_missing_at_ignored():
    rt = Rt()
    run(rt, {"path": "nonsense", "controller": "x", "verification": "y", "at": AT1})
    run(rt, {"path": "plan_only"})
    run(rt, {"path": "plan_only", "at": "yesterday"})
    assert rt.calls == [] and rt.executor.control_meta == {}


def test_failed_restore_is_retried_next_pull():
    rt = Rt(controller_result="restore_failed")
    run(rt, {"controller": "own_ems", "at": AT1})
    assert rt.executor.control_meta.get("at") is None and rt.executor.control_meta.get("ack") is None
    rt.controller_result = "applied"
    run(rt, {"controller": "own_ems", "at": AT1})
    assert rt.executor.control_meta["at"] == AT1
