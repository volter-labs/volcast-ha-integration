"""Tests for API key shape validation (pre-network check in config flow)."""
from __future__ import annotations

import pytest

from custom_components.volcast.key_format import check_api_key_format

VALID = "vk_" + "0123456789abcdef" * 4


def test_valid_key_passes():
    assert check_api_key_format(VALID) is None


def test_masked_preview_with_three_dots_is_detected():
    assert check_api_key_format("vk_494e...77827") == "masked_key"


def test_masked_preview_with_unicode_ellipsis_is_detected():
    assert check_api_key_format("vk_494e…77827") == "masked_key"


@pytest.mark.parametrize(
    "key",
    [
        "",
        "vk_",
        "vk_" + "0" * 63,          # one short
        "vk_" + "0" * 65,          # one long
        "VK_" + "0" * 64,          # wrong prefix case
        "vk_" + "g" * 64,          # non-hex
        "0" * 67,                  # no prefix
    ],
)
def test_wrong_shape_is_invalid_format(key):
    assert check_api_key_format(key) == "invalid_key_format"


def test_surrounding_whitespace_is_tolerated():
    assert check_api_key_format(f"  {VALID}\n") is None
