"""Kanał sygnałów w prawdziwym HA: setup z blokiem `signals` w planie i rozładunek bez osieroconych zadań.

Gniazdo to atrapa (dołączenie do tematu przechodzi, potem cisza), plan idzie z podmienionego
`async_get_schedule`. Fixture sprzątający pytest-homeassistant-custom-component po teście
oblewa go, gdy zostanie żywe zadanie — to jest właściwa asercja „bez osieroconych zadań”.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import aiohttp

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from custom_components.volcast.cloud.client import VolcastCloud

from .conftest import BASE, control_of, make_entry, setup_entry

TOPIC = "ha-sig:" + "b" * 64
HOST = BASE.replace("https://", "wss://")
BLOCK = {"version": 1, "live_for_s": 0,
         "channel": {"url": f"{HOST}/realtime/v1/websocket", "apikey": "k" * 20, "topic": TOPIC}}


class HangingSocket:
    """Odpowiada „ok” na dołączenie, potem wisi na `receive` aż do limitu czasu."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False
        self._replies: list[str] = []

    async def send_str(self, data: str) -> None:
        frame = json.loads(data)
        self.sent.append(frame)
        if frame["event"] == "phx_join":
            self._replies.append(json.dumps({"topic": frame["topic"], "event": "phx_reply", "ref": frame["ref"],
                                             "payload": {"status": "ok"}}))

    async def receive(self, timeout=None):
        if self._replies:
            return aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, self._replies.pop(0), None)
        await asyncio.sleep(timeout if timeout is not None else 3600)
        raise asyncio.TimeoutError

    async def close(self) -> None:
        self.closed = True


async def test_channel_joins_from_plan_and_unload_leaves_no_tasks(hass: HomeAssistant, network_down):
    sockets: list[HangingSocket] = []
    urls: list[str] = []

    async def ws_connect(self, url, **_kw):
        urls.append(url)
        sockets.append(HangingSocket())
        return sockets[-1]

    async def get_schedule(self):
        return {"signals": BLOCK}

    with patch.object(VolcastCloud, "async_get_schedule", get_schedule), \
            patch.object(aiohttp.ClientSession, "ws_connect", ws_connect):
        entry = make_entry(hass)
        await setup_entry(hass, entry)
        rt = control_of(hass, entry)
        assert entry.state is ConfigEntryState.LOADED and rt.channel is not None
        for _ in range(50):                      # dołączenie idzie w zadaniu tła
            if rt.channel.connected:
                break
            await asyncio.sleep(0.01)
        assert rt.channel.connected is True
        assert len(urls) == 1 and urls[0].startswith(f"{HOST}/realtime/v1/websocket?")
        assert sockets[0].sent[0]["event"] == "phx_join"

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.NOT_LOADED
        assert rt.channel.connected is False and sockets[0].closed is True
        assert rt.channel._task is None and rt.live.running is False


async def test_plan_without_signals_opens_no_socket_in_real_ha(hass: HomeAssistant, network_down):
    opened: list[str] = []

    async def ws_connect(self, url, **_kw):
        opened.append(url)
        raise AssertionError("ws_connect bez bloku signals")

    async def get_schedule(self):
        return {"control_enabled": False}

    with patch.object(VolcastCloud, "async_get_schedule", get_schedule), \
            patch.object(aiohttp.ClientSession, "ws_connect", ws_connect):
        entry = make_entry(hass)
        await setup_entry(hass, entry)
        assert control_of(hass, entry).channel.connected is False
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    assert opened == []
