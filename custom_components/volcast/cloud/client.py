"""Klienci HTTP chmury Volcast.

Każdy błąd sieci/formatu = wynik „brak", nigdy wyjątek do wołającego — z wyjątkiem
401 planu (`CloudAuthError`) i odmowy założenia sesji parowania
(`PairingDisabled`/`PairingError`), bo te zmieniają zachowanie wołającego.

Klucz konta i tokeny nigdy nie trafiają do logów: logujemy wyłącznie status HTTP
albo nazwę klasy wyjątku (treść wyjątku sieciowego bywa adresem z parametrami).

Kontrakt parowania (`POST pairing-session`, akcja w `body.action`):
begin 201 / 400 / 429 / 503; poll 202 pending, 200 confirmed (klucz RAZ) albo
consumed (wybory), 410 expired/cancelled, 404; progress 200 / 409 / 400;
request_plan 200 / 429 (ochłonięcie) / 409; cancel 200 / 409. Integracja nie słucha
kanału Realtime (pingi są bez danych) — wybory odczytuje z `poll` po `consumed`.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from ..key_format import API_KEY_PATTERN

_LOGGER = logging.getLogger(__name__)
_TIMEOUT = aiohttp.ClientTimeout(total=15)
# Import historii bywa długi (wycena wielu godzin); chmura wstawia tylko brakujące godziny.
_HISTORY_TIMEOUT = aiohttp.ClientTimeout(total=30)
# Chmura czeka na planer do 45 s i odpowiada dopiero po nim — czekamy dłużej.
_PLAN_TIMEOUT = aiohttp.ClientTimeout(total=60)
# Odpowiedzi chmury są małe; większe ciało (zadeklarowane) = błąd, nie czytamy go.
_MAX_BODY = 256 * 1024
# ValueError obejmuje błędne JSON-y (json.JSONDecodeError).
_NET_ERRORS = (aiohttp.ClientError, asyncio.TimeoutError, ValueError)
_BACKEND_KEYS = ("base_url", "forecast", "submit_production", "telemetry", "schedule",
                 "history_import", "pairing")
_MAX_URL = 512

# Kształty z chmury: id sesji i konta to UUID, token odpytywania `vps_` + 64 hex.
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_POLL_TOKEN = re.compile(r"^vps_[0-9a-f]{64}$")
# Limity pól opisowych po stronie chmury (dłuższe/ze znakami sterującymi = 400 albo null).
_NAME_MAX = 64
_VERSION_MAX = 32
_DETAIL_MAX = 200            # liczone w jednostkach UTF-16 (JS `length`)
_CHOICE_MAX = 64
_EXPIRES_MAX = 40
_FALLBACK_NAME = "Home Assistant"
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")
# Znaki, które chmura wycina z nazwy (klasa jak po stronie chmury, plus ZWJ). Nie usuwamy
# ich sami — chmura robi to lepiej (zostawia ZWJ w emoji); sprawdzamy tylko, czy po jej
# czyszczeniu coś zostanie, bo pusta nazwa to 400.
_INVISIBLE = re.compile(
    r"[\x00-\x1f\x7f-\x9f\xad\u061c\u180e\u200b-\u200f\u2028\u2029\u202a-\u202e"
    r"\u2060-\u2064\u2066-\u2069\ufeff\ufff9-\ufffb\U000e0000-\U000e007f]")


def _https(v: Any, *, allow_query: bool = False) -> bool:
    """Adres https z hostem: bez danych logowania, fragmentu, białych znaków i końcowego
    ukośnika; zapytanie tylko przy `allow_query` (adres strony parowania)."""
    if (not isinstance(v, str) or len(v) > _MAX_URL or v.endswith("/") or "#" in v
            or any(c.isspace() for c in v) or (not allow_query and "?" in v)):
        return False
    try:
        parts = urlsplit(v)
        host = parts.hostname
    except ValueError:
        return False
    return parts.scheme == "https" and bool(host) and "@" not in parts.netloc


def is_https_url(v: Any) -> bool:
    """Adres usługi (np. parowania) podany przez użytkownika: https, host, bez zapytania."""
    return _https(v)


def _clean(text: Any, limit: int) -> str:
    """Znaki sterujące → spacja, zwinięte białe znaki, obcięcie do limitu chmury."""
    s = _CONTROL_CHARS.sub(" ", str(text))
    return " ".join(s.split())[:limit].strip()


def _cut_utf16(text: str, limit: int) -> str:
    """Obcięcie do `limit` jednostek UTF-16 — tak liczy długość chmura (JS)."""
    while len(text.encode("utf-16-le")) // 2 > limit:
        text = text[:-1]
    return text


def _iso(v: Any) -> bool:
    if not isinstance(v, str) or not v or len(v) > _EXPIRES_MAX:
        return False
    try:
        datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _too_big(resp) -> bool:
    length = getattr(resp, "content_length", None)
    return isinstance(length, int) and length > _MAX_BODY


def _redirect(status: int) -> bool:
    return 300 <= status < 400


@dataclass(frozen=True)
class Backend:
    base_url: str
    forecast: str
    submit_production: str
    telemetry: str
    schedule: str
    history_import: str
    pairing: str

    @classmethod
    def from_dict(cls, raw: Any) -> "Backend | None":
        if not isinstance(raw, dict) or not all(_https(raw.get(k)) for k in _BACKEND_KEYS):
            return None
        # Klucz konta idzie na każdy z tych adresów — wszystkie muszą leżeć pod bazą.
        prefix = raw["base_url"] + "/"
        if not all(raw[k].startswith(prefix) for k in _BACKEND_KEYS if k != "base_url"):
            return None
        return cls(**{k: raw[k] for k in _BACKEND_KEYS})

    def as_dict(self) -> dict[str, str]:
        return {k: getattr(self, k) for k in _BACKEND_KEYS}


class CloudAuthError(Exception):
    """Klucz konta odrzucony (401)."""


async def _post_json(session, url: str, body: dict, headers: dict | None, what: str,
                     timeout: aiohttp.ClientTimeout = _TIMEOUT) -> tuple[int, Any]:
    """POST z JSON-em; (0, None) przy błędzie sieci, (status, None) przy złym ciele.

    Bez podążania za przekierowaniem: aiohttp przenosi `X-API-Key` (i ciało przy
    307/308) na inne źródło, także na http — 3xx to po prostu porażka.
    """
    kw: dict[str, Any] = {"json": body, "timeout": timeout, "allow_redirects": False}
    if headers is not None:
        kw["headers"] = headers
    try:
        async with session.post(url, **kw) as resp:
            if _redirect(resp.status) or _too_big(resp):
                _LOGGER.debug("%s refused: HTTP %s", what, resp.status)
                return 0, None
            try:
                data = await resp.json(content_type=None)
            except _NET_ERRORS:
                data = None
            if resp.status >= 300:
                _LOGGER.debug("%s HTTP %s", what, resp.status)
            return resp.status, data
    except _NET_ERRORS as err:
        _LOGGER.debug("%s failed: %s", what, type(err).__name__)
        return 0, None


class VolcastCloud:
    """Plan (`get-schedule`, zawsze `?contract=2`), telemetria i import historii."""

    def __init__(self, session, api_key: str, backend: Backend) -> None:
        self._s = session
        self._key = api_key
        self._b = backend

    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self._key}

    async def async_get_schedule(self) -> dict | None:
        try:
            async with self._s.get(f"{self._b.schedule}?contract=2", headers=self._headers(),
                                   timeout=_TIMEOUT, allow_redirects=False) as resp:
                if resp.status == 401:
                    raise CloudAuthError
                if resp.status != 200 or _too_big(resp):
                    _LOGGER.debug("get-schedule HTTP %s", resp.status)
                    return None
                data = await resp.json(content_type=None)
        except CloudAuthError:
            raise
        except _NET_ERRORS as err:
            _LOGGER.debug("get-schedule failed: %s", type(err).__name__)
            return None
        return data if isinstance(data, dict) else None

    async def async_post_telemetry(self, reading: dict) -> bool:
        status, _ = await _post_json(self._s, self._b.telemetry, {"readings": [reading]},
                                     self._headers(), "device-telemetry")
        return status == 200

    async def async_import_history(self, hours: list[dict]) -> dict | None:
        status, data = await _post_json(self._s, self._b.history_import,
                                        {"source": "ha_recorder", "hours": hours},
                                        self._headers(), "history import", _HISTORY_TIMEOUT)
        return data if status == 200 and isinstance(data, dict) else None


@dataclass(frozen=True)
class PairingSession:
    session_id: str
    poll_token: str
    connect_url: str
    expires_at: str


@dataclass(frozen=True)
class PollResult:
    # "pending" | "confirmed" | "consumed" | "expired" | "gone" | "disabled" | "error"
    status: str
    api_key: str | None = None
    user_id: str | None = None
    backend: Backend | None = None
    choices: dict = field(default_factory=dict)


class PairingDisabled(Exception):
    """Parowanie wyłączone po stronie chmury (503)."""


class PairingError(Exception):
    """Sesji nie da się założyć (limit, zły format odpowiedzi, sieć)."""


class PairingClient:
    """Integracja w sesji parowania; uwierzytelnia się `session_id` + `poll_token`."""

    def __init__(self, session, url: str) -> None:
        self._s = session
        self._url = url

    async def _call(self, body: dict, timeout: aiohttp.ClientTimeout = _TIMEOUT) -> tuple[int, Any]:
        # Bez nagłówka klucza: sesja ma własny token, klucza konta jeszcze nie ma.
        return await _post_json(self._s, self._url, body, None, f"pairing {body.get('action')}", timeout)

    @staticmethod
    def _auth(s: PairingSession, action: str) -> dict:
        return {"action": action, "session_id": s.session_id, "poll_token": s.poll_token}

    async def async_begin(self, *, instance_id: str, instance_name: str, ha_version: str,
                          client_version: str) -> PairingSession:
        name = _clean(instance_name, _NAME_MAX)
        if not _INVISIBLE.sub("", name).strip():
            name = _FALLBACK_NAME
        body: dict[str, Any] = {"action": "begin", "kind": "ha", "instance_id": instance_id,
                                "instance_name": name}
        for key, value in (("ha_version", ha_version), ("client_version", client_version)):
            cleaned = _clean(value, _VERSION_MAX)
            if cleaned:
                body[key] = cleaned
        status, data = await self._call(body)
        if status == 503:
            raise PairingDisabled
        if status != 201 or not isinstance(data, dict):
            code = data.get("error") if isinstance(data, dict) else None
            raise PairingError(f"begin HTTP {status}" + (f" ({code})" if isinstance(code, str) else ""))
        sid, token = data.get("session_id"), data.get("poll_token")
        url, expires = data.get("connect_url"), data.get("expires_at")
        if not (isinstance(sid, str) and _UUID.match(sid)):
            raise PairingError("begin: malformed session id")
        if not (isinstance(token, str) and _POLL_TOKEN.match(token)):
            raise PairingError("begin: malformed poll token")
        if not _https(url, allow_query=True):
            raise PairingError("begin: connect_url must be https")
        if not _iso(expires):
            raise PairingError("begin: malformed expires_at")
        return PairingSession(sid, token, url, expires)

    async def async_poll(self, s: PairingSession) -> PollResult:
        status, data = await self._call(self._auth(s, "poll"))
        if status == 202:
            return PollResult("pending")
        if status == 410:            # wygasła albo anulowana — dla integracji to samo
            return PollResult("expired")
        if status == 404:
            return PollResult("gone")
        if status == 503:
            return PollResult("disabled")
        if status != 200 or not isinstance(data, dict):
            return PollResult("error")
        if data.get("status") == "confirmed":
            key, user_id = data.get("api_key"), data.get("user_id")
            backend = Backend.from_dict(data.get("backend"))
            if not (isinstance(key, str) and API_KEY_PATTERN.match(key)):
                _LOGGER.debug("pairing poll: malformed account key")
                return PollResult("error")
            if backend is None or not (isinstance(user_id, str) and _UUID.match(user_id)):
                _LOGGER.debug("pairing poll: malformed backend or account")
                return PollResult("error")
            return PollResult("confirmed", api_key=key, user_id=user_id, backend=backend)
        if data.get("status") == "consumed":
            raw = data.get("choices")
            choices = {k: v for k, v in raw.items()
                       if isinstance(k, str) and isinstance(v, str) and len(v) <= _CHOICE_MAX
                       } if isinstance(raw, dict) else {}
            return PollResult("consumed", choices=choices)
        return PollResult("error")

    async def async_progress(self, s: PairingSession, steps: list[dict]) -> bool:
        # Chmura odrzuca CAŁY postęp przy jednym złym `detail` — czyścimy go tutaj.
        payload = []
        for step in steps:
            step = dict(step)
            if "detail" in step:
                step["detail"] = _cut_utf16(_clean(step["detail"], _DETAIL_MAX), _DETAIL_MAX).strip()
            payload.append(step)
        status, _ = await self._call({**self._auth(s, "progress"), "steps": payload})
        return status == 200

    async def async_request_plan(self, s: PairingSession) -> dict | None:
        status, data = await self._call(self._auth(s, "request_plan"), _PLAN_TIMEOUT)
        if status == 429:            # plan zlecony przed chwilą — to nie błąd
            return {"skipped": "cooldown"}
        return data if status == 200 and isinstance(data, dict) else None

    async def async_cancel(self, s: PairingSession) -> None:
        await self._call(self._auth(s, "cancel"))
