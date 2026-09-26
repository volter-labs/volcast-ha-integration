"""Atrapa aiohttp.ClientSession: kolejka odpowiedzi per (metoda, url bez zapytania).

Odwzorowuje dwa zachowania prawdziwej sesji, na których polega bezpieczeństwo klienta:
- przekierowania 3xx są śledzone, gdy wołający nie poda `allow_redirects=False`
  (nagłówki i ciało idą dalej — jak `X-API-Key` w aiohttp przy zmianie źródła);
- odpowiedź z opóźnieniem `delay_s` dłuższym niż `timeout.total` kończy się
  `asyncio.TimeoutError` (bez prawdziwego czekania).
"""
from __future__ import annotations

import asyncio
import json as _json
from typing import Any


class FakeResponse:
    def __init__(self, status: int, body: Any = None, headers: dict | None = None,
                 content_length: int | None = None):
        self.status = status
        self._body = body
        self.headers = headers or {}
        self.content_length = content_length

    async def json(self, content_type=None):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    async def text(self):
        return _json.dumps(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self):
        self.routes: dict[tuple[str, str], list[Any]] = {}
        self.calls: list[dict] = []

    def add(self, method: str, url: str, status: Any, body: Any = None, *, headers: dict | None = None,
            delay_s: float = 0.0, content_length: int | None = None):
        self.routes.setdefault((method, url), []).append((status, body, headers, delay_s, content_length))

    def _take(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        queue = self.routes.get((method, url.split("?", 1)[0]))
        if not queue:
            raise AssertionError(f"brak odpowiedzi dla {method} {url}")
        status, body, headers, delay_s, length = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(status, Exception):
            raise status
        timeout = kw.get("timeout")
        if delay_s and (timeout is None or timeout.total is None or delay_s > timeout.total):
            raise asyncio.TimeoutError()
        location = (headers or {}).get("Location")
        if 300 <= status < 400 and location and kw.get("allow_redirects", True):
            # Jak aiohttp: nagłówki (poza Authorization/Cookie) i ciało 307/308 lecą dalej.
            return self._take(method, location, **kw)
        return FakeResponse(status, body, headers, length)

    def get(self, url, **kw):
        return self._take("GET", url, **kw)

    def post(self, url, **kw):
        return self._take("POST", url, **kw)
