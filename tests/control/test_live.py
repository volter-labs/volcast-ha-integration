import asyncio
import logging

from custom_components.volcast.cloud.client import TelemetryResult
from custom_components.volcast.cloud.signals import LIVE_MAX_S
from custom_components.volcast.control.live import LIVE_INTERVAL_S, LiveSender

LOGGER = "custom_components.volcast.control"
READING = {"timestamp": "2026-09-23T10:00:00+00:00", "live": True, "pv_power_w": 1200.0}


class Clock:
    """Zegar monotoniczny przesuwany przez atrapę `sleep`."""

    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def mono(self):
        return self.t

    async def sleep(self, s):
        self.sleeps.append(s)
        self.t += s
        await asyncio.sleep(0)


class Cloud:
    """Odpowiada kolejnymi statusami (ostatni się powtarza); wyjątek = rzuć."""

    def __init__(self, statuses=(200,), signals=None):
        self.statuses, self.signals = list(statuses), signals
        self.sent = []

    async def async_post_telemetry(self, reading, *, persist=True):
        self.sent.append((reading, persist))
        st = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if isinstance(st, BaseException):
            raise st
        return TelemetryResult(st, self.signals if st == 200 else None)


class Telemetry:
    def __init__(self, readings=None):
        self.readings = readings

    def build_live_reading(self):
        if self.readings is None:
            return dict(READING)
        return self.readings.pop(0) if len(self.readings) > 1 else self.readings[0]


def make(cloud=None, telemetry=None, on_signals=None, clock=None):
    clock = clock or Clock()
    got = []

    async def default_on_signals(raw):
        got.append(raw)
    live = LiveSender(cloud=cloud or Cloud(), telemetry=telemetry or Telemetry(),
                      on_signals=on_signals or default_on_signals, monotonic=clock.mono, sleep=clock.sleep)
    return live, clock, got


async def _finish(live):
    """Czeka, aż pętla skończy się sama (bez anulowania)."""
    for _ in range(1000):
        if not live.running:
            return
        await asyncio.sleep(0)
    raise AssertionError("pętla nie skończyła się")


def test_window_of_nine_seconds_sends_three_non_persistent_posts():
    cloud = Cloud()
    live, clock, _ = make(cloud)

    async def go():
        live.update(9)
        assert live.running
        await _finish(live)
    asyncio.run(go())
    assert LIVE_INTERVAL_S == 3.0
    assert [p for _, p in cloud.sent] == [False, False, False]
    assert all(r["live"] is True for r, _ in cloud.sent)
    assert not live.running


def test_409_ends_loop_immediately():
    cloud = Cloud(statuses=(200, 409, 200))
    live, _, _ = make(cloud)

    async def go():
        live.update(60)
        await _finish(live)
    asyncio.run(go())
    assert len(cloud.sent) == 2 and not live.running


def test_401_ends_loop():
    cloud = Cloud(statuses=(401,))
    live, _, _ = make(cloud)

    async def go():
        live.update(60)
        await _finish(live)
    asyncio.run(go())
    assert len(cloud.sent) == 1


def test_status_zero_and_5xx_do_not_end_loop():
    cloud = Cloud(statuses=(0, 503, 0))
    live, _, got = make(cloud)

    async def go():
        live.update(12)
        await _finish(live)
    asyncio.run(go())
    assert len(cloud.sent) == 4 and got == []          # bez odpowiedzi 200 nie ma sygnałów


def test_200_passes_signals_to_callback():
    sig = {"version": 1, "live_for_s": 60}
    live, _, got = make(Cloud(signals=sig))

    async def go():
        live.update(3)
        await _finish(live)
    asyncio.run(go())
    assert got == [sig]


def test_update_zero_from_inside_loop_stops_without_self_cancel():
    cloud = Cloud()
    holder = {}

    async def on_signals(raw):
        holder["live"].update(0)                       # hub.apply → live.update(0) w pętli
    live, _, _ = make(cloud, on_signals=on_signals)
    holder["live"] = live

    async def go():
        live.update(60)
        task = live._task
        await _finish(live)
        return task
    task = asyncio.run(go())
    assert len(cloud.sent) == 1
    assert task.done() and not task.cancelled() and task.exception() is None


def test_response_extends_deadline():
    cloud = Cloud()
    holder = {"n": 0}

    async def on_signals(raw):
        holder["n"] += 1
        if holder["n"] == 1:
            holder["live"].update(30)                  # chmura przedłuża okno do 30 s od teraz
    live, clock, _ = make(cloud, on_signals=on_signals)
    holder["live"] = live

    async def go():
        live.update(3)
        await _finish(live)
    asyncio.run(go())
    assert len(cloud.sent) == 10 and clock.t == 30.0


def test_deadline_capped_at_live_max():
    live, clock, _ = make(Cloud())

    async def go():
        clock.t = 100.0
        live.update(10 ** 6)
        assert live._deadline == 100.0 + LIVE_MAX_S
        await live.async_stop()
    asyncio.run(go())


