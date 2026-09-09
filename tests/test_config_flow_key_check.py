"""Config flow: key-shape check runs before any network call and maps to strings.json."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, patch

import pytest

# --- extend the conftest stubs with what config_flow.py imports -----------------
_ce = sys.modules["homeassistant.config_entries"]


class _FakeConfigFlow:
    """Records async_show_form calls instead of rendering."""

    def __init_subclass__(cls, **kwargs):  # accepts `domain=...`
        pass

    def async_show_form(self, *, step_id, data_schema=None, errors=None, **_):
        return {"type": "form", "step_id": step_id, "errors": errors or {}}

    async def async_set_unique_id(self, _):
        return None

    def _abort_if_unique_id_configured(self):
        return None


for name, val in {
    "ConfigFlow": _FakeConfigFlow,
    "ConfigFlowResult": dict,
    "OptionsFlowWithConfigEntry": type("OptionsFlowWithConfigEntry", (), {}),
}.items():
    if not hasattr(_ce, name):
        setattr(_ce, name, val)

_const = sys.modules.setdefault("homeassistant.const", ModuleType("homeassistant.const"))
if not hasattr(_const, "CONF_API_KEY"):
    _const.CONF_API_KEY = "api_key"
_core = sys.modules.setdefault("homeassistant.core", ModuleType("homeassistant.core"))
if not hasattr(_core, "callback"):
    _core.callback = lambda f: f
_helpers = sys.modules["homeassistant.helpers"]
if "homeassistant.helpers.selector" not in sys.modules:
    sel = ModuleType("homeassistant.helpers.selector")
    sys.modules["homeassistant.helpers.selector"] = sel
    _helpers.selector = sel

from custom_components.volcast import config_flow  # noqa: E402

STRINGS = Path(__file__).parent.parent / "custom_components" / "volcast" / "strings.json"
TRANSLATIONS_EN = STRINGS.parent / "translations" / "en.json"


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize(
    "key, expected",
    [
        ("vk_494e...77827", "masked_key"),
        ("vk_494e…77827", "masked_key"),
        ("not-a-key", "invalid_key_format"),
    ],
)
def test_malformed_key_short_circuits_before_network(key, expected):
    flow = config_flow.VolcastConfigFlow()
    with patch.object(config_flow, "_validate_api_key", new=AsyncMock()) as validate:
        result = _run(flow.async_step_user({"api_key": key}))
    assert result["type"] == "form"
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": expected}
    validate.assert_not_awaited()


def test_well_formed_key_reaches_validation():
    flow = config_flow.VolcastConfigFlow()
    good = "vk_" + "0123456789abcdef" * 4
    with patch.object(
        config_flow, "_validate_api_key", new=AsyncMock(return_value={"title": "Volcast — X"})
    ) as validate, patch.object(flow, "async_step_production", new=AsyncMock(return_value={"type": "form", "step_id": "production"})):
        result = _run(flow.async_step_user({"api_key": f"  {good} "}))
    validate.assert_awaited_once()
    assert validate.await_args.args[0] == good  # stripped
    assert result["step_id"] == "production"


@pytest.mark.parametrize("path", [STRINGS, TRANSLATIONS_EN])
def test_error_codes_have_user_facing_strings(path):
    errors = json.loads(path.read_text(encoding="utf-8"))["config"]["error"]
    for code in ("masked_key", "invalid_key_format", "invalid_auth", "cannot_connect", "unknown"):
        assert code in errors, f"{path.name} missing config.error.{code}"
    assert "Copy or Share" in errors["masked_key"]
