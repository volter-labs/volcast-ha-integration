"""Tests for config_flow title generation — empty-location regression.

The Volcast backend has been observed to return `location: ""` in the API
response. The old `.get("location", "Volcast")` default only fires on a
missing key, not an empty value, so the title degraded to "Volcast — "
(trailing em-dash and space). These tests pin down clean degradation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests import conftest  # noqa: F401  ensure HA stubs registered


def _mock_session(json_body: dict, status: int = 200):
    """Build a mocked aiohttp.ClientSession context manager."""
    resp = AsyncMock()
    resp.ok = (200 <= status < 300)
    resp.status = status
    resp.json = AsyncMock(return_value=json_body)

    get_ctx = AsyncMock()
    get_ctx.__aenter__ = AsyncMock(return_value=resp)
    get_ctx.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.get = MagicMock(return_value=get_ctx)

    session_ctx = MagicMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session)
    session_ctx.__aexit__ = AsyncMock(return_value=False)
    return session_ctx


class TestTitleGeneration:
    """`_validate_api_key` must produce a clean title regardless of backend quirks."""

    @pytest.mark.asyncio
    async def test_full_location_appears_in_title(self):
        from custom_components.volcast.config_flow import _validate_api_key

        body = {"attributes": {"location": "Melbourne, AU"}}
        with patch("aiohttp.ClientSession", return_value=_mock_session(body)):
            result = await _validate_api_key("key", "https://example.com/forecast")
        assert result == {"title": "Volcast — Melbourne, AU"}

    @pytest.mark.asyncio
    async def test_empty_string_location_degrades_to_volcast(self):
        """Regression: server returns location='' — title was 'Volcast — ' (broken)."""
        from custom_components.volcast.config_flow import _validate_api_key

        body = {"attributes": {"location": ""}}
        with patch("aiohttp.ClientSession", return_value=_mock_session(body)):
            result = await _validate_api_key("key", "https://example.com/forecast")
        assert result == {"title": "Volcast"}

    @pytest.mark.asyncio
    async def test_missing_location_key_degrades_to_volcast(self):
        from custom_components.volcast.config_flow import _validate_api_key

        body = {"attributes": {}}
        with patch("aiohttp.ClientSession", return_value=_mock_session(body)):
            result = await _validate_api_key("key", "https://example.com/forecast")
        assert result == {"title": "Volcast"}

    @pytest.mark.asyncio
    async def test_null_location_degrades_to_volcast(self):
        """Defence in depth: server returns location: null."""
        from custom_components.volcast.config_flow import _validate_api_key

        body = {"attributes": {"location": None}}
        with patch("aiohttp.ClientSession", return_value=_mock_session(body)):
            result = await _validate_api_key("key", "https://example.com/forecast")
        assert result == {"title": "Volcast"}

    @pytest.mark.asyncio
    async def test_whitespace_only_location_degrades_to_volcast(self):
        """Spaces / tabs only — still no useful location."""
        from custom_components.volcast.config_flow import _validate_api_key

        body = {"attributes": {"location": "   \t  "}}
        with patch("aiohttp.ClientSession", return_value=_mock_session(body)):
            result = await _validate_api_key("key", "https://example.com/forecast")
        assert result == {"title": "Volcast"}

    @pytest.mark.asyncio
    async def test_location_with_surrounding_whitespace_is_trimmed(self):
        from custom_components.volcast.config_flow import _validate_api_key

        body = {"attributes": {"location": "  Warsaw, PL  "}}
        with patch("aiohttp.ClientSession", return_value=_mock_session(body)):
            result = await _validate_api_key("key", "https://example.com/forecast")
        assert result == {"title": "Volcast — Warsaw, PL"}

    @pytest.mark.asyncio
    async def test_attributes_missing_entirely_degrades(self):
        from custom_components.volcast.config_flow import _validate_api_key

        body = {"state": 8.78}  # no "attributes" key
        with patch("aiohttp.ClientSession", return_value=_mock_session(body)):
            result = await _validate_api_key("key", "https://example.com/forecast")
        assert result == {"title": "Volcast"}

    @pytest.mark.asyncio
    async def test_attributes_null_does_not_crash(self):
        """Regression: backend returning {"attributes": null} previously crashed
        with AttributeError in `.get(...)`, surfacing as opaque "unknown" config-flow error.
        """
        from custom_components.volcast.config_flow import _validate_api_key

        body = {"attributes": None}
        with patch("aiohttp.ClientSession", return_value=_mock_session(body)):
            result = await _validate_api_key("key", "https://example.com/forecast")
        assert result == {"title": "Volcast"}

    @pytest.mark.asyncio
    async def test_non_string_location_degrades(self):
        """Defence in depth: non-string location (eg. backend bug returning a
        number) should not crash on .strip()."""
        from custom_components.volcast.config_flow import _validate_api_key

        body = {"attributes": {"location": 42}}
        with patch("aiohttp.ClientSession", return_value=_mock_session(body)):
            result = await _validate_api_key("key", "https://example.com/forecast")
        assert result == {"title": "Volcast"}
