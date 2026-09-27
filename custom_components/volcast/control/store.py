"""Trwały stan sterowania wpisu: plan z chmury (praca bez łącza), zgoda konta,
lokalny przełącznik, własność stanu falownika i migawka do trybu bazowego.

`owner` wiąże własność i migawkę z profilem i encją trybu, dla których powstały —
migawka innego falownika albo innego mapowania nie może trafić w nowe encje.

`restore_keys` — klucze, które zapisaliśmy w tej własności, bez tych, które właściciel
potem zmienił (`taken_over`, na stałe do końca własności). Powrót do trybu bazowego
dotyczy tylko ich. `restore_keys` = None: stan zapisany przed tym polem — powrót obejmuje
wszystkie klucze migawki (jak dotąd)."""
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
    owner: dict = field(default_factory=dict)
    restore_keys: list[str] | None = None
    taken_over: list[str] = field(default_factory=list)


def _keys(values: list) -> list[str]:
    return [v for v in values if isinstance(v, str)]


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
        owner = raw.get("owner")
        restore_keys = raw.get("restore_keys")
        taken_over = raw.get("taken_over")
        return ControlState(
            plan_raw=plan if isinstance(plan, dict) else None,
            consent=consent if isinstance(consent, bool) else None,
            local_switch=b("local_switch", False), owned=b("owned", False),
            snapshot={k: v for k, v in snap.items() if isinstance(v, (int, float, str))
                      and not isinstance(v, bool)} if isinstance(snap, dict) else {},
            history_imported_at=hist if isinstance(hist, str) else None,
            owner={k: v for k, v in owner.items() if isinstance(k, str) and isinstance(v, str)}
            if isinstance(owner, dict) else {},
            restore_keys=_keys(restore_keys) if isinstance(restore_keys, list) else None,
            taken_over=_keys(taken_over) if isinstance(taken_over, list) else [])

    async def async_save(self, state: ControlState) -> None:
        await self._store.async_save(asdict(state))

    async def async_remove(self) -> None:
        await self._store.async_remove()
