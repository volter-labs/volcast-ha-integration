"""Trwały stan wykonawcy ładowarki EV — osobny magazyn (`volcast.loads.{entry_id}`).

Osobny od rekordu sterowania falownikiem: poprzednia wersja zapisuje tamten rekord własnym
kształtem i zgubiłaby obce pola. Zły kształt pola → wartość domyślna (nic nie wiemy o
ostatnim zapisie, więc następny cykl może zapisać jeszcze raz — ładowarka nie ma budżetu NVM).

`last_setpoint_a` — ostatnia zapisana nastawa w jednostce encji nastawy (A; W dla ładowarek
sterowanych mocą). Czasy jako ISO-8601 UTC.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from homeassistant.helpers.storage import Store

STORAGE_VERSION = 1
ACTIONS = ("start", "stop")


@dataclass
class LoadsState:
    last_action: str | None = None          # "start" | "stop" — ostatnia NASZA komenda
    last_write_at: str | None = None
    last_setpoint_a: float | None = None
    plan_generated_at: str | None = None    # `loads_meta.plan_generated_at` planu, do którego należy stan
    fallback_since: str | None = None
    manual_event: str | None = None         # "start" | "stop"
    manual_at: str | None = None


def _text(v) -> str | None:
    return v if isinstance(v, str) and v else None


def _choice(v) -> str | None:
    return v if v in ACTIONS else None


def _number(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


class LoadsStore:
    def __init__(self, hass, entry_id: str) -> None:
        self._store = Store(hass, STORAGE_VERSION, f"volcast.loads.{entry_id}")

    async def async_load(self) -> LoadsState:
        raw = await self._store.async_load()
        if not isinstance(raw, dict):
            return LoadsState()
        return LoadsState(
            last_action=_choice(raw.get("last_action")),
            last_write_at=_text(raw.get("last_write_at")),
            last_setpoint_a=_number(raw.get("last_setpoint_a")),
            plan_generated_at=_text(raw.get("plan_generated_at")),
            fallback_since=_text(raw.get("fallback_since")),
            manual_event=_choice(raw.get("manual_event")),
            manual_at=_text(raw.get("manual_at")),
        )

    async def async_save(self, state: LoadsState) -> None:
        await self._store.async_save(asdict(state))

    async def async_remove(self) -> None:
        await self._store.async_remove()
