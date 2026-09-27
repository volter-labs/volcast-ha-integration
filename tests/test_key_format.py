"""Tests for API key shape validation (pre-network check in config flow)."""
from __future__ import annotations

import pytest

from custom_components.volcast.key_format import account_unique_id, check_api_key_format, is_legacy_unique_id

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


def test_account_unique_id_is_a_stable_hash_without_the_key():
    key = "vk_" + "0123456789abcdef" * 4
    uid = account_unique_id(key)
    assert uid == account_unique_id(f"  {key} ") and uid != account_unique_id("vk_" + "f" * 64)
    assert key not in uid and key[3:11] not in uid
    assert uid.startswith("account_") and len(uid) == len("account_") + 64


def test_legacy_unique_id_detection():
    """Wpisy sprzed skrótu miały jawny klucz jako unique_id (także skrócony/zły kształt)."""
    key = "vk_" + "a" * 64
    assert is_legacy_unique_id(key) and is_legacy_unique_id("vk_494e...77827")
    assert not is_legacy_unique_id(account_unique_id(key))
    assert not is_legacy_unique_id("discovery_only") and not is_legacy_unique_id(None)
