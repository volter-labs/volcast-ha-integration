"""Tests for shared http_with_retry helper."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import aiohttp
import pytest

from custom_components.volcast.http_retry import http_with_retry, RetryResult


class _FakeResponse:
    def __init__(
        self,
        status: int,
        json_data: dict | None = None,
        *,
        aenter_raise: Exception | None = None,
    ):
        self.status = status
        self._json = json_data or {}
        self._aenter_raise = aenter_raise
        self.ok = 200 <= status < 300

    async def json(self):
        return self._json

    async def __aenter__(self):
        if self._aenter_raise is not None:
            raise self._aenter_raise
        return self

    async def __aexit__(self, *_):
        return False


def _mock_session(responses):
    """responses = list of _FakeResponse OR Exception instances."""
    iter_resp = iter(responses)
    session = MagicMock()

    def post(*args, **kwargs):
        nxt = next(iter_resp)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    session.post = MagicMock(side_effect=post)
    session.get = MagicMock(side_effect=post)
    return session


@pytest.mark.asyncio
async def test_http_with_retry_first_attempt_success():
    session = _mock_session([_FakeResponse(200, {"ok": True})])
    result = await http_with_retry(
        session, method="POST", url="http://test", payload={}, headers={},
        delays=(0,), per_attempt_timeout=1,
    )
    assert result.success is True
    assert result.attempts == 1
    assert result.data == {"ok": True}


@pytest.mark.asyncio
async def test_http_with_retry_503_then_success():
    session = _mock_session([
        _FakeResponse(503),
        _FakeResponse(200, {"ok": True}),
    ])
    result = await http_with_retry(
        session, method="POST", url="http://test", payload={}, headers={},
        delays=(0, 0), per_attempt_timeout=1,
    )
    assert result.success is True
    assert result.attempts == 2


@pytest.mark.asyncio
async def test_http_with_retry_all_attempts_503():
    session = _mock_session([_FakeResponse(503) for _ in range(4)])
    result = await http_with_retry(
        session, method="POST", url="http://test", payload={}, headers={},
        delays=(0, 0, 0, 0), per_attempt_timeout=1,
    )
    assert result.success is False
    assert result.status == 503
    assert result.retriable is True
    assert result.attempts == 4


@pytest.mark.asyncio
async def test_http_with_retry_401_no_retry():
    session = _mock_session([_FakeResponse(401)])
    result = await http_with_retry(
        session, method="POST", url="http://test", payload={}, headers={},
        delays=(0, 0, 0, 0), per_attempt_timeout=1,
    )
    assert result.success is False
    assert result.status == 401
    assert result.retriable is False
    assert result.attempts == 1


@pytest.mark.asyncio
async def test_http_with_retry_timeout_then_success():
    session = _mock_session([
        asyncio.TimeoutError(),
        _FakeResponse(200, {"ok": True}),
    ])
    result = await http_with_retry(
        session, method="POST", url="http://test", payload={}, headers={},
        delays=(0, 0), per_attempt_timeout=1,
    )
    assert result.success is True
    assert result.attempts == 2


@pytest.mark.asyncio
async def test_http_with_retry_429_is_retriable():
    session = _mock_session([
        _FakeResponse(429),
        _FakeResponse(200, {"ok": True}),
    ])
    result = await http_with_retry(
        session, method="POST", url="http://test", payload={}, headers={},
        delays=(0, 0), per_attempt_timeout=1,
    )
    assert result.success is True
    assert result.attempts == 2


@pytest.mark.asyncio
async def test_http_with_retry_empty_delays_raises():
    """Empty delays is a programmer error — explicit ValueError, not silent garbage."""
    session = _mock_session([])
    with pytest.raises(ValueError, match="non-empty"):
        await http_with_retry(
            session, method="POST", url="http://test", payload={}, headers={},
            delays=(),
        )


@pytest.mark.asyncio
async def test_http_with_retry_aenter_client_error_then_success():
    """ClientError raised from __aenter__ (e.g. DNS / connect refused) is retried."""
    session = _mock_session([
        _FakeResponse(0, aenter_raise=aiohttp.ClientError("connect refused")),
        _FakeResponse(200, {"ok": True}),
    ])
    result = await http_with_retry(
        session, method="POST", url="http://test", payload={}, headers={},
        delays=(0, 0), per_attempt_timeout=1,
    )
    assert result.success is True
    assert result.attempts == 2
