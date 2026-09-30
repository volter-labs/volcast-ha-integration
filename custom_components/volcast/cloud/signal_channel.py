"""Kanał sygnałów: minimalny klient Phoenix (Supabase Realtime) na jednym temacie.

Kanał niesie wyłącznie pingi bez danych — każde `broadcast` na naszym temacie i każdy
udany join budzą wołającego (`on_wake`), który sam pobiera plan i blok `signals`.
Treść pingu jest ignorowana, więc podrobiony ping kosztuje najwyżej jedno pobranie.
Gdy kanał leży, działa ścieżka awaryjna (sygnały z telemetrii i planu) — dlatego każdy
błąd kończy się tylko ponownym łączeniem z backoffem, nigdy wyjątkiem do wołającego.

Heartbeat i limity czasu liczymy sami (wstrzyknięte `monotonic`/`sleep`): `receive`
dostaje czas do najbliższego terminu (odpowiedź na join albo kolejny heartbeat).

Adres zawiera klucz, a temat jest sekretem niskiej wagi: do logów trafia wyłącznie
`redact_topic` i nazwa klasy wyjątku (treść wyjątku sieciowego bywa adresem).
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any
from urllib.parse import urlencode

import aiohttp

from .signals import ChannelCfg, redact_topic

_LOGGER = logging.getLogger(__name__)

_VSN = "1.0.0"
_TOPIC_PREFIX = "realtime:"
_JOIN_REF = "1"
_JOIN_TIMEOUT_S = 10.0
_CONNECT_TIMEOUT_S = 15.0
_HEARTBEAT_S = 25.0
# Tyle heartbeatów bez odpowiedzi = połączenie zerwane.
_MAX_MISSED_HEARTBEATS = 2
_MAX_FRAME = 64 * 1024
_BACKOFF_MIN_S = 1.0
_BACKOFF_MAX_S = 60.0
_BACKOFF_MAX_STEP = 6            # 2**6 > 60 — dalej rośnie już tylko sufit
_JITTER = 0.2
# Połączenie uznajemy za zdrowe (reset backoffu) dopiero po odpowiedzi na heartbeat
# albo po tylu sekundach od join — krótko żyjący join nie może zapętlić szybkiego łączenia.
_STABLE_S = 30.0
_WS_CLOSE_TIMEOUT_S = 10.0
# `ClientWSTimeout` jest od aiohttp 3.11 (HA 2024.12). Starsze (wspierane HA od 2024.4) biorą
# `timeout: float` = czas zamknięcia, a `receive_timeout` i tak domyślnie None — import modułu
# nie może rzucać na starym aiohttp, bo pociągnąłby za sobą całą integrację.
_WS_TIMEOUT: Any = (aiohttp.ClientWSTimeout(ws_receive=None, ws_close=_WS_CLOSE_TIMEOUT_S)
                    if hasattr(aiohttp, "ClientWSTimeout") else _WS_CLOSE_TIMEOUT_S)
_JOIN_PAYLOAD = {"config": {"broadcast": {"ack": False, "self": False},
                            "presence": {"key": ""}, "private": False}}
_CLOSING = (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING,
            aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR)
_NET_ERRORS = (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError)

TaskFactory = Callable[[Coroutine[Any, Any, None], str], "asyncio.Task[None]"]


class _ChannelDown(Exception):
    """Serwer zamknął gniazdo albo kanał (bez treści — powód idzie tylko do logu)."""


def _default_task_factory(coro: Coroutine[Any, Any, None], name: str) -> asyncio.Task[None]:
    return asyncio.get_running_loop().create_task(coro, name=name)


def _decode(msg: Any) -> dict | None:
    """Ramka tekstowa JSON z polami `topic`/`event` albo None (śmieci ignorujemy)."""
    if msg.type in _CLOSING:
        raise _ChannelDown(msg.type.name)
    if msg.type is not aiohttp.WSMsgType.TEXT:
        return None
    data = msg.data
    if not isinstance(data, str) or len(data) > _MAX_FRAME:
        _LOGGER.debug("signal channel: oversized frame ignored")
        return None
    try:
        frame = json.loads(data)
    except ValueError:
        _LOGGER.debug("signal channel: non-JSON frame ignored")
        return None
    if (not isinstance(frame, dict) or not isinstance(frame.get("topic"), str)
            or not isinstance(frame.get("event"), str)):
        _LOGGER.debug("signal channel: malformed frame ignored")
        return None
    return frame


class SignalChannel:
    """Jedno zadanie tła utrzymujące dołączenie do tematu z `ChannelCfg`."""

    def __init__(
        self,
        session: Any,
        *,
        on_wake: Callable[[], Awaitable[None]],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[], float] = random.random,
        monotonic: Callable[[], float] = time.monotonic,
        task_factory: TaskFactory | None = None,
    ) -> None:
        self._session = session
        self._on_wake = on_wake
        self._sleep = sleep
        self._rand = rand
        self._mono = monotonic
        self._task_factory = task_factory or _default_task_factory
        self._cfg: ChannelCfg | None = None
        self._task: asyncio.Task[None] | None = None
        self._connected = False
        self._healthy = False

    @property
    def connected(self) -> bool:
        """True tylko po udanym join, dopóki heartbeat żyje."""
        return self._connected

    async def async_update(self, cfg: ChannelCfg | None) -> None:
        """Ustawia konfigurację kanału; nie czeka na połączenie (to robi zadanie tła)."""
        if cfg == self._cfg and (cfg is None or self._task is not None):
            return
        await self.async_stop()
        if cfg is None:
            return
        self._cfg = cfg
        self._task = self._task_factory(self._run(cfg), "volcast signal channel")

    async def async_stop(self) -> None:
        """Anuluje zadanie (także w trakcie backoffu), zamyka gniazdo i czeka na koniec."""
        task, self._task, self._cfg = self._task, None, None
        self._connected = False
        if task is None:
            return
        task.cancel()
        await asyncio.wait({task})
        if not task.cancelled() and task.exception() is not None:
            _LOGGER.debug("signal channel ended: %s", type(task.exception()).__name__)

    # ------------------------------------------------------------------ pętla

    def _backoff(self, step: int) -> float:
        base = min(_BACKOFF_MIN_S * 2 ** step, _BACKOFF_MAX_S)
        return base * (1.0 + _JITTER * (2.0 * self._rand() - 1.0))

    async def _run(self, cfg: ChannelCfg) -> None:
        step = 0
        name = redact_topic(cfg.topic)
        while True:
            self._healthy = False
            try:
                await self._connect_once(cfg)
            except _ChannelDown as err:
                _LOGGER.debug("signal channel %s closed: %s", name, err)
            except _NET_ERRORS as err:
                _LOGGER.debug("signal channel %s failed: %s", name, type(err).__name__)
            finally:
                self._connected = False
            if self._healthy:
                step = 0
            delay = self._backoff(step)
            step = min(step + 1, _BACKOFF_MAX_STEP)
            await self._sleep(delay)

    async def _connect_once(self, cfg: ChannelCfg) -> None:
        url = f"{cfg.url}?{urlencode({'apikey': cfg.apikey, 'vsn': _VSN})}"
        async with asyncio.timeout(_CONNECT_TIMEOUT_S):
            ws = await self._session.ws_connect(
                url, autoping=True, heartbeat=None, timeout=_WS_TIMEOUT, max_msg_size=_MAX_FRAME)
        try:
            await self._serve(ws, cfg)
        finally:
            try:
                await ws.close()
            except _NET_ERRORS as err:
                _LOGGER.debug("signal channel close failed: %s", type(err).__name__)

    async def _next(self, ws: Any, deadline: float) -> dict | None:
        """Następna poprawna ramka przed terminem albo None, gdy termin minął."""
        while True:
            wait = deadline - self._mono()
            if wait <= 0:
                return None
            try:
                msg = await ws.receive(timeout=wait)
            except asyncio.TimeoutError:
                return None
            frame = _decode(msg)
            if frame is not None:
                return frame

    async def _serve(self, ws: Any, cfg: ChannelCfg) -> None:
        topic = _TOPIC_PREFIX + cfg.topic
        name = redact_topic(cfg.topic)
        await ws.send_str(json.dumps({"topic": topic, "event": "phx_join", "ref": _JOIN_REF,
                                      "join_ref": _JOIN_REF, "payload": _JOIN_PAYLOAD}))
        deadline = self._mono() + _JOIN_TIMEOUT_S
        while True:
            frame = await self._next(ws, deadline)
            if frame is None:
                _LOGGER.debug("signal channel %s: join timed out", name)
                return
            if frame["topic"] != topic:
                continue
            if frame["event"] == "phx_reply" and frame.get("ref") == _JOIN_REF:
                payload = frame.get("payload")
                if isinstance(payload, dict) and payload.get("status") == "ok":
                    break
                _LOGGER.debug("signal channel %s: join refused", name)
                return
            if frame["event"] in ("phx_error", "phx_close"):
                raise _ChannelDown(frame["event"])

        joined_at = self._mono()
        self._connected = True
        _LOGGER.debug("signal channel %s joined", name)
        try:
            await self._wake()
            await self._listen(ws, topic, joined_at)
        finally:
            if self._mono() - joined_at >= _STABLE_S:
                self._healthy = True

    async def _listen(self, ws: Any, topic: str, joined_at: float) -> None:
        ref = int(_JOIN_REF)
        pending: set[str] = set()       # heartbeaty bez odpowiedzi
        next_hb = joined_at + _HEARTBEAT_S
        while True:
            frame = await self._next(ws, next_hb)
            if frame is None:
                if len(pending) >= _MAX_MISSED_HEARTBEATS:
                    raise _ChannelDown("heartbeat lost")
                ref += 1
                await ws.send_str(json.dumps({"topic": "phoenix", "event": "heartbeat",
                                              "payload": {}, "ref": str(ref)}))
                pending.add(str(ref))
                next_hb = self._mono() + _HEARTBEAT_S
                continue
            event = frame["event"]
            if frame["topic"] == "phoenix":
                if event == "phx_reply" and frame.get("ref") in pending:
                    pending.clear()
                    self._healthy = True
            elif frame["topic"] == topic:
                if event == "broadcast":
                    await self._wake()
                elif event in ("phx_error", "phx_close"):
                    raise _ChannelDown(event)

    async def _wake(self) -> None:
        try:
            await self._on_wake()
        except Exception as err:  # noqa: BLE001 — callback nie może zabić pętli kanału
            _LOGGER.warning("signal channel wake callback failed: %s", type(err).__name__)
