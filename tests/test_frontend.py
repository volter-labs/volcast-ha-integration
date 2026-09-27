"""Testy karty planu i panelu bocznego (rejestracja plików statycznych, zasobów Lovelace)."""
import asyncio
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from custom_components.volcast import frontend as fe

JS = Path(__file__).parents[1] / "custom_components" / "volcast" / "www" / "volcast-plan-card.js"


def test_card_url_versioned_and_path_bare():
    assert fe.card_url("2.0.0b2") == f"{fe.CARD_PATH}?v=2.0.0b2"
    assert fe.card_url(None) == fe.card_url("unknown") == fe.CARD_PATH and "?" not in fe.CARD_PATH


def test_resource_action():
    url = fe.card_url("2")
    assert fe.resource_action([], url) == ("create", None)
    assert fe.resource_action([{"id": "r1", "url": fe.card_url("1")}], url) == ("update", "r1")
    assert fe.resource_action([{"id": "r1", "url": url}], url) == ("none", "r1")


def test_register_card_never_raises():
    hass = SimpleNamespace(data={}, http=SimpleNamespace(async_register_static_paths=AsyncMock(side_effect=RuntimeError)))
    assert asyncio.run(fe.async_register_card(hass, "2")) is None


def test_register_card_once_and_resource_created():
    res = SimpleNamespace(async_get_info=AsyncMock(), async_items=lambda: [], async_create_item=AsyncMock(),
                          async_update_item=AsyncMock())
    hass = SimpleNamespace(data={"lovelace": SimpleNamespace(resources=res)},
                           http=SimpleNamespace(async_register_static_paths=AsyncMock()))
    assert asyncio.run(fe.async_register_card(hass, "2")) == fe.card_url("2")
    assert asyncio.run(fe.async_register_card(hass, "2")) == fe.card_url("2")
    assert hass.http.async_register_static_paths.await_count == 1
    res.async_create_item.assert_awaited_once()


def test_register_card_yaml_resources_still_returns_url():
    # Dashboard w trybie YAML: `resources` istnieje i wystawia listę, ale nie da
    # się do niej nic dopisać (żadnego `async_create_item`/`async_update_item`).
    # Rejestracja pliku statycznego musi się mimo to udać i zwrócić adres karty.
    res = SimpleNamespace(async_items=lambda: [])
    hass = SimpleNamespace(data={"lovelace": SimpleNamespace(resources=res)},
                           http=SimpleNamespace(async_register_static_paths=AsyncMock()))
    assert asyncio.run(fe.async_register_card(hass, "3")) == fe.card_url("3")
    assert hass.data.get("volcast_frontend") is True


def test_register_card_survives_frontend_not_loaded(monkeypatch):
    """Bez załadowanego `frontend` HA rzuca KeyError z `add_extra_js_url` — to nie błąd karty."""
    from homeassistant.components import frontend
    monkeypatch.setattr(frontend, "add_extra_js_url", MagicMock(side_effect=KeyError("frontend_extra_module_url")))
    res = SimpleNamespace(async_get_info=AsyncMock(), async_items=lambda: [], async_create_item=AsyncMock(),
                          async_update_item=AsyncMock())
    hass = SimpleNamespace(data={"lovelace": SimpleNamespace(resources=res)},
                           http=SimpleNamespace(async_register_static_paths=AsyncMock()))
    assert asyncio.run(fe.async_register_card(hass, "2")) == fe.card_url("2")
    assert hass.data.get("volcast_frontend") is True
    res.async_create_item.assert_awaited_once()


def test_register_panel_calls_panel_custom_with_expected_kwargs(monkeypatch):
    from homeassistant.components import panel_custom
    calls = []

    async def register(hass, **kwargs):
        calls.append(kwargs)
    monkeypatch.setattr(panel_custom, "async_register_panel", register)
    assert asyncio.run(fe.async_register_panel(SimpleNamespace(), "sensor.x", "2")) is True
    assert calls == [{"frontend_url_path": fe.PANEL_PATH, "webcomponent_name": "volcast-panel",
                      "sidebar_title": "Volcast", "sidebar_icon": "mdi:solar-power-variant",
                      "module_url": fe.card_url("2"), "config": {"entity": "sensor.x"},
                      "require_admin": False}]


def test_register_panel_never_raises_on_failure(monkeypatch):
    from homeassistant.components import panel_custom

    async def boom(*_a, **_k):
        raise RuntimeError("already registered")
    monkeypatch.setattr(panel_custom, "async_register_panel", boom)
    assert asyncio.run(fe.async_register_panel(SimpleNamespace(), "sensor.x", "2")) is False


def test_remove_panel_never_raises(monkeypatch):
    from homeassistant.components import frontend as frontend_mod

    def boom(*_a, **_k):
        raise RuntimeError("unknown panel")
    monkeypatch.setattr(frontend_mod, "async_remove_panel", boom)
    fe.async_remove_panel(SimpleNamespace())


def test_js_defines_elements_and_uses_new_attribute_names():
    text = JS.read_text(encoding="utf-8")
    assert 'customElements.define("volcast-plan-card"' in text or "customElements.define('volcast-plan-card'" in text
    assert "volcast-panel" in text
    assert not re.search(r"volter", text, re.I)
    for old in ("sloty", "zgoda_konta", "przelacznik_lokalny", "wazny_do", "sterowanie_wlaczone", "pojemnosc_kwh"):
        assert old not in text, old
    for new in ("slots", "account_consent", "local_switch", "valid_until", "battery_capacity_kwh", "display_kind"):
        assert new in text, new
    # Sensor nie ma ceny na najwyższym poziomie atrybutów; karta musi ją liczyć ze slotu.
    assert not re.search(r"\ba\.price\b", text)


def test_footer_price_comes_from_the_current_slot_not_a_top_level_attribute():
    text = JS.read_text(encoding="utf-8")
    assert "biezacy ? biezacy.price : null" in text


def test_js_parses_when_node_available():
    node = shutil.which("node")
    if node is None:
        return
    assert subprocess.run([node, "--check", str(JS)], capture_output=True).returncode == 0
