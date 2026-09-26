"""Config flow: step "user" is a menu (API key vs discovery-only)."""
from __future__ import annotations

import asyncio
import sys
from types import ModuleType
from typing import Any

import pytest

# --- extend the conftest stubs with what config_flow.py imports -----------------
_ce = sys.modules["homeassistant.config_entries"]


class AbortFlowStub(Exception):
    """Test double for homeassistant.data_entry_flow.AbortFlow."""


class _FakeConfigFlow:
    """Records async_show_form/async_show_menu/async_create_entry instead of rendering."""

    def __init_subclass__(cls, **kwargs):  # accepts `domain=...`
        pass

    def async_show_form(self, *, step_id, data_schema=None, errors=None, **_):
        return {"type": "form", "step_id": step_id, "errors": errors or {}}

    def async_show_menu(self, *, step_id, menu_options, **_):
        return {"type": "menu", "step_id": step_id, "menu_options": menu_options}

    def async_create_entry(self, *, title=None, data, options=None, **_):
        return {"type": "create_entry", "title": title, "data": data}

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


def _run(coro):
    return asyncio.run(coro)


def test_user_step_is_menu():
    flow = config_flow.VolcastConfigFlow()
    res = _run(flow.async_step_user())
    assert res == {"type": "menu", "step_id": "user", "menu_options": ["api_key", "discovery_only"]}


def test_discovery_only_creates_entry_without_api_key():
    flow = config_flow.VolcastConfigFlow()
    res = _run(flow.async_step_discovery_only())
    assert res["type"] == "create_entry"
    assert res["data"] == {"mode": "discovery_only"}
    assert "api_key" not in res["data"]


def test_second_discovery_only_entry_aborts(monkeypatch):
    flow = config_flow.VolcastConfigFlow()
    monkeypatch.setattr(flow, "_abort_if_unique_id_configured",
                        lambda: (_ for _ in ()).throw(AbortFlowStub("already_configured")))
    with pytest.raises(AbortFlowStub):
        _run(flow.async_step_discovery_only())
