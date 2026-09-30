import asyncio

from custom_components.volcast.control.signals_hub import SignalsHub

BASE = "https://staging.volcast.app"
TOPIC = "ha-sig:" + "a" * 64


def block(url="wss://staging.volcast.app/socket", live=60, version=1):
    return {
        "version": version,
        "live_for_s": live,
        "channel": {"url": url, "apikey": "k", "topic": TOPIC},
    }


class Channel:
    def __init__(self, err=None):
        self.calls = []
        self.err = err

    async def async_update(self, cfg):
        self.calls.append(cfg)
        if self.err:
            raise self.err


class Live:
    def __init__(self, err=None, coro=False):
        self.calls = []
        self.err = err
        self.coro = coro
        if coro:
            self.update = self._aupdate

    def update(self, n):
        self.calls.append(n)
        if self.err:
            raise self.err

    async def _aupdate(self, n):
        self.calls.append(n)


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def make(clock=None, refresh=None, channel=None, live=None):
    clock = clock or Clock()
    st = {"refresh": 0, "sleeps": [], "gate": None}

    async def default_refresh():
        st["refresh"] += 1

    async def sleep(d):
        st["sleeps"].append(d)
        if st["gate"] is not None:
            await st["gate"].wait()
        else:
            await asyncio.sleep(0)

    hub = SignalsHub(
        base_url=BASE,
        channel=channel or Channel(),
        live=live or Live(),
        refresh=refresh or default_refresh,
        monotonic=clock,
        sleep=sleep,
    )
    return hub, clock, st


def test_apply_valid_block():
    async def go():
        ch, lv = Channel(), Live()
        hub, _, _ = make(channel=ch, live=lv)
        await hub.apply(block())
        assert ch.calls[0].topic == TOPIC and lv.calls == [60]

    asyncio.run(go())


def test_apply_none_and_non_dict_and_v2():
    async def go():
        for raw, in [(None,), ("x",), (block(version=2),)]:
            ch, lv = Channel(), Live()
            hub, _, _ = make(channel=ch, live=lv)
            await hub.apply(raw)
            assert ch.calls == [None] and lv.calls == [0]

    asyncio.run(go())


def test_apply_foreign_host_keeps_live():
    async def go():
        ch, lv = Channel(), Live()
        hub, _, _ = make(channel=ch, live=lv)
        await hub.apply(block(url="wss://evil.example/socket"))
        assert ch.calls == [None] and lv.calls == [60]

    asyncio.run(go())


def test_apply_async_live_supported():
    async def go():
        lv = Live(coro=True)
        hub, _, _ = make(live=lv)
        await hub.apply(block())
        assert lv.calls == [60]

    asyncio.run(go())


def test_apply_never_raises():
    async def go():
        hub, _, _ = make(channel=Channel(err=RuntimeError("x")), live=Live(err=ValueError("y")))
        await hub.apply(block())
        await hub.apply(None)

    asyncio.run(go())


def test_apply_channel_error_still_updates_live():
    async def go():
        lv = Live()
        hub, _, _ = make(channel=Channel(err=RuntimeError("x")), live=lv)
        await hub.apply(block())
        assert lv.calls == [60]

    asyncio.run(go())


def test_burst_one_now_one_delayed():
    async def go():
        hub, clock, st = make()
        for _ in range(10):
            await hub.request_refresh()
            clock.t += 0.1
        assert st["refresh"] == 1
        await asyncio.sleep(0)  # zadanie tła zdąży wywołać sleep
        assert len(st["sleeps"]) == 1 and abs(st["sleeps"][0] - 4.9) < 1e-6
        for _ in range(5):
            await asyncio.sleep(0)
        assert st["refresh"] == 2
        await hub.async_stop()

    asyncio.run(go())


def test_after_window_immediate():
    async def go():
        hub, clock, st = make()
        await hub.request_refresh()
        clock.t += 5.0
        await hub.request_refresh()
        assert st["refresh"] == 2 and st["sleeps"] == []

    asyncio.run(go())


def test_refresh_exception_keeps_debounce():
    async def go():
        n = {"c": 0}

        async def bad():
            n["c"] += 1
            raise RuntimeError("boom")

        hub, clock, st = make(refresh=bad)
        await hub.request_refresh()
        await hub.request_refresh()
        assert n["c"] == 1
        await asyncio.sleep(0)
        assert len(st["sleeps"]) == 1
        for _ in range(5):
            await asyncio.sleep(0)
        assert n["c"] == 2
        clock.t += 6
        await hub.request_refresh()
        assert n["c"] == 3
        await hub.async_stop()

    asyncio.run(go())


def test_no_parallel_refresh_while_running():
    async def go():
        gate = asyncio.Event()
        st2 = {"active": 0, "max": 0, "n": 0}

        async def slow():
            st2["n"] += 1
            st2["active"] += 1
            st2["max"] = max(st2["max"], st2["active"])
            await gate.wait()
            st2["active"] -= 1

        hub, clock, st = make(refresh=slow)
        first = asyncio.ensure_future(hub.request_refresh())
        await asyncio.sleep(0)
        clock.t += 6  # okno minęło, ale pobranie wciąż trwa
        await hub.request_refresh()
        await hub.request_refresh()
        await asyncio.sleep(0)
        assert st2["n"] == 1 and st2["max"] == 1
        gate.set()
        await first
        for _ in range(10):
            await asyncio.sleep(0)
        assert st2["n"] == 2 and st2["max"] == 1
        await hub.async_stop()

    asyncio.run(go())


def test_stop_cancels_pending():
    async def go():
        hub, _, st = make()
        st["gate"] = asyncio.Event()
        await hub.request_refresh()
        await hub.request_refresh()
        await asyncio.sleep(0)
        await hub.async_stop()
        st["gate"].set()
        for _ in range(5):
            await asyncio.sleep(0)
        assert st["refresh"] == 1

    asyncio.run(go())
