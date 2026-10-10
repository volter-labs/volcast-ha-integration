import asyncio

from .test_control_choice import AT1, AT2, AT3, real_rt, run


def test_path_applied_by_newer_path_at_even_for_the_same_value():
    rt = real_rt()
    calls = []

    async def path(v):
        calls.append(v)
        return "applied"

    rt.async_apply_path_choice = path
    run(rt, {"path": "plan_only", "path_at": AT1, "at": AT1})
    run(rt, {"path": "plan_only", "path_at": AT1, "at": AT2})      # ten sam znacznik: nic
    run(rt, {"path": "plan_only", "path_at": AT2, "at": AT2})      # ten sam wybór, nowszy znacznik
    assert calls == ["plan_only", "plan_only"]
    assert rt.executor.control_meta["path_at"] == AT2


def test_older_field_timestamp_is_ignored_and_fields_are_independent():
    rt = real_rt()
    run(rt, {"controller": "own_ems", "controller_at": AT2, "verification": "abort", "verification_at": AT3,
             "at": AT3})
    assert rt.executor.plan_only is True and rt.verification.calls.count("abort") == 1
    run(rt, {"controller": "volcast", "controller_at": AT1, "verification": "abort", "verification_at": AT3,
             "at": AT3})
    assert rt.executor.plan_only is True and rt.verification.calls.count("abort") == 1


def test_verification_falls_back_to_at_without_verification_at():
    rt = real_rt()
    run(rt, {"verification": "retry", "at": AT1})
    run(rt, {"verification": "retry", "at": AT1})
    run(rt, {"verification": "retry", "at": AT2})
    assert rt.verification.calls == ["retry", "retry"]


def test_repair_uses_dedicated_issue_id(monkeypatch):
    from custom_components.volcast.control import runtime as rt_mod

    created = []
    monkeypatch.setattr(rt_mod.ir, "async_create_issue", lambda hass, domain, issue_id, **kw: created.append(
        (issue_id, kw["translation_key"])), raising=False)
    rt = real_rt()
    rt.report_choice_error = rt_mod.ControlRuntime.report_choice_error.__get__(rt)
    t = [0.0]
    rt.choice_clock = lambda: t[0]

    async def failed(_c):
        return "restore_failed"

    rt.async_apply_controller_choice = failed
    for _ in range(3):
        run(rt, {"controller": "own_ems", "at": AT1})
        t[0] += 300.0
    assert created == [("control_choice_failed_e1", "control_choice_failed")]


def test_pings_between_pulls_do_not_spend_attempts():
    rt = real_rt()
    n = []

    async def failed(_c):
        n.append(1)
        return "restore_failed"

    rt.async_apply_controller_choice = failed
    t = [0.0]
    rt.choice_clock = lambda: t[0]
    for _ in range(6):                      # seria pingów w ciągu kilku sekund
        run(rt, {"controller": "own_ems", "at": AT1})
        t[0] += 2.0
    assert len(n) == 1 and rt.executor.control_meta["tries"] == {"controller:own_ems": 1}
    t[0] += 300.0
    run(rt, {"controller": "own_ems", "at": AT1})
    assert len(n) == 2
