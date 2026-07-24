"""Button platform for Volcast — ręczny force sync produkcji."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .reconciler import DailyReconciler

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Volcast buttons from a config entry."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    reconciler: DailyReconciler | None = entry_data.get("reconciler")
    if reconciler is None:
        # Bez energy_entity nie ma czego synchronizować — nie dodawaj buttona.
        return
    async_add_entities([VolcastSyncButton(reconciler, entry.entry_id)])


class VolcastSyncButton(ButtonEntity):
    """Button: ręczne uzgodnienie produkcji (wczoraj + dziś).

    Idempotentne — godziny już dostarczone i bieżąca godzina są pomijane,
    więc wielokrotne klikanie jest bezpieczne (bez luk = zero POSTów).
    """

    _attr_has_entity_name = True
    _attr_translation_key = "sync_now"
    _attr_icon = "mdi:cloud-sync"

    def __init__(self, reconciler: DailyReconciler, entry_id: str) -> None:
        self._reconciler = reconciler
        self._attr_unique_id = f"{entry_id}_sync_now"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry_id)},
            "name": "Volcast Solar Forecast",
            "manufacturer": "Volter Labs",
            "model": "PV Forecast",
            "entry_type": "service",
        }

    async def async_press(self) -> None:
        """Klik → uzgodnij wczoraj + dziś."""
        results = await self._reconciler.reconcile_recent()
        _LOGGER.info(
            "Manual sync pressed: %s",
            "; ".join(
                (r.reason or ("ok" if r.success else "fail"))
                + f" submitted={r.submitted}"
                for r in results
            ) or "no results",
        )
