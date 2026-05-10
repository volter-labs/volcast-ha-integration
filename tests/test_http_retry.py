"""Tests for shared http_with_retry helper."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from custom_components.volcast.http_retry import http_with_retry, RetryResult


class _FakeResponse:
    def __init__(self, status: int, json_data: dict | None = None, text: str = ""):
        self.status = status
        self._json = json_data or {}
        self._text = text
        self.ok = 200 <= status < 300

    async def json(self):
        return self._json

    async def text(self):
        return self._text

    async def __aenter__(self):
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
