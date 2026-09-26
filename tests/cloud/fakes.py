"""Atrapa aiohttp.ClientSession: kolejka odpowiedzi per (metoda, url bez zapytania)."""
from __future__ import annotations

import json as _json
from typing import Any


class FakeResponse:
    def __init__(self, status: int, body: Any = None):
        self.status = status
        self._body = body

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

    def add(self, method: str, url: str, status: Any, body: Any = None):
        self.routes.setdefault((method, url), []).append((status, body))

    def _take(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        queue = self.routes.get((method, url.split("?", 1)[0]))
        if not queue:
            raise AssertionError(f"brak odpowiedzi dla {method} {url}")
        status, body = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(status, Exception):
            raise status
        return FakeResponse(status, body)

    def get(self, url, **kw):
        return self._take("GET", url, **kw)

    def post(self, url, **kw):
        return self._take("POST", url, **kw)
