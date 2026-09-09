"""Pre-network shape check for Volcast API keys.

A real key is ``vk_`` followed by 64 lowercase hex characters (67 chars total).
The most common support case is a user pasting the *shortened preview* shown in
the app (``vk_494e...77827``) instead of using the Copy button. That used to
surface as a generic "invalid API key" after a network round-trip; catching it
here lets the config flow explain exactly what went wrong.
"""

from __future__ import annotations

import re

API_KEY_PATTERN = re.compile(r"^vk_[0-9a-f]{64}$")

ERROR_MASKED_KEY = "masked_key"
ERROR_INVALID_FORMAT = "invalid_key_format"

_ELLIPSES = ("...", "…")


def check_api_key_format(api_key: str) -> str | None:
    """Return an error code for a malformed key, or ``None`` if the shape is valid.

    Codes map 1:1 to ``config.error`` keys in ``strings.json``.
    """
    key = api_key.strip()
    if any(e in key for e in _ELLIPSES):
        return ERROR_MASKED_KEY
    if not API_KEY_PATTERN.match(key):
        return ERROR_INVALID_FORMAT
    return None
