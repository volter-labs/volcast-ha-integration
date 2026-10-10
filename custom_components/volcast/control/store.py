"""Trwały stan sterowania wpisu: plan z chmury (praca bez łącza), zgoda konta,
lokalny przełącznik, własność stanu falownika i migawka do trybu bazowego.

`owner` wiąże własność i migawkę z profilem i encją trybu, dla których powstały —
migawka innego falownika albo innego mapowania nie może trafić w nowe encje.

`restore_keys` — klucze, które zapisaliśmy w tej własności, bez tych, które właściciel
potem zmienił (`taken_over`, na stałe do końca własności). Powrót do trybu bazowego
dotyczy tylko ich. `restore_keys` = None: stan zapisany przed tym polem — powrót obejmuje
wszystkie klucze migawki (jak dotąd).

`owner["mode_uid"]` (identyfikator rejestru encji trybu — przeżywa zmianę entity_id)
jest w magazynie trzymany OBOK rekordu (`owner_mode_uid`), nie w nim: poprzednia wersja
porównuje cały rekord `{profile, domain, mode_entity}` i po powrocie do niej musi go
dalej uznać za swój (inaczej zgubiłaby migawkę i własność).

`tou_snapshot` — programy harmonogramu właściciela (surowe słowa) sprzed naszego pierwszego
zapisu okien czasowych; wczytywana przez osobny, walidujący loader (zły kształt → brak
migawki + ostrzeżenie, powrót idzie wtedy do programów bazowych).

`verification` — rekord drabiny weryfikacji urządzenia (`core/control/ladder.py`, `Ladder.to_record`);
zły kształt → pusty (drabina rusza od nowa) + ostrzeżenie. `plan_only` — tryb „tylko plan” (wybór
własnego sterownika): bez zapisów i bez ponownych napraw o konflikcie. `conflict_ack` — pary
`[kind, label]` konfliktów sterowników, przy których właściciel wybrał sterowanie Volcast: nie
zatrzymują drabiny ponownie (także po restarcie); złe wpisy pomijane.

`nvm_log` — ramki zapisu do pamięci nieulotnej falownika w oknie budżetu (`[klucz, czas UTC]`),
żeby budżet przeżył restart i przeładowanie.

Sól instalacji (odciski urządzenia i celu połączenia bezpośredniego) jest w OSOBNYM magazynie
(`volcast.installation`), nie w rekordzie sterowania: poprzednia wersja zapisuje ten rekord
własnym kształtem i zgubiłaby pole — a nowa sól unieważniłaby zapisane odciski."""
from __future__ import annotations

import asyncio
import logging
import math
import secrets
from dataclasses import asdict, dataclass, field

from homeassistant.helpers.storage import Store

from ..core.control.conflict import CONFLICT_KINDS, MAX_LABEL
from ..core.control.ladder import valid_record
from ..core.control.tou_writes import validate_tou_snapshot

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
_OWNER_MODE_UID = "owner_mode_uid"
_INSTALLATION_KEY = "volcast.installation"
_SALT_CACHE = "volcast_installation_salt"
_SALT_LOCK = "volcast_installation_salt_lock"
_SALT_BYTES = 16


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
    tou_snapshot: dict | None = None
    nvm_log: list = field(default_factory=list)
    verification: dict = field(default_factory=dict)
    plan_only: bool = False
    conflict_ack: list = field(default_factory=list)


CONFLICT_ACK_MAX = 16


def conflict_ack_pairs(raw) -> list:
    """Pary `[kind, label]` (rodzaj z kontraktu, etykieta ≤ 64), bez powtórzeń, najwyżej `CONFLICT_ACK_MAX`."""
    out: list = []
    for item in raw if isinstance(raw, (list, tuple)) else ():
        if isinstance(item, (list, tuple)) and len(item) == 2 and item[0] in CONFLICT_KINDS \
                and isinstance(item[1], str) and len(item[1]) <= MAX_LABEL and [item[0], item[1]] not in out:
            out.append([item[0], item[1]])
    return out[:CONFLICT_ACK_MAX]


