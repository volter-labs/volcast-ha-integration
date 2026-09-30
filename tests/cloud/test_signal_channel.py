"""Kanał sygnałów (minimalny klient Phoenix) na atrapach gniazda, sesji i zegara.

Atrapy są lokalne: `FakeWs` odtwarza kolejkę ramek (tekst, bajty albo znaczniki
`TIMEOUT`/`CLOSE`/`HOLD`), `FakeWsSession.ws_connect` wydaje kolejne gniazda albo rzuca,
a `Clock` zastępuje monotonic/sleep — `receive(timeout)` i `sleep` przesuwają czas, więc
testy nie czekają naprawdę.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import namedtuple

import aiohttp
import pytest

from custom_components.volcast.cloud.signal_channel import SignalChannel
from custom_components.volcast.cloud.signals import ChannelCfg

HEX = "ab" * 32
HEX2 = "cd" * 32
KEY = "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoiYW5vbiJ9.c2VjcmV0LXNpZ25hdHVyZQ"
URL = "wss://staging.volcast.app/realtime/v1/websocket"
CFG = ChannelCfg(URL, KEY, f"ha-sig:{HEX}")
CFG2 = ChannelCfg(URL, KEY, f"ha-sig:{HEX2}")
TOPIC = f"realtime:ha-sig:{HEX}"

TIMEOUT, CLOSE, HOLD = "<timeout>", "<close>", "<hold>"
Msg = namedtuple("Msg", "type data extra")


def reply(ref="1", status="ok", topic=TOPIC):
    return json.dumps({"topic": topic, "event": "phx_reply", "ref": ref,
                       "payload": {"status": status, "response": {}}})


def hb_reply(ref):
    return reply(ref, topic="phoenix")


def ping(topic=TOPIC):
    return json.dumps({"topic": topic, "event": "broadcast", "ref": None,
                       "payload": {"type": "broadcast", "event": "signal", "payload": {}}})


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps: list[float] = []
        self.hold = False

    def monotonic(self):
        return self.t

    async def sleep(self, delay):
        self.sleeps.append(delay)
        self.t += delay
        if self.hold:
            await asyncio.get_running_loop().create_future()   # do anulowania
        await asyncio.sleep(0)


class FakeWs:
    def __init__(self, clock, *frames):
        self.clock = clock
        self.frames = list(frames)
        self.sent: list[dict] = []
        self.timeouts: list[float] = []
        self.closed = False

    async def send_str(self, data):
        if self.closed:
            raise aiohttp.ClientConnectionResetError("closed")
        self.sent.append(json.loads(data))

    async def receive(self, timeout=None):
        await asyncio.sleep(0)
        self.timeouts.append(timeout)
        if self.closed:
            return Msg(aiohttp.WSMsgType.CLOSED, None, None)
        item = self.frames.pop(0) if self.frames else TIMEOUT
        if item == HOLD:
            self.frames.insert(0, HOLD)
            await asyncio.get_running_loop().create_future()
        if item == TIMEOUT:
            self.clock.t += timeout
            raise asyncio.TimeoutError()
        if item == CLOSE:
            return Msg(aiohttp.WSMsgType.CLOSE, 1000, "")
        if isinstance(item, bytes):
            return Msg(aiohttp.WSMsgType.BINARY, item, None)
        return Msg(aiohttp.WSMsgType.TEXT, item, None)

    async def close(self):
        self.closed = True
        return True


class FakeWsSession:
    """`ws_connect` wydaje kolejne pozycje (gniazdo albo wyjątek); po wyczerpaniu wisi."""

    def __init__(self, *items):
        self.items = list(items)
        self.connects: list[dict] = []

    async def ws_connect(self, url, **kw):
        await asyncio.sleep(0)
        self.connects.append({"url": url, **kw})
        if not self.items:
            await asyncio.get_running_loop().create_future()
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def make(session, clock, rand=0.5, on_wake=None):
    wakes: list[int] = []

    async def default_wake():
        wakes.append(1)

    ch = SignalChannel(session, on_wake=on_wake or default_wake, sleep=clock.sleep,
                       rand=lambda: rand, monotonic=clock.monotonic)
    return ch, wakes


async def settle(pred=lambda: False, n=400):
    for _ in range(n):
        if pred():
            return
        await asyncio.sleep(0)


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------- join i ping

def test_join_ok_wakes_and_connects_with_contract_frames():
    async def main():
        clock = Clock()
        ws = FakeWs(clock, reply(), HOLD)
        s = FakeWsSession(ws)
        ch, wakes = make(s, clock)
        await ch.async_update(CFG)
        await settle(lambda: ch.connected and wakes)
        assert ch.connected and wakes == [1]
        c = s.connects[0]
        assert c["url"] == f"{URL}?apikey={KEY}&vsn=1.0.0"
        assert c["autoping"] is True and c["heartbeat"] is None and c["max_msg_size"] == 65536
        assert isinstance(c["timeout"], aiohttp.ClientWSTimeout)
        assert ws.sent[0] == {
            "topic": TOPIC, "event": "phx_join", "ref": "1", "join_ref": "1",
            "payload": {"config": {"broadcast": {"ack": False, "self": False},
                                   "presence": {"key": ""}, "private": False}}}
        await ch.async_stop()
        assert ws.closed and not ch.connected
    run(main())


def test_join_refused_backs_off_and_reconnects():
    async def main():
        clock = Clock()
        # obcy ref i obcy temat nie są odpowiedzią na nasz join
        ws1 = FakeWs(clock, reply(ref="7"), reply(topic=f"realtime:ha-sig:{HEX2}"),
                     reply(status="error"))
        ws2 = FakeWs(clock, reply(), HOLD)
        s = FakeWsSession(ws1, ws2)
        ch, wakes = make(s, clock)
        await ch.async_update(CFG)
        await settle(lambda: ch.connected)
        assert ws1.closed and clock.sleeps == [1.0] and wakes == [1] and ch.connected
        await ch.async_stop()
    run(main())


def test_join_without_reply_in_10s_backs_off():
    async def main():
        clock = Clock()
        ws1 = FakeWs(clock, TIMEOUT)
        s = FakeWsSession(ws1)
        ch, wakes = make(s, clock)
        await ch.async_update(CFG)
        await settle(lambda: len(s.connects) == 2)
        assert ws1.timeouts == [10.0] and ws1.closed and clock.sleeps == [1.0]
        assert wakes == [] and not ch.connected
        await ch.async_stop()
    run(main())


def test_ping_on_own_topic_wakes_other_topics_ignored():
    async def main():
        clock = Clock()
        other = f"realtime:ha-sig:{HEX2}"
        ws = FakeWs(clock, reply(), ping(other), ping("phoenix"), ping(), HOLD)
        s = FakeWsSession(ws)
        ch, wakes = make(s, clock)
        await ch.async_update(CFG)
        await settle(lambda: ws.frames == [HOLD])
        await settle()
        assert wakes == [1, 1] and ch.connected and len(s.connects) == 1
        await ch.async_stop()
    run(main())


def test_garbage_frames_ignored():
    async def main():
        clock = Clock()
        big = json.dumps({"topic": TOPIC, "event": "broadcast", "pad": "x" * 70000})
        ws = FakeWs(clock, reply(), "not json", big, '{"no": "fields"}', "[1, 2]",
                    json.dumps({"topic": 5, "event": "broadcast"}), b"\x00", ping(), HOLD)
        s = FakeWsSession(ws)
        ch, wakes = make(s, clock)
        await ch.async_update(CFG)
        await settle(lambda: ws.frames == [HOLD])
        await settle()
        assert wakes == [1, 1] and ch.connected and len(s.connects) == 1
        await ch.async_stop()
    run(main())


def test_wake_exception_does_not_kill_loop(caplog):
    async def main():
        clock = Clock()
        calls = []

        async def bad_wake():
            calls.append(1)
            raise RuntimeError(f"boom {KEY}")

        ws = FakeWs(clock, reply(), ping(), HOLD)
        s = FakeWsSession(ws)
        ch, _ = make(s, clock, on_wake=bad_wake)
        await ch.async_update(CFG)
        await settle(lambda: len(calls) == 2)
        await settle()
        assert calls == [1, 1] and ch.connected and len(s.connects) == 1
        await ch.async_stop()
    caplog.set_level(logging.DEBUG, logger="custom_components.volcast.cloud")
    run(main())
    assert "RuntimeError" in caplog.text and KEY not in caplog.text


# ----------------------------------------------------------------- heartbeat

def test_heartbeat_frames_and_two_missed_replies_reconnect():
    async def main():
        clock = Clock()
        ws1 = FakeWs(clock, reply())                       # dalej same TIMEOUT-y
        ws2 = FakeWs(clock, reply(), HOLD)
        s = FakeWsSession(ws1, ws2)
        ch, wakes = make(s, clock)
        await ch.async_update(CFG)
        await settle(lambda: len(s.connects) == 2 and ch.connected)
        hbs = ws1.sent[1:]
        assert hbs == [{"topic": "phoenix", "event": "heartbeat", "payload": {}, "ref": "2"},
                       {"topic": "phoenix", "event": "heartbeat", "payload": {}, "ref": "3"}]
        assert ws1.timeouts[1:4] == [25.0, 25.0, 25.0] and ws1.closed
        assert wakes == [1, 1]
        await ch.async_stop()
    run(main())


def test_heartbeat_reply_keeps_connection_alive():
    async def main():
        clock = Clock()
        ws = FakeWs(clock, reply(), TIMEOUT, hb_reply("2"), TIMEOUT, TIMEOUT, hb_reply("4"),
                    TIMEOUT, HOLD)
        s = FakeWsSession(ws)
        ch, _ = make(s, clock)
        await ch.async_update(CFG)
        await settle(lambda: ws.frames == [HOLD])
        await settle()
        assert [f["ref"] for f in ws.sent[1:]] == ["2", "3", "4", "5"]
        assert ch.connected and not ws.closed and len(s.connects) == 1
        await ch.async_stop()
    run(main())


def test_phx_error_and_close_frame_reconnect():
    async def main():
        clock = Clock()
        err = json.dumps({"topic": TOPIC, "event": "phx_error", "ref": "1", "payload": {}})
        ws1 = FakeWs(clock, reply(), err)
        ws2 = FakeWs(clock, reply(), CLOSE)
        ws3 = FakeWs(clock, reply(), HOLD)
        s = FakeWsSession(ws1, ws2, ws3)
        ch, wakes = make(s, clock)
        await ch.async_update(CFG)
        await settle(lambda: len(s.connects) == 3 and ch.connected)
        assert ws1.closed and ws2.closed and clock.sleeps == [1.0, 2.0] and wakes == [1, 1, 1]
        await ch.async_stop()
    run(main())


# ------------------------------------------------------------------- backoff

@pytest.mark.parametrize("rand,factor", [(0.5, 1.0), (0.0, 0.8), (1.0, 1.2)])
def test_backoff_sequence_with_jitter_bounds(rand, factor):
    async def main():
        clock = Clock()
        s = FakeWsSession(*[aiohttp.ClientConnectionError("x") for _ in range(9)])
        ch, _ = make(s, clock, rand=rand)
        await ch.async_update(CFG)
        await settle(lambda: len(s.connects) == 10)
        base = [1, 2, 4, 8, 16, 32, 60, 60, 60]
        assert clock.sleeps == pytest.approx([b * factor for b in base])
        await ch.async_stop()
    run(main())


def test_network_errors_of_all_kinds_back_off():
    async def main():
        clock = Clock()
        s = FakeWsSession(asyncio.TimeoutError(), OSError("x"), ValueError("x"),
                          aiohttp.WSServerHandshakeError(None, (), status=403))
        ch, _ = make(s, clock)
        await ch.async_update(CFG)
        await settle(lambda: len(s.connects) == 5)
        assert clock.sleeps == [1.0, 2.0, 4.0, 8.0]
        await ch.async_stop()
    run(main())


def _fails(n):
    return [aiohttp.ClientConnectionError("x") for _ in range(n)]


def test_backoff_resets_after_first_heartbeat_reply():
    async def main():
        clock = Clock()
        ws = FakeWs(clock, reply(), TIMEOUT, hb_reply("2"), CLOSE)
        s = FakeWsSession(*_fails(3), ws)
        ch, _ = make(s, clock)
        await ch.async_update(CFG)
        await settle(lambda: len(s.connects) == 5)
        assert clock.sleeps == [1.0, 2.0, 4.0, 1.0]
        await ch.async_stop()
    run(main())


def test_backoff_resets_after_30s_alive():
    async def main():
        clock = Clock()
        ws = FakeWs(clock, reply(), TIMEOUT, TIMEOUT, CLOSE)      # 50 s bez odpowiedzi na hb
        s = FakeWsSession(*_fails(3), ws)
        ch, _ = make(s, clock)
        await ch.async_update(CFG)
        await settle(lambda: len(s.connects) == 5)
        assert clock.sleeps == [1.0, 2.0, 4.0, 1.0]
        await ch.async_stop()
    run(main())


def test_short_lived_join_does_not_reset_backoff():
    async def main():
        clock = Clock()
        ws = FakeWs(clock, reply(), CLOSE)
        s = FakeWsSession(*_fails(3), ws)
        ch, wakes = make(s, clock)
        await ch.async_update(CFG)
        await settle(lambda: len(s.connects) == 5)
        assert clock.sleeps == [1.0, 2.0, 4.0, 8.0] and wakes == [1]
        await ch.async_stop()
    run(main())


# ------------------------------------------------------ cykl życia i konfiguracja

def test_topic_change_closes_old_socket_and_rejoins():
    async def main():
        clock = Clock()
        ws1 = FakeWs(clock, reply(), HOLD)
        ws2 = FakeWs(clock, reply(topic=f"realtime:ha-sig:{HEX2}"), HOLD)
        s = FakeWsSession(ws1, ws2)
        ch, wakes = make(s, clock)
        await ch.async_update(CFG)
        await settle(lambda: ch.connected)
        await ch.async_update(CFG2)
        assert ws1.closed
        await settle(lambda: ch.connected)
        assert ws2.sent[0]["topic"] == f"realtime:ha-sig:{HEX2}" and wakes == [1, 1]
        assert clock.sleeps == []                               # nowy temat bez backoffu
        await ch.async_stop()
    run(main())


def test_apikey_or_url_change_rejoins():
    async def main():
        clock = Clock()
        ws1, ws2, ws3 = (FakeWs(clock, reply(), HOLD) for _ in range(3))
        s = FakeWsSession(ws1, ws2, ws3)
        ch, _ = make(s, clock)
        await ch.async_update(CFG)
        await settle(lambda: ch.connected)
        await ch.async_update(ChannelCfg(URL, KEY + "x", CFG.topic))
        await settle(lambda: ch.connected)
        await ch.async_update(ChannelCfg(URL + "2", KEY + "x", CFG.topic))
        await settle(lambda: ch.connected)
        assert ws1.closed and ws2.closed and not ws3.closed and len(s.connects) == 3
        assert s.connects[1]["url"] == f"{URL}?apikey={KEY}x&vsn=1.0.0"
        await ch.async_stop()
    run(main())


def test_same_cfg_twice_connects_once():
    async def main():
        clock = Clock()
        ws = FakeWs(clock, reply(), HOLD)
        s = FakeWsSession(ws)
        ch, _ = make(s, clock)
        await ch.async_update(CFG)
        await settle(lambda: ch.connected)
        await ch.async_update(ChannelCfg(URL, KEY, CFG.topic))
        await settle()
        assert len(s.connects) == 1 and not ws.closed and ch.connected
        await ch.async_stop()
    run(main())


def test_none_cfg_never_connects_and_disconnects_existing():
    async def main():
        clock = Clock()
        ws = FakeWs(clock, reply(), HOLD)
        s = FakeWsSession(ws)
        ch, _ = make(s, clock)
        await ch.async_update(None)
        await settle()
        assert s.connects == [] and not ch.connected
        await ch.async_update(CFG)
        await settle(lambda: ch.connected)
        await ch.async_update(None)
        assert ws.closed and not ch.connected and len(s.connects) == 1
        await ch.async_update(CFG)                              # po None wolno wrócić
        await settle(lambda: len(s.connects) == 2)
        await ch.async_stop()
    run(main())


def test_stop_during_backoff_ends_task_quietly():
    async def main():
        clock = Clock()
        clock.hold = True
        s = FakeWsSession(aiohttp.ClientConnectionError("x"))
        tasks = []

        def factory(coro, name):
            t = asyncio.get_running_loop().create_task(coro, name=name)
            tasks.append(t)
            return t

        async def wake():
            pass

        ch = SignalChannel(s, on_wake=wake, sleep=clock.sleep, rand=lambda: 0.5,
                           monotonic=clock.monotonic, task_factory=factory)
        await ch.async_update(CFG)
        await settle(lambda: clock.sleeps == [1.0])
        assert clock.sleeps == [1.0] and not ch.connected
        await ch.async_stop()
        assert len(tasks) == 1 and tasks[0].done() and tasks[0].cancelled()
        await ch.async_stop()                                   # powtórne zatrzymanie = nic
    run(main())


def test_update_does_not_block_on_hanging_connect():
    async def main():
        clock = Clock()
        s = FakeWsSession()                                     # ws_connect wisi
        ch, _ = make(s, clock)
        await asyncio.wait_for(ch.async_update(CFG), timeout=1)
        await settle(lambda: len(s.connects) == 1)
        await asyncio.wait_for(ch.async_stop(), timeout=1)
    run(main())


# -------------------------------------------------------------------- logi

def test_secrets_never_logged(caplog):
    async def main():
        clock = Clock()
        full_url = f"{URL}?apikey={KEY}&vsn=1.0.0"
        ws1 = FakeWs(clock, reply(), "junk", ping(), CLOSE)
        ws2 = FakeWs(clock, reply(status="error"))
        ws3 = FakeWs(clock, reply(), HOLD)
        s = FakeWsSession(aiohttp.ClientConnectionError(f"cannot connect {full_url}"),
                          ws1, ws2, ValueError(f"bad {TOPIC}"), ws3)
        ch, _ = make(s, clock)
        await ch.async_update(CFG)
        await settle(lambda: len(s.connects) == 5 and ch.connected)
        await ch.async_update(CFG2)
        await ch.async_stop()
    caplog.set_level(logging.DEBUG, logger="custom_components.volcast.cloud")
    run(main())
    assert caplog.records
    assert KEY not in caplog.text and HEX not in caplog.text and HEX2 not in caplog.text
    assert "apikey" not in caplog.text


# ------------------------------------------------- zgodność ze starszym aiohttp

def _load_fresh_module(name):
    """Świeża kopia modułu kanału spoza `sys.modules` — import wykonany od nowa."""
    import importlib.util
    import pathlib

    import custom_components.volcast.cloud.signal_channel as mod
    spec = importlib.util.spec_from_file_location(
        f"custom_components.volcast.cloud.{name}", pathlib.Path(mod.__file__))
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    return fresh


def test_import_and_connect_without_client_ws_timeout(monkeypatch):
    """aiohttp < 3.11 (HA sprzed 2024.12) nie zna `ClientWSTimeout` — `timeout` to wtedy float."""
    monkeypatch.delattr(aiohttp, "ClientWSTimeout")
    fresh = _load_fresh_module("_signal_channel_old_aiohttp")

    async def main():
        clock = Clock()
        ws = FakeWs(clock, reply(), HOLD)
        s = FakeWsSession(ws)

        async def wake():
            pass

        ch = fresh.SignalChannel(s, on_wake=wake, sleep=clock.sleep, rand=lambda: 0.5,
                                 monotonic=clock.monotonic)
        await ch.async_update(CFG)
        await settle(lambda: ch.connected)
        assert ch.connected
        assert s.connects[0]["timeout"] == 10.0
        await ch.async_stop()
    run(main())


def test_new_aiohttp_uses_client_ws_timeout_without_receive_limit():
    async def main():
        clock = Clock()
        s = FakeWsSession(FakeWs(clock, reply(), HOLD))
        ch, _ = make(s, clock)
        await ch.async_update(CFG)
        await settle(lambda: ch.connected)
        t = s.connects[0]["timeout"]
        assert isinstance(t, aiohttp.ClientWSTimeout) and t.ws_receive is None and t.ws_close == 10.0
        await ch.async_stop()
    run(main())


# ------------------------------------------------ odporność pętli na nieoczekiwane

def test_phx_reply_with_unhashable_ref_is_ignored():
    async def main():
        clock = Clock()
        bad = json.dumps({"topic": "phoenix", "event": "phx_reply", "ref": [1],
                          "payload": {"status": "ok"}})
        bad2 = json.dumps({"topic": "phoenix", "event": "phx_reply", "ref": {"a": 1},
                           "payload": {"status": "ok"}})
        ws = FakeWs(clock, reply(), bad, bad2, HOLD)
        s = FakeWsSession(ws)
        ch, _ = make(s, clock)
        await ch.async_update(CFG)
        await settle(lambda: len(ws.timeouts) >= 4)
        assert not ch._task.done() and ch.connected and len(s.connects) == 1
        await ch.async_stop()
    run(main())


def test_unexpected_exception_reconnects_with_backoff(caplog):
    async def main():
        clock = Clock()
        ws = FakeWs(clock, reply(), HOLD)
        s = FakeWsSession(RuntimeError(f"secret {KEY}"), KeyError("x"), ws)
        ch, _ = make(s, clock)
        await ch.async_update(CFG)
        await settle(lambda: ch.connected)
        assert ch.connected and len(s.connects) == 3 and clock.sleeps == [1.0, 2.0]
        await ch.async_stop()
    caplog.set_level(logging.DEBUG, logger="custom_components.volcast.cloud")
    run(main())
    assert "RuntimeError" in caplog.text and KEY not in caplog.text


def test_dead_task_with_same_cfg_is_restarted():
    async def main():
        clock = Clock()
        s = FakeWsSession(FakeWs(clock, reply(), HOLD), FakeWs(clock, reply(), HOLD))
        ch, _ = make(s, clock)
        await ch.async_update(CFG)
        await settle(lambda: ch.connected)
        ch._task.cancel()                                        # zadanie zakończone poza nami
        await settle(lambda: ch._task.done())
        await ch.async_update(CFG)
        await settle(lambda: len(s.connects) == 2 and ch.connected)
        assert len(s.connects) == 2 and ch.connected
        await ch.async_stop()
    run(main())
