"""Wyszukiwanie falownika do połączenia bezpośredniego (warstwy 3–4) i ocena, czy da się je zaoferować.

Wspólne dla opcji integracji i onboardingu:

* `async_search` — kandydaci z odpowiedzi UDP 48899 albo jeden cel wpisany ręcznie; kolizje
  statyczne liczone Z GÓRY (nazwy hostów innych wpisów rozwiązywane, limit 2 s na nazwę), także
  żywe połączenie bezpośrednie innego wpisu na tym adresie; całość z limitem czasu
  (`DISCOVER_TIMEOUT_S`) — sonda jest sekwencyjna. Wyłącznie odczyt; nic nie rzuca.
* `target_from_report` — cel połączenia z raportu: adres z kandydata, odcisk urządzenia,
  moc znamionowa z rejestrów, klucze bez odczytu zwrotnego i możliwości z próby.
* `offer_reason` — None, gdy „Bezpośrednio” można zaoferować (tożsamość ∧ próba udana ∧ profil
  i jego sekcja `modbus` zweryfikowane ∧ brak kolizji), inaczej powód.
* `display_capabilities` — możliwości do pokazania: rejestr jest i da się go odczytać.
* `recommendation_text` — szczegół kroku `control_mode` z rekomendacją ścieżki (`core/control/recommend`).

Adres falownika nigdy nie trafia do logów ani do postępu onboardingu.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Iterable, Mapping, Sequence

from ..const import DOMAIN
from ..core.control.conflict import SELF_DOMAIN, static_conflicts
from ..core.control.recommend import DIRECT, DIRECT_WITH_INTEGRATION_DATA, ENTITIES
from ..core.discovery.identify import Candidate, candidates_from
from ..core.discovery.network import probe_udp_48899
from ..core.discovery.probe import ProbeReport, discover
from ..core.profile import ProfileError, builtin_ids, direct_verified, load_builtin
from ..core.transports.factory import make_transport
from .direct import async_entry_snaps
from .store import async_installation_salt

_LOGGER = logging.getLogger(__name__)
DISCOVER_TIMEOUT_S = 60.0
# Pętla zwrotna (symulator) — WYŁĄCZNIE zestawy testów; w działającej instalacji zawsze False.
ALLOW_LOOPBACK = False

NOT_FOUND, CONFLICT, IN_USE, UNVERIFIED = "direct_not_found", "direct_conflict", "direct_in_use", "direct_unverified"
RECOMMENDED = "control_recommended"
# Teksty kroku `control_mode` w onboardingu (strona i aplikacja tłumaczą je z jednej tabeli).
REMOTE_TEXT = {
    UNVERIFIED: "direct control is not available for this inverter yet",
    CONFLICT: "another integration is using this inverter",
    IN_USE: "another integration is using this inverter",
    NOT_FOUND: "inverter not found on the network",
    RECOMMENDED: "recommended: {path}",
}
REMOTE_TEXT_MAX = 200


def recommendation_text(rec) -> str:
    """Szczegół kroku `control_mode` z rekomendacją (≤200 znaków): ścieżka i integracja albo urządzenie.

    Bez adresu i seriala — tylko domena integracji albo marka i model z rekomendacji.
    """
    text = REMOTE_TEXT[RECOMMENDED].format(path=rec.path)
    if rec.path in (ENTITIES, DIRECT_WITH_INTEGRATION_DATA):
        subject = (rec.integration or {}).get("domain")
    elif rec.path == DIRECT:
        subject = " ".join(str(v) for v in ((rec.device or {}).get("manufacturer"),
                                            (rec.device or {}).get("model")) if v)
    else:
        subject = None
    if subject:
        text = f"{text} ({subject})"
    return text if len(text) <= REMOTE_TEXT_MAX else text[:REMOTE_TEXT_MAX - 1] + "…"


def load_profiles() -> list:
    """Profile wbudowane (blokujące — wołać przez `async_add_executor_job`)."""
    out = []
    for pid in builtin_ids():
        try:
            out.append(load_builtin(pid))
        except ProfileError as err:
            _LOGGER.warning("Volcast profile %s rejected: %s", pid, err)
    return out


def _default_factory(cfg):
    return make_transport(cfg, allow_loopback=ALLOW_LOOPBACK)


def live_hosts(hass, entry_id: str) -> set[str]:
    """Adresy z żywym połączeniem bezpośrednim INNEGO wpisu (sonda ich nie rusza)."""
    hosts = (getattr(hass, "data", None) or {}).get(DOMAIN, {}).get("direct_hosts", {}) or {}
    out = set()
    for host, conn in hosts.items():
        owner = getattr(getattr(conn, "_entry", None), "entry_id", None)
        if owner != entry_id:
            out.add(host)
    return out


async def async_clash(hass, entry_id: str, host: str, *, resolve=None) -> tuple[str, ...]:
    """Kolizja statyczna celu (fail-closed) i żywe połączenie innego wpisu → `volcast`."""
    try:
        snaps = await async_entry_snaps(hass, entry_id, resolve=resolve)
        clash = static_conflicts(host, snaps)
    except Exception as err:  # noqa: BLE001 — „nie wiemy” = kolizja
        _LOGGER.debug("Volcast direct search: conflict check failed (%s)", type(err).__name__)
        return ("unknown",)
    if host in live_hosts(hass, entry_id):
        clash = (*clash, SELF_DOMAIN)
    return tuple(dict.fromkeys(clash))


def clash_label(clash: Sequence[str]) -> str:
    """Domena(y) INNYCH integracji z kolizji do komunikatu (bez Volcast); bez znanej nazwy → `unknown`."""
    return ", ".join(d for d in clash if d not in ("unknown", SELF_DOMAIN)) or "unknown"


async def async_search(hass, entry, profiles: Sequence, *, manual: Candidate | None = None,
                       timeout_s: float = DISCOVER_TIMEOUT_S, transport_factory: Callable | None = None,
                       udp_probe: Callable[[], Awaitable] | None = None, allow_loopback: bool | None = None,
                       resolve=None) -> list[ProbeReport]:
    """Raporty sondy (kolejność kandydatów); pusta lista przy braku kandydatów albo przekroczonym czasie."""
    try:
        salt = await async_installation_salt(hass)
        if manual is not None:
            candidates = [manual]
        else:
            network = await (udp_probe or probe_udp_48899)()
            candidates = candidates_from(getattr(network, "replies", None) or [], (), None,
                                         allow_loopback=ALLOW_LOOPBACK if allow_loopback is None else allow_loopback)
        if not candidates:
            return []
        snaps = await async_entry_snaps(hass, entry.entry_id, resolve=resolve)
        live = live_hosts(hass, entry.entry_id)

        def conflicts(host: str) -> tuple[str, ...]:
            clash = static_conflicts(host, snaps)
            return (*clash, SELF_DOMAIN) if host in live else clash

        return await asyncio.wait_for(
            discover(candidates, profiles, transport_factory=transport_factory or _default_factory,
                     conflicts=conflicts, salt=salt), timeout_s)
    except asyncio.TimeoutError:
        _LOGGER.warning("Volcast direct search: no answer within %s s", int(timeout_s))
        return []
    except Exception as err:  # noqa: BLE001 — wyszukiwanie nie psuje opcji ani onboardingu
        _LOGGER.warning("Volcast direct search failed (%s)", type(err).__name__)
        return []


def found(reports: Iterable[ProbeReport]) -> list[ProbeReport]:
    """Raporty z rozpoznanym falownikiem (tożsamość i adres kandydata)."""
    return [r for r in reports if r.identity is not None and r.candidate is not None]


def target_from_report(report: ProbeReport) -> dict | None:
    ident, cand = report.identity, report.candidate
    if ident is None or cand is None or not ident.device_fp:
        return None
    target = {"profile_id": ident.profile_id, "transport": ident.transport, "host": cand.host, "port": ident.port,
              "unit_id": ident.unit_id, "device_fp": ident.device_fp,
              "unreadable": sorted(report.unreadable), "capabilities": dict(report.capabilities)}
    if ident.transport == "solarman_v5" and cand.logger_serial is not None:
        target["logger_serial"] = cand.logger_serial
    if isinstance(ident.rated_power_w, (int, float)) and not isinstance(ident.rated_power_w, bool) \
            and ident.rated_power_w > 0:
        target["rated_power_w"] = float(ident.rated_power_w)
    return target


def _profile(profiles: Sequence, pid: str | None):
    return next((p for p in profiles if p.id == pid), None)


def offer_reason(report: ProbeReport | None, profiles: Sequence, clash: Sequence[str] = ()) -> str | None:
    """None = „Bezpośrednio” dostępne; inaczej powód (kolejność: kolizja, brak, bez ścieżki rejestrów).

    Profil roboczy (draft) nie blokuje oferty: przy rozpoznanym falowniku i udanej próbie drabina
    weryfikacji rusza od identyfikacji (próba bez zapisu), a zapisy idą dopiero po zgodzie na szczeblach
    4–5. `UNVERIFIED` = brak profilu albo nieudana próba ścieżki rejestrów."""
    if clash or (report is not None and "conflict" in report.errors):
        return IN_USE if SELF_DOMAIN in clash else CONFLICT
    if report is None or report.identity is None:
        return NOT_FOUND
    profile = _profile(profiles, report.identity.profile_id)
    if profile is None or not report.direct_available:
        return UNVERIFIED
    return None


def display_capabilities(caps: Mapping[str, bool], unreadable: Iterable[str]) -> list[str]:
    """Możliwości do pokazania: rejestr jest (`caps[k]`) i da się go odczytać."""
    skip = set(unreadable)
    return sorted(k for k, v in caps.items() if v is True and k not in skip)


def brand_model(report: ProbeReport, profiles: Sequence) -> str:
    """„<Marka> <model>” z profilu (pierwsze słowo etykiety) i identyfikacji — bez adresu i seriala."""
    ident = report.identity
    profile = _profile(profiles, ident.profile_id) if ident else None
    words = str((profile.raw.get("label") if profile is not None else "") or "").split()
    parts = [p for p in (words[0] if words else "", (ident.model or "") if ident else "") if p]
    return " ".join(parts) or (ident.profile_id if ident else "?")


def label(report: ProbeReport, profiles: Sequence) -> str:
    """Etykieta kandydata BEZ adresu: marka i model, profil, status ścieżki rejestrów."""
    ident = report.identity
    profile = _profile(profiles, ident.profile_id) if ident else None
    status = "verified" if profile is not None and direct_verified(profile) else "test only"
    return f"{brand_model(report, profiles)} — {ident.profile_id if ident else '?'}, {status}"