def test_capped_window_sends_at_most_max_over_interval():
    cloud = Cloud()
    live, clock, _ = make(cloud)

    async def go():
        live.update(10 ** 6)
        await _finish(live)
    asyncio.run(go())
    assert len(cloud.sent) == LIVE_MAX_S / LIVE_INTERVAL_S and clock.t == LIVE_MAX_S


def test_no_reading_means_no_post_but_loop_lives():
    cloud = Cloud()
    live, clock, _ = make(cloud, telemetry=Telemetry(readings=[None, None, READING]))

    async def go():
        live.update(9)
        await _finish(live)
    asyncio.run(go())
    assert len(cloud.sent) == 1 and len(clock.sleeps) == 3


def test_cloud_exception_does_not_end_loop(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    cloud = Cloud(statuses=(OSError("http://10.0.0.1 refused"), 200))
    live, _, got = make(cloud)

    async def go():
        live.update(6)
        await _finish(live)
    asyncio.run(go())
    assert len(cloud.sent) == 2 and len(got) == 1
    assert "10.0.0.1" not in caplog.text and "OSError" in caplog.text


def test_callback_and_reading_exceptions_do_not_end_loop():
    class Broken(Telemetry):
        def __init__(self):
            super().__init__()
            self.n = 0

        def build_live_reading(self):
            self.n += 1
            if self.n == 1:
                raise RuntimeError("x")
            return super().build_live_reading()

    async def on_signals(raw):
        raise ValueError("y")
    cloud = Cloud()
    live, _, _ = make(cloud, telemetry=Broken(), on_signals=on_signals)

    async def go():
        live.update(9)
        await _finish(live)
    asyncio.run(go())
    assert len(cloud.sent) == 2


def test_update_zero_stops_running_loop():
    async def go():
        clock = Clock()
        blocker = asyncio.Event()

        async def slow_sleep(s):
            await blocker.wait()
        live = LiveSender(cloud=Cloud(), telemetry=Telemetry(), on_signals=_noop,
                          monotonic=clock.mono, sleep=slow_sleep)
        live.update(60)
        task = live._task
        for _ in range(5):
            await asyncio.sleep(0)
        assert live.running
        live.update(0)
        for _ in range(3):
            await asyncio.sleep(0)
        assert not live.running and task.done()
        live.update(-5)                                # powtórne gaszenie bez skutków
    asyncio.run(go())


def test_update_when_running_only_moves_deadline():
    created = []

    def factory(coro, name):
        t = asyncio.get_running_loop().create_task(coro, name=name)
        created.append(name)
        return t

    async def go():
        clock = Clock()
        blocker = asyncio.Event()

        async def slow_sleep(s):
            await blocker.wait()
        live = LiveSender(cloud=Cloud(), telemetry=Telemetry(), on_signals=_noop,
                          monotonic=clock.mono, sleep=slow_sleep, task_factory=factory)
        live.update(60)
        await asyncio.sleep(0)
        clock.t = 10.0
        live.update(5)                                 # czas względny: skrócenie też dozwolone
        assert live._deadline == 15.0 and len(created) == 1
        await live.async_stop()
    asyncio.run(go())
    assert created == ["volcast-live"]


def test_restart_after_409():
    cloud = Cloud(statuses=(409, 200))
    live, _, _ = make(cloud)

    async def go():
        live.update(60)
        await _finish(live)
        assert not live.running
        live.update(3)
        assert live.running
        await _finish(live)
    asyncio.run(go())
    assert len(cloud.sent) == 2


def test_async_stop_during_sleep():
    async def go():
        clock = Clock()
        blocker = asyncio.Event()

        async def slow_sleep(s):
            await blocker.wait()
        live = LiveSender(cloud=Cloud(), telemetry=Telemetry(), on_signals=_noop,
                          monotonic=clock.mono, sleep=slow_sleep)
        live.update(60)
        for _ in range(5):
            await asyncio.sleep(0)
        assert live.running
        await live.async_stop()
        assert not live.running
        await live.async_stop()                        # idempotentne
    asyncio.run(go())


def test_async_stop_does_not_swallow_own_cancellation():
    async def go():
        clock = Clock()
        blocker = asyncio.Event()

        async def stubborn_sleep(s):
            # pętla nie chce skończyć od razu po anulowaniu — async_stop czeka
            try:
                await blocker.wait()
            except asyncio.CancelledError:
                await asyncio.Event().wait()           # przerwie je dopiero anulowanie async_stop
                raise
        live = LiveSender(cloud=Cloud(), telemetry=Telemetry(), on_signals=_noop,
                          monotonic=clock.mono, sleep=stubborn_sleep)
        live.update(60)
        for _ in range(5):
            await asyncio.sleep(0)
        stopper = asyncio.get_running_loop().create_task(live.async_stop())
        for _ in range(5):                             # pętla przyjęła anulowanie i dalej czeka
            await asyncio.sleep(0)
        assert not stopper.done()
        stopper.cancel()
        try:
            await stopper
        except asyncio.CancelledError:
            return True
        return False
    assert asyncio.run(go()) is True


def test_non_int_live_for_s_treated_as_zero():
    live, _, _ = make(Cloud())
    live.update(True)
    live.update(None)
    assert not live.running


async def _noop(raw):
    return None
