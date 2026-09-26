"""Trwały stan sterowania wpisu: plan z chmury (praca bez łącza), zgoda konta,
lokalny przełącznik, własność stanu falownika i migawka do trybu bazowego."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from homeassistant.helpers.storage import Store

STORAGE_VERSION = 1


@dataclass
class ControlState:
    plan_raw: dict | None = None
    consent: bool | None = None
    local_switch: bool = False
    owned: bool = False
    snapshot: dict = field(default_factory=dict)
    history_imported_at: str | None = None


class ControlStore:
    def __init__(self, hass, entry_id: str) -> None:
        self._store = Store(hass, STORAGE_VERSION, f"volcast.control.{entry_id}")

    async def async_load(self) -> ControlState:
        raw = await self._store.async_load()
        if not isinstance(raw, dict):
            return ControlState()

        def b(key, default):
            v = raw.get(key)
            return v if isinstance(v, bool) else default

        consent = raw.get("consent")
        snap = raw.get("snapshot")
        plan = raw.get("plan_raw")
        hist = raw.get("history_imported_at")
        return ControlState(
            plan_raw=plan if isinstance(plan, dict) else None,
            consent=consent if isinstance(consent, bool) else None,
            local_switch=b("local_switch", False), owned=b("owned", False),
            snapshot={k: v for k, v in snap.items() if isinstance(v, (int, float, str))
                      and not isinstance(v, bool)} if isinstance(snap, dict) else {},
            history_imported_at=hist if isinstance(hist, str) else None)

    async def async_save(self, state: ControlState) -> None:
        await self._store.async_save(asdict(state))

    async def async_remove(self) -> None:
        await self._store.async_remove()
