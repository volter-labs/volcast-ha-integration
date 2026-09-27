"""Lokalny wyłącznik sterowania (tylko wpis sparowany z kontem)."""
from __future__ import annotations

from .const import DOMAIN
from .control_entities import VolcastControlSwitch


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    rt = (hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}).get("control")
    if rt is not None:
        async_add_entities([VolcastControlSwitch(entry, rt)])
