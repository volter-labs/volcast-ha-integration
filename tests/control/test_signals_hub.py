import asyncio

import pytest

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


async def settle(n=20):
    for _ in range(n):
        await asyncio.sleep(0)


def make(clock=None, refresh=None, channel=None, live=None, **kw):
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
        **kw,
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
        await hub.request_refresh()
        assert st["refresh"] == 0                 # tylko zaplanowane — nie w zadaniu wołającego
        await asyncio.sleep(0)
        assert st["refresh"] == 1
        for _ in range(9):
            clock.t += 0.1
            await hub.request_refresh()
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
        await settle()
        clock.t += 5.0
        await hub.request_refresh()
        await settle()
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
        await asyncio.sleep(0)
        await hub.request_refresh()
        assert n["c"] == 1
        await asyncio.sleep(0)
        assert len(st["sleeps"]) == 1
        for _ in range(5):
            await asyncio.sleep(0)
        assert n["c"] == 2
        clock.t += 6
        await hub.request_refresh()
        await settle()
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
        await settle()                  # pobranie ruszyło i wisi na bramce
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
        await asyncio.sleep(0)
        await hub.request_refresh()
        await asyncio.sleep(0)
        await hub.async_stop()
        st["gate"].set()
        for _ in range(5):
            await asyncio.sleep(0)
        assert st["refresh"] == 1

    asyncio.run(go())


# ── pobranie zawsze w zadaniu huba (nie kanału) ─────────────────────────

def test_refresh_runs_in_hub_task_not_in_callers_task():
    async def go():
        seen = []

        async def refresh():
            seen.append(asyncio.current_task())

        hub, _, _ = make(refresh=refresh)
        await hub.request_refresh()
        await settle()
        assert len(seen) == 1 and seen[0] is not asyncio.current_task()
        await hub.async_stop()

    asyncio.run(go())


def test_channel_change_from_inside_refresh_does_not_cancel_the_refresh():
    """Ping → pobranie → nowy blok z innym tematem: kanał się przełącza, a reszta pobrania
    (zgoda, plan, cykl wykonawcy) dobiega końca."""
    from custom_components.volcast.cloud.signal_channel import SignalChannel
    from tests.cloud.test_signal_channel import HOLD, Clock as WsClock, FakeWs, FakeWsSession

    topic2 = "ha-sig:" + "b" * 64

    def join_ok(topic):
        import json
        return json.dumps({"topic": "realtime:" + topic, "event": "phx_reply", "ref": "1",
                           "payload": {"status": "ok", "response": {}}})

    async def go():
        wclock = WsClock()
        s = FakeWsSession(FakeWs(wclock, join_ok(TOPIC), HOLD), FakeWs(wclock, join_ok(topic2), HOLD))
        holder = {}
        st2 = {"n": 0, "done": 0}

        async def wake():
            await holder["hub"].request_refresh()

        ch = SignalChannel(s, on_wake=wake, sleep=wclock.sleep, rand=lambda: 0.5, monotonic=wclock.monotonic)

        async def refresh():
            st2["n"] += 1
            if st2["n"] == 1:
                b = block()
                b["channel"]["topic"] = topic2
                await holder["hub"].apply(b)           # fetcher → on_signals
            await asyncio.sleep(0)                     # dalsza część: zgoda, plan, cykl
            st2["done"] += 1

        clock = Clock()
        hub, _, _ = make(clock=clock, refresh=refresh, channel=ch)
        holder["hub"] = hub
        await hub.apply(block())
        for _ in range(200):
            if st2["done"] and len(s.connects) == 2 and ch.connected:
                break
            await asyncio.sleep(0)
        assert st2["done"] == st2["n"] >= 1
        assert len(s.connects) == 2 and ch.connected
        await hub.async_stop()
        await ch.async_stop()

    asyncio.run(go())


def test_stop_waits_for_refresh_in_progress_and_does_not_cancel_it():
    async def go():
        gate = asyncio.Event()
        st2 = {"started": 0, "finished": 0}

        async def refresh():                            # zapis wykonawcy w toku
            st2["started"] += 1
            await gate.wait()
            st2["finished"] += 1

        hub, _, _ = make(refresh=refresh)
        await hub.request_refresh()
        await settle()
        assert st2["started"] == 1
        stop = asyncio.ensure_future(hub.async_stop())
        await settle()
        assert not stop.done()                          # rozładunek czeka na zapis
        gate.set()
        await asyncio.wait_for(stop, timeout=1)
        assert st2["finished"] == 1

    asyncio.run(go())


