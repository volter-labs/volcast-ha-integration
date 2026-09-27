"""Karta planu i panel boczny — dodatek do sterowania, nigdy warunek działania."""
from __future__ import annotations

import logging
from pathlib import Path

_LOGGER = logging.getLogger(__name__)
CARD_PATH = "/volcast_static/volcast-plan-card.js"
PANEL_PATH = "volcast"
_FLAG = "volcast_frontend"


def card_url(version: str | None) -> str:
    return f"{CARD_PATH}?v={version}" if version and version != "unknown" else CARD_PATH


def resource_action(items: list[dict], url: str) -> tuple[str, str | None]:
    for item in items:
        current = str(item.get("url") or "")
        if current.split("?", 1)[0] == CARD_PATH:
            return ("none" if current == url else "update"), item.get("id")
    return "create", None


async def async_register_card(hass, version: str | None) -> str | None:
    """Zarejestruj plik karty pod stałym adresem i, jeśli się da, dopisz go jako zasób.

    Rejestracja pliku statycznego i dopisanie go jako zasobu dashboardu to dwa
    oddzielne kroki. Pierwszy musi się udać, bo bez niego adres jest martwy —
    jego porażka kończy funkcję (`None`). Drugi jest wygodą: dashboard w trybie
    YAML nie ma jak przyjąć zasobu (`resources` bez `async_create_item`), a to
    nie może ani zablokować panelu, ani przy każdym reloadzie próbować
    zarejestrować tę samą ścieżkę drugi raz — HA na duplikat rzuca wyjątkiem,
    więc flaga `_FLAG` idzie w górę zaraz po udanej rejestracji pliku, żeby
    kolejne wejścia (np. po zmianie opcji) w ogóle tam nie wracały.
    """
    url = card_url(version)
    if hass.data.get(_FLAG):
        return url
    path = str(Path(__file__).parent / "www" / "volcast-plan-card.js")
    try:
        try:
            from homeassistant.components.http import StaticPathConfig
            await hass.http.async_register_static_paths(
                [StaticPathConfig(CARD_PATH, path, cache_headers=False)])
        except ImportError:              # HA sprzed StaticPathConfig
            hass.http.register_static_path(CARD_PATH, path, cache_headers=False)
    except Exception:  # noqa: BLE001
        _LOGGER.warning("Volcast plan card could not be registered; add %s as a dashboard resource",
                        url, exc_info=True)
        return None
    hass.data[_FLAG] = True

    try:
        from homeassistant.components.frontend import add_extra_js_url
        add_extra_js_url(hass, url)
    except ImportError:
        pass

    try:
        resources = getattr(hass.data.get("lovelace"), "resources", None)
        if resources is not None:
            if hasattr(resources, "async_get_info"):
                await resources.async_get_info()
            action, ident = resource_action(list(resources.async_items()), url)
            if action == "create" and hasattr(resources, "async_create_item"):
                await resources.async_create_item({"res_type": "module", "url": url})
            elif action == "update" and ident and hasattr(resources, "async_update_item"):
                await resources.async_update_item(ident, {"res_type": "module", "url": url})
            elif action != "none" and not hasattr(resources, "async_create_item"):
                # Tryb YAML: nic tu nie dopiszemy programowo.
                _LOGGER.info("Dashboard is in YAML mode; add %s as a Lovelace resource manually", url)
    except Exception:  # noqa: BLE001 — zasób jest wygodą, nie warunkiem karty
        _LOGGER.debug("Volcast plan card resource registration skipped", exc_info=True)

    return url


async def async_register_panel(hass, entity_id: str | None, version: str | None) -> bool:
    try:
        from homeassistant.components import panel_custom
        await panel_custom.async_register_panel(
            hass, frontend_url_path=PANEL_PATH, webcomponent_name="volcast-panel", sidebar_title="Volcast",
            sidebar_icon="mdi:solar-power-variant", module_url=card_url(version),
            config={"entity": entity_id}, require_admin=False)
        return True
    except Exception:  # noqa: BLE001 — także „panel już zarejestrowany" po reloadzie
        _LOGGER.debug("Volcast panel not registered", exc_info=True)
        return False


def async_remove_panel(hass) -> None:
    try:
        from homeassistant.components.frontend import async_remove_panel as remove
        remove(hass, PANEL_PATH)
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Volcast panel removal skipped", exc_info=True)
