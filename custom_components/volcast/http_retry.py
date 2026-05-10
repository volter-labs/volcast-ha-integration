"""Shared HTTP retry helper for Volcast integration.

Used by both forecast coordinator and production submit path.
Pattern: 5/15/45s delays inside one logical operation, before raising.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

DEFAULT_DELAYS = (0, 5, 15, 45)
DEFAULT_TIMEOUT = 10  # seconds, per attempt
RETRIABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


@dataclass
class RetryResult:
    success: bool
    status: int | None = None
    data: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    last_error: str | None = None
    retriable: bool = True  # for non-2xx final state, was the failure retriable?


async def http_with_retry(
    session: aiohttp.ClientSession,
    *,
    method: str = "POST",
    url: str,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    delays: tuple[int, ...] = DEFAULT_DELAYS,
    per_attempt_timeout: int = DEFAULT_TIMEOUT,
) -> RetryResult:
    """Execute HTTP with retry. Returns RetryResult on every HTTP/network outcome.

    Contract:
      - NEVER raises for HTTP status codes (any of 1xx-5xx is wrapped in
        RetryResult). Callers inspect ``result.success`` / ``result.status`` /
        ``result.retriable`` and decide whether to raise UpdateFailed,
        queue for later, or surface to the user.
      - NEVER raises for network-level errors (``aiohttp.ClientError``,
        ``asyncio.TimeoutError``) — these are caught, recorded in
        ``last_error``, and counted as retriable attempts.
      - DOES raise ``ValueError`` if ``delays`` is empty. This is a
        programmer error / API misuse, not a runtime HTTP/network failure,
        so callers must fix the call site rather than handling it.
    """
    if not delays:
        raise ValueError("delays must be a non-empty tuple")

    last_status: int | None = None
    last_err: str | None = None
    last_retriable: bool = True
    timeout_obj = aiohttp.ClientTimeout(total=per_attempt_timeout)

    for attempt, delay in enumerate(delays):
        if delay:
            await asyncio.sleep(delay)
        try:
            kwargs: dict[str, Any] = {"timeout": timeout_obj}
            if headers:
                kwargs["headers"] = headers
            if payload is not None:
                kwargs["json"] = payload
            request_method = session.post if method.upper() == "POST" else session.get
            async with request_method(url, **kwargs) as resp:
                last_status = resp.status
                if resp.ok:
                    try:
                        data = await resp.json()
                    except (aiohttp.ContentTypeError, ValueError) as err:
                        _LOGGER.debug(
                            "Failed to parse JSON body on %d response: %s",
                            resp.status,
                            err,
                        )
                        data = {}
                    return RetryResult(
                        success=True,
                        status=resp.status,
                        data=data,
                        attempts=attempt + 1,
                    )
                if resp.status not in RETRIABLE_STATUSES:
                    last_retriable = False
                    last_err = f"non-retriable HTTP {resp.status}"
                    break
                last_err = f"HTTP {resp.status}"
                _LOGGER.debug(
                    "http_with_retry attempt %d/%d failed: %s",
                    attempt + 1,
                    len(delays),
                    last_err,
                )
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            last_err = f"network: {err}"
            _LOGGER.debug(
                "http_with_retry attempt %d/%d network error: %s",
                attempt + 1,
                len(delays),
                last_err,
            )

    return RetryResult(
        success=False,
        status=last_status,
        attempts=len(delays) if last_retriable else (attempt + 1),
        last_error=last_err,
        retriable=last_retriable,
    )