def test_stop_gives_up_waiting_after_limit_without_cancelling():
    async def go():
        gate = asyncio.Event()
        st2 = {"finished": 0, "cancelled": 0}

        async def refresh():
            try:
                await gate.wait()
            except asyncio.CancelledError:
                st2["cancelled"] += 1
                raise
            st2["finished"] += 1

        hub, _, _ = make(refresh=refresh, stop_timeout_s=0.01)
        await hub.request_refresh()
        await settle()
        await asyncio.wait_for(hub.async_stop(), timeout=1)
        gate.set()
        await settle()
        assert st2 == {"finished": 1, "cancelled": 0}

    asyncio.run(go())


def test_stop_cancels_waiting_refresh_and_blocks_new_ones():
    async def go():
        hub, clock, st = make()
        await hub.request_refresh()
        await settle()
        st["gate"] = asyncio.Event()
        await hub.request_refresh()                     # w oknie → czeka w sleep
        await settle()
        await hub.async_stop()
        await hub.request_refresh()
        clock.t += 10
        st["gate"].set()
        await settle()
        assert st["refresh"] == 1

    asyncio.run(go())


def test_apply_after_stop_does_not_revive_channel_or_live():
    async def go():
        ch, lv = Channel(), Live()
        hub, _, _ = make(channel=ch, live=lv)
        await hub.async_stop()
        await hub.apply(block())
        assert ch.calls == [] and lv.calls == []

    asyncio.run(go())


def test_freeze_blocks_refreshes_synchronously_but_keeps_one_in_progress():
    async def go():
        gate = asyncio.Event()
        st2 = {"n": 0, "finished": 0}

        async def refresh():
            st2["n"] += 1
            await gate.wait()
            st2["finished"] += 1

        clock = Clock()
        hub, _, _ = make(clock=clock, refresh=refresh)
        await hub.request_refresh()
        await settle()
        clock.t += 10
        await hub.request_refresh()                     # czeka na blokadę
        await settle()
        hub.freeze()                                    # synchronicznie, bez await
        await hub.request_refresh()
        gate.set()
        await settle()
        assert st2 == {"n": 1, "finished": 1}
        await hub.async_stop()

    asyncio.run(go())


def test_eager_task_factory_and_task_cancelled_before_start_do_not_wedge_refreshes():
    async def go():
        n = {"c": 0}

        async def refresh():
            n["c"] += 1

        loop = asyncio.get_running_loop()
        hub, clock, _ = make(refresh=refresh,
                             task_factory=lambda coro, name: asyncio.eager_task_factory(loop, coro, name=name))
        await hub.request_refresh()
        assert n["c"] == 1                              # gorliwie, ale w zadaniu huba
        clock.t += 10
        await hub.request_refresh()
        await settle()
        assert n["c"] == 2

        # zadanie anulowane przed startem (korutyna zamknięta bez `finally`)
        def closing_factory(coro, name):
            coro.close()
            fut = loop.create_future()
            fut.cancel()
            return fut
        hub2, clock2, _ = make(refresh=refresh, task_factory=closing_factory)
        await hub2.request_refresh()
        hub2._task_factory = lambda coro, name: loop.create_task(coro, name=name)
        clock2.t += 10
        await hub2.request_refresh()
        await settle()
        assert n["c"] == 3
        await hub.async_stop()
        await hub2.async_stop()

    asyncio.run(go())


# --- symulacja w czasie wirtualnym: sen i pobrania trwają, aż test przesunie zegar ---

def make_sim(refresh):
    clock = Clock()

    async def sleep(d):
        end = clock.t + d
        while clock.t < end - 1e-9:
            await asyncio.sleep(0)

    hub = SignalsHub(base_url=BASE, channel=Channel(), live=Live(), refresh=refresh,
                     monotonic=clock, sleep=sleep)
    return hub, clock


async def until(clock, t):
    """Przesuwa zegar krokami po 0,5 s aż do `t`, oddając pętli sterowanie po każdym kroku."""
    while clock.t < t - 1e-9:
        clock.t += 0.5
        await settle()


