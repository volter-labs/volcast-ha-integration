import asyncio

from custom_components.volcast.cloud.client import PollResult

from .test_onboarding import OK_PLAN, last, make


def test_remote_plan_only_choice_uses_runtime_path_handler():
    ob, client, entry = make([PollResult("consumed", choices={"control_mode": "plan_only"})], plan=OK_PLAN)
    calls = []

    async def handler(path):
        calls.append(path)
        return "applied"

    ob._runtime().async_apply_path_choice = handler
    asyncio.run(ob.async_run())
    assert calls == ["plan_only"]
    assert last(client)["control_mode"]["state"] == "done" and last(client)["control_mode"]["detail"] == "plan_only"
    assert "control_mode" not in entry.options
