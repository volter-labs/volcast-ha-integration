"""Blok `signals` z odpowiedzi chmury — czysty parser bez I/O.

Każdy błąd formatu = pole pominięte (wartość domyślna), nigdy wyjątek: blok sygnałów
nie może wywrócić pobrania planu. Kanał (`channel`) jest ważny tylko wtedy, gdy wskazuje
na ten sam host co `base_url` z parowania — inaczej klucz trafiłby do obcego serwera.
Temat kanału jest losowym sekretem niskiej wagi: do logów wyłącznie `redact_topic`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

SIGNALS_VERSION_MAJOR = 1
LIVE_MAX_S = 900

_MAX_URL = 512
_MAX_APIKEY = 4096
_TOPIC = re.compile(r"ha-sig:[0-9a-f]{64}")
_TOPIC_PREFIX = "ha-sig:"
_WS = re.compile(r"\s")


@dataclass(frozen=True)
class ChannelCfg:
    url: str
    apikey: str
    topic: str


@dataclass(frozen=True)
class Signals:
    version: int
    channel: ChannelCfg | None
    live_for_s: int


def _int(v: Any) -> int | None:
    # bool jest podklasą int — nie jest liczbą z kontraktu
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _port(parts: Any, scheme_default: int) -> int | None:
    # `.port` rzuca ValueError przy porcie spoza zakresu
    p = parts.port
    return scheme_default if p is None else p


def _parse_channel(raw: Any, base_url: str) -> ChannelCfg | None:
    if not isinstance(raw, dict):
        return None
    url, apikey, topic = raw.get("url"), raw.get("apikey"), raw.get("topic")
    if not (isinstance(url, str) and isinstance(apikey, str) and isinstance(topic, str)):
        return None
    if not url or len(url) > _MAX_URL or _WS.search(url):
        return None
    if not 1 <= len(apikey) <= _MAX_APIKEY or _WS.search(apikey):
        return None
    if not _TOPIC.fullmatch(topic):
        return None
    try:
        u = urlsplit(url)
        b = urlsplit(base_url)
        if u.scheme != "wss" or b.scheme != "https":
            return None
        if not u.hostname or u.hostname != b.hostname:  # hostname jest małymi literami
            return None
        if u.username is not None or u.password is not None or "@" in u.netloc:
            return None
        # zapytanie i fragment dokleja/odrzuca klient; `?` bez treści też odrzucamy
        if u.query or u.fragment or "?" in url or "#" in url:
            return None
        if _port(u, 443) != _port(b, 443):
            return None
    except ValueError:
        return None
    return ChannelCfg(url, apikey, topic)


def parse_signals(raw: object, *, base_url: str) -> Signals | None:
    """Zwraca `Signals` albo None, gdy blok nie jest obsługiwanym obiektem v1."""
    if not isinstance(raw, dict):
        return None
    version = _int(raw.get("version"))
    if version is None or version != SIGNALS_VERSION_MAJOR:
        return None
    live = _int(raw.get("live_for_s"))
    live_for_s = 0 if live is None or live < 0 else min(live, LIVE_MAX_S)
    return Signals(version, _parse_channel(raw.get("channel"), base_url), live_for_s)


def redact_topic(topic: str) -> str:
    """Do logów: prefiks + 8 pierwszych znaków tematu; niepoprawne wejście bez znaków."""
    if isinstance(topic, str) and _TOPIC.fullmatch(topic):
        return f"{_TOPIC_PREFIX}{topic[len(_TOPIC_PREFIX):len(_TOPIC_PREFIX) + 8]}…"
    return f"{_TOPIC_PREFIX}…"