def test_pings_during_slow_fetch_coalesce_into_one_more_fetch():
    async def go():
        gate = asyncio.Event()
        starts = []
        clock_ref = {}

        async def refresh():
            starts.append(clock_ref["c"].t)
            if len(starts) == 1:
                await gate.wait()                   # pierwsze pobranie trwa 12 s

        hub, clock = make_sim(refresh)
        clock_ref["c"] = clock
        await hub.request_refresh()
        await settle()
        assert starts == [100.0]
        max_tasks = 0
        for _ in range(20):                         # 20 pingów rozłożonych na 12 s, z oddaniem pętli
            clock.t += 0.6
            await hub.request_refresh()
            await settle()
            max_tasks = max(max_tasks, len(hub._tasks))
        gate.set()
        await until(clock, clock.t + 10)
        assert len(starts) == 2                     # jedno dodatkowe pobranie, nie 20
        assert starts[1] - starts[0] >= 5.0
        assert max_tasks <= 2                       # pobranie w toku + najwyżej jedno czekające
        await hub.async_stop()

    asyncio.run(go())


def test_fetch_starts_are_at_least_one_window_apart_under_continuous_pings():
    async def go():
        starts = []
        clock_ref = {}

        async def refresh():
            c = clock_ref["c"]
            starts.append(c.t)
            end = c.t + (8.0 if len(starts) % 3 == 1 else 0.5)   # przeplot długich i krótkich pobrań
            while c.t < end - 1e-9:
                await asyncio.sleep(0)

        hub, clock = make_sim(refresh)
        clock_ref["c"] = clock
        for _ in range(80):                         # ping co 0,5 s przez 40 s
            await hub.request_refresh()
            clock.t += 0.5
            await settle()
        await until(clock, clock.t + 20)
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        assert gaps and min(gaps) >= 5.0 - 1e-9
        assert starts[-1] <= 100.0 + 40.0 + 8.0 + 1e-9   # po ostatnim pingu co najwyżej jedno pobranie
        await hub.async_stop()

    asyncio.run(go())


def test_two_pings_early_in_a_fetch_give_exactly_one_more_fetch():
    async def go():
        gate = asyncio.Event()
        starts = []
        clock_ref = {}

        async def refresh():
            starts.append(clock_ref["c"].t)
            if len(starts) == 1:
                await gate.wait()

        hub, clock = make_sim(refresh)
        clock_ref["c"] = clock
        await hub.request_refresh()
        await settle()
        await until(clock, 101.0)
        await hub.request_refresh()
        await settle()
        await until(clock, 102.0)
        await hub.request_refresh()
        await settle()
        await until(clock, 103.0)
        gate.set()
        await until(clock, 115.0)
        assert starts == [100.0, 105.0]
        await hub.async_stop()

    asyncio.run(go())


def test_task_factory_error_does_not_leave_hub_waiting():
    async def go():
        n = {"c": 0}

        async def refresh():
            n["c"] += 1

        def broken(coro, name):
            coro.close()
            raise RuntimeError("no loop")

        loop = asyncio.get_running_loop()
        hub, _, _ = make(refresh=refresh, task_factory=broken)
        with pytest.raises(RuntimeError):
            await hub.request_refresh()
        hub._task_factory = lambda coro, name: loop.create_task(coro, name=name)
        await hub.request_refresh()
        await settle()
        assert n["c"] == 1
        await hub.async_stop()

    asyncio.run(go())


def test_apply_in_flight_at_stop_does_not_start_live():
    async def go():
        gate = asyncio.Event()

        class SlowChannel(Channel):
            async def async_update(self, cfg):
                self.calls.append(cfg)
                await gate.wait()                   # np. czekanie na zamknięcie starego gniazda

        ch, lv = SlowChannel(), Live()
        hub, _, _ = make(channel=ch, live=lv)
        pending = asyncio.ensure_future(hub.apply(block()))   # np. odpowiedź telemetrii — nie śledzona
        await settle()
        await hub.async_stop()
        gate.set()
        await pending
        assert lv.calls == []                       # spóźnione apply nie wznawia nadawania live
        await hub.apply(block())
        assert len(ch.calls) == 1 and lv.calls == []

    asyncio.run(go())
