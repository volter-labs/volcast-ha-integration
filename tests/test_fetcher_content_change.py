import asyncio

from custom_components.volcast.cloud.fetcher import ScheduleFetcher

_BASE = {"from": "2026-09-23T10:00:00Z", "to": "2026-09-23T11:00:00Z"}
SELF = {**_BASE, "mode": "self_consume", "power_w": 800, "soc_target": 60, "grid_export_target_w": 0}
HOLD = {**_BASE, "mode": "hold", "power_w": None, "soc_target": None, "grid_export_target_w": None}


class Cloud:
    def __init__(self, *responses):
        self.responses = list(responses)

    async def async_get_schedule(self):
        return self.responses.pop(0)


def _doc(slot, sid="same"):
    return {"schedule_id": sid, "slots": [slot], "control_enabled": True,
            "fallback": {"mode": "self_consume", "soc_reserve": 10}}


def test_same_id_new_content_calls_on_plan_again_then_dedups():
    plans = []

    async def on_plan(raw, sched):
        plans.append(raw["slots"][0]["mode"])

    async def noop(v):
        pass

    f = ScheduleFetcher(Cloud(_doc(SELF), _doc(HOLD), _doc(HOLD)), on_plan=on_plan,
                        on_consent=noop, on_auth_failure=noop)
    assert asyncio.run(f.async_refresh()) == "accepted"
    assert asyncio.run(f.async_refresh()) == "accepted"
    assert asyncio.run(f.async_refresh()) == "unchanged"
    assert plans == ["self_consume", "hold"]
