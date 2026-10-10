import asyncio
from types import SimpleNamespace

from custom_components.volcast.control.runtime import ControlRuntime, FlushLimiter


def test_flush_limiter_coalesces_within_window():
    async def go():
        loop = asyncio.get_running_loop()
        flushed, tasks = [], []

        async def flush():
            flushed.append(1)

        hass = SimpleNamespace(loop=loop, async_create_background_task=lambda coro, name: tasks.append(
            loop.create_task(coro)))
        t = [100.0]
        lim = FlushLimiter(hass, flush, interval_s=10.0, clock=lambda: t[0])
        lim.request()
        lim.request()                      # zlane z pierwszym
        await asyncio.gather(*tasks)
        assert flushed == [1]
        t[0] = 103.0
        lim.request()
        lim.request()
        assert len(tasks) == 1 and lim._timer is not None     # czeka do końca okna
        lim.cancel()
        assert lim._timer is None

    asyncio.run(go())


def test_runtime_control_block_bumps_seq_only_on_change_and_persists():
    saved = []

    class Ex:
        def __init__(self):
            self.control_meta = {}

        async def async_save_control_meta(self):
            saved.append(dict(self.control_meta))

    ex = Ex()
    rt = ControlRuntime(executor=ex, fetcher=None, telemetry=None, cloud=None, choice=None, mapped={},
                        rated_power_w=None)
    a = rt.control_block()
    b = rt.control_block()
    assert a["seq"] == b["seq"] and a["conflicts"] == []
    asyncio.run(rt.async_persist_control_meta())
    asyncio.run(rt.async_persist_control_meta())               # bez zmiany: bez drugiego zapisu
    assert len(saved) == 1 and saved[0]["seq"] == a["seq"]
    ex.control_meta["ack"] = {"path": "plan_only", "at": "2026-10-10T08:00:00Z"}
    c = rt.control_block()
    assert c["seq"] > a["seq"] and c["choice_ack"]["path"] == "plan_only"