def _nvm_log(raw) -> list:
    """Wpisy budżetu `[klucz, czas]`; niepoprawne pomijane (budżet i tak waliduje przy wczytaniu)."""
    out = []
    for item in raw if isinstance(raw, list) else ():
        if isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[0], str) and item[0] \
                and isinstance(item[1], (int, float)) and not isinstance(item[1], bool) and math.isfinite(item[1]):
            out.append([item[0], float(item[1])])
    return out


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
        owner = ({k: v for k, v in owner.items() if isinstance(k, str) and isinstance(v, str)}
                 if isinstance(owner, dict) else {})
        tou_raw = raw.get("tou_snapshot")
        tou_snapshot = validate_tou_snapshot(tou_raw) if tou_raw is not None else None
        if tou_raw is not None and tou_snapshot is None:
            _LOGGER.warning("Stored time-of-use snapshot is malformed; ignoring it")
        verification = raw.get("verification")
        if verification is not None and not valid_record(verification):
            _LOGGER.warning("Stored device verification record is malformed; starting the verification again")
            verification = None
        mode_uid = raw.get(_OWNER_MODE_UID)
        if owner and isinstance(mode_uid, str) and mode_uid:
            owner["mode_uid"] = mode_uid
        return ControlState(
            plan_raw=plan if isinstance(plan, dict) else None,
            consent=consent if isinstance(consent, bool) else None,
            local_switch=b("local_switch", False), owned=b("owned", False),
            snapshot={k: v for k, v in snap.items() if isinstance(v, (int, float, str))
                      and not isinstance(v, bool)} if isinstance(snap, dict) else {},
            history_imported_at=hist if isinstance(hist, str) else None,
            owner=owner,
            restore_keys=_keys(restore_keys) if isinstance(restore_keys, list) else None,
            taken_over=_keys(taken_over) if isinstance(taken_over, list) else [],
            tou_snapshot=tou_snapshot, nvm_log=_nvm_log(raw.get("nvm_log")),
            verification=dict(verification) if verification else {}, plan_only=b("plan_only", False),
            conflict_ack=conflict_ack_pairs(raw.get("conflict_ack")))

    async def async_save(self, state: ControlState) -> None:
        data = asdict(state)
        mode_uid = data["owner"].pop("mode_uid", None)
        if mode_uid:
            data[_OWNER_MODE_UID] = mode_uid
        await self._store.async_save(data)

    async def async_remove(self) -> None:
        await self._store.async_remove()


async def async_installation_salt(hass) -> bytes:
    """Sól instalacji (16 losowych bajtów), tworzona raz i trzymana poza rekordem sterowania.

    Nigdy w diagnostyce ani w logach. Zły zapis w magazynie → nowa sól (odciski do ponownej sondy).
    """
    cache = hass.data.get(_SALT_CACHE)
    if isinstance(cache, bytes) and len(cache) == _SALT_BYTES:
        return cache
    lock = hass.data.setdefault(_SALT_LOCK, asyncio.Lock())
    async with lock:                     # dwa wpisy przy pierwszym tworzeniu — jedna sól
        cache = hass.data.get(_SALT_CACHE)
        if isinstance(cache, bytes) and len(cache) == _SALT_BYTES:
            return cache
        return await _load_or_create_salt(hass)


async def _load_or_create_salt(hass) -> bytes:
    store = Store(hass, STORAGE_VERSION, _INSTALLATION_KEY)
    raw = await store.async_load()
    salt = None
    value = raw.get("salt") if isinstance(raw, dict) else None
    if isinstance(value, str):
        try:
            salt = bytes.fromhex(value)
        except ValueError:
            salt = None
    if salt is None or len(salt) != _SALT_BYTES:
        salt = secrets.token_bytes(_SALT_BYTES)
        await store.async_save({"salt": salt.hex()})
    hass.data[_SALT_CACHE] = salt
    return salt
