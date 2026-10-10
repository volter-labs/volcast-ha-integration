import asyncio
from types import SimpleNamespace

from custom_components.volcast.cloud import control_choice
from custom_components.volcast.control import runtime as rt_mod
from custom_components.volcast.control.runtime import ControlRuntime

AT1, AT2, AT3 = "2026-10-10T07:58:00Z", "2026-10-10T08:30:00Z", "2026-10-10T09:00:00Z"


class Exec:
    def __init__(self):
        self.control_meta = {}
        self.saved = 0
        self.plan_only = False
        self.owned = False
        self.paused = False

    async def async_save_control_meta(self):
        self.saved += 1

    async def async_set_plan_only(self, on):
        self.plan_only = on
        return True

    async def async_restore_now(self):
        self.owned = False


class Ver:
    def __init__(self):
        self.calls = []

    async def async_abort(self):
        self.calls.append("abort")

    async def async_retry(self):
        self.calls.append("retry")

    async def async_park(self):
        self.calls.append("park")

    async def async_restart(self):
        self.calls.append("restart")


class Conflicts:
    def __init__(self):
        self.calls = []

    async def async_refresh(self):
        self.calls.append("refresh")

    async def async_acknowledge(self):
        self.calls.append("ack")

    async def async_set_box_active(self, active):
        self.calls.append(("box", active))


def real_rt(options=None):
    entry = SimpleNamespace(entry_id="e1", options=dict(options or {}))
    hass = SimpleNamespace(config_entries=SimpleNamespace(
        async_update_entry=lambda e, **kw: [setattr(e, k, v) for k, v in kw.items()]))
    rt = ControlRuntime(executor=Exec(), fetcher=None, telemetry=None, cloud=None, choice=None, mapped={},
                        rated_power_w=None, hass=hass, entry=entry)
    rt.verification, rt.conflicts = Ver(), Conflicts()
    rt.notify_control_state = lambda: rt.__dict__.setdefault("pings", []).append(1)
    rt.report_choice_error = lambda: rt.__dict__.setdefault("errors", []).append(1)
    return rt


def run(rt, block):
    asyncio.run(control_choice.apply(block, rt))


def test_sticky_unapplicable_path_does_not_block_abort_or_controller():
    # `direct` po przeładowaniu: brak sondy → "ignored"; kolejne decyzje i tak działają.
    rt = real_rt()
    run(rt, {"path": "direct", "verification": "abort", "at": AT1})
    assert rt.verification.calls == ["abort"]
    assert rt.executor.control_meta["path_done"] == "direct" and "path" not in (rt.executor.control_meta["ack"])
    run(rt, {"path": "direct", "controller": "own_ems", "at": AT2})
    assert rt.executor.plan_only is True
    assert rt.executor.control_meta["ack"] == {"controller": "own_ems", "at": AT2}
    run(rt, {"path": "direct", "controller": "own_ems", "verification": "abort", "at": AT3})
    assert rt.verification.calls.count("abort") == 2


def test_sticky_controller_does_not_reacknowledge_on_retry():
    rt = real_rt()
    run(rt, {"controller": "volcast", "at": AT1})
    assert rt.conflicts.calls == ["ack"]
    run(rt, {"controller": "volcast", "verification": "retry", "at": AT2})
    assert rt.conflicts.calls == ["ack"] and rt.verification.calls == ["retry"]


def test_verification_once_per_at_and_older_ignored():
    rt = real_rt()
    run(rt, {"verification": "abort", "at": AT2})
    run(rt, {"verification": "abort", "at": AT2})
    run(rt, {"verification": "abort", "at": AT1})
    assert rt.verification.calls == ["abort"]
    run(rt, {"verification": "abort", "at": AT3})
    assert rt.verification.calls == ["abort", "abort"]


def test_exception_does_not_ack_and_is_retried_at_most_three_times():
    rt = real_rt()

    async def boom(_c):
        raise RuntimeError("x")

    rt.async_apply_controller_choice = boom
    t = [0.0]
    rt.choice_clock = lambda: t[0]
    for _ in range(5):
        run(rt, {"controller": "own_ems", "at": AT1})
        t[0] += 300.0
    meta = rt.executor.control_meta
    assert "ack" not in meta or "controller" not in meta["ack"]
    assert meta["controller_done"] == "own_ems" and rt.errors == [1]
    assert "tries" not in meta


def test_failure_below_the_bound_is_not_recorded():
    rt = real_rt()

    async def failed(_c):
        return "restore_failed"

    rt.async_apply_controller_choice = failed
    t = [0.0]
    rt.choice_clock = lambda: t[0]
    run(rt, {"controller": "own_ems", "at": AT1})
    t[0] += 300.0
    run(rt, {"controller": "own_ems", "at": AT1})
    meta = rt.executor.control_meta
    assert "controller_done" not in meta and meta["tries"] == {"controller:own_ems": 2}
    assert not getattr(rt, "errors", None) and not getattr(rt, "pings", None)


def test_refused_path_change_is_restore_failed(monkeypatch):
    rt = real_rt()
    rt.choice = SimpleNamespace(profile=SimpleNamespace(id="p"), integration_domain="goodwe")
    monkeypatch.setattr(rt_mod, "entity_mode_ready", lambda c, m: True)

    async def refuse(runtime, old, new):
        return False

    monkeypatch.setattr(rt_mod, "async_control_change_allowed", refuse)
    rt.executor.plan_only = True
    assert asyncio.run(rt.async_apply_path_choice("entities")) == "restore_failed"
    assert rt.executor.plan_only is True and rt.entry.options == {}


def test_allowed_entities_path_clears_plan_only_and_sets_options(monkeypatch):
    rt = real_rt()
    rt.choice = SimpleNamespace(profile=SimpleNamespace(id="p"), integration_domain="goodwe")
    monkeypatch.setattr(rt_mod, "entity_mode_ready", lambda c, m: True)

    async def allow(runtime, old, new):
        return True

    monkeypatch.setattr(rt_mod, "async_control_change_allowed", allow)
    rt.executor.plan_only = True
    assert asyncio.run(rt.async_apply_path_choice("entities")) == "applied"
    assert rt.executor.plan_only is False and rt.entry.options["control_mode"] == "entities"


def test_direct_without_probe_is_ignored_and_plan_only_maps_to_own_ems():
    rt = real_rt()
    assert asyncio.run(rt.async_apply_path_choice("direct")) == "ignored"
    assert asyncio.run(rt.async_apply_path_choice("plan_only")) == "applied"
    assert rt.executor.plan_only is True


def test_box_active_every_pull_and_none_block_noop():
    rt = real_rt()
    run(rt, {"box_active": True})
    run(rt, {"box_active": True})
    run(rt, None)
    assert rt.conflicts.calls == [("box", True), ("box", True)]
