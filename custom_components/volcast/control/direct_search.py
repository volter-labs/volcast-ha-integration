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
* `entities_option` / `direct_option` / `control_offer` — oferta kroku `control_mode` (obie opcje z kodem
  opisu z zamkniętego zbioru, `recommended`, `reason`).

Adres falownika nigdy nie trafia do logów ani do postępu onboardingu.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Iterable, Mapping, Sequence

from ..const import DOMAIN
from ..core.control.caps import entity_mode_ready
from ..core.control.conflict import CONFLICT_DOMAINS, SELF_DOMAIN, static_conflicts
from ..core.control.recommend import DIRECT, DIRECT_WITH_INTEGRATION_DATA, ENTITIES, REASONS, offer_path
from ..core.control.select import control_verified
from ..core.discovery.identify import Candidate, candidates_from, probe_order
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

# Kody opisu opcji w kroku `control_mode` — zbiór zamknięty z protokołu parowania; każdy należy do jednej
# opcji, chmura odrzuca kod spoza zbioru albo cudzej opcji.
ENTITIES_OK, ENTITIES_NO_WRITE, ENTITIES_UNVERIFIED = (
    "entities_ok", "entities_no_write_entities", "entities_profile_unverified")
DIRECT_OK, DIRECT_REQUIRES_DISABLE, DIRECT_DRAFT_TRIAL, DIRECT_UNAVAILABLE = (
    "direct_ok", "direct_requires_disable_integration", "direct_profile_draft_trial", "direct_unavailable")
DESCRIPTION_CODES = (ENTITIES_OK, ENTITIES_NO_WRITE, ENTITIES_UNVERIFIED, DIRECT_OK, DIRECT_REQUIRES_DISABLE,
                     DIRECT_DRAFT_TRIAL, DIRECT_UNAVAILABLE)


def _option(oid: str, available: bool, code: str) -> dict:
    return {"id": oid, "available": available, "description_code": code}


def entities_option(choice, mapped) -> dict:
    """Opcja „przez encje”: ta sama reguła co w opcjach (`entity_mode_ready`) — tylko profil i wpis integracji
    `verified` z encją trybu; niezweryfikowany → `entities_profile_unverified`, inaczej brak encji zapisu."""
    if entity_mode_ready(choice, mapped or {}):
        return _option(ENTITIES, True, ENTITIES_OK)
    domain = getattr(choice, "integration_domain", None)
    if choice is not None and domain and not control_verified(choice.profile, domain):
        return _option(ENTITIES, False, ENTITIES_UNVERIFIED)
    return _option(ENTITIES, False, ENTITIES_NO_WRITE)


def holders_state(hass) -> tuple:
    """Stan wpisów, które mogą trzymać falownik (integracje falowników, `modbus`, inne wpisy Volcast): para
    (id wpisu, wyłączony?). Zmiana (wyłączenie, usunięcie, dodanie) = warto ponowić odrzucony start
    bezpośredni; bez zmiany — nie. Tylko pamięć HA (bez rozwiązywania nazw); błąd → `()`."""
    try:
        entries = hass.config_entries.async_entries()
    except Exception:  # noqa: BLE001 — brak listy wpisów = brak wiedzy o zmianie
        return ()
    return tuple(sorted((str(getattr(e, "entry_id", "")), getattr(e, "disabled_by", None) is not None)
                        for e in entries if getattr(e, "domain", None) in CONFLICT_DOMAINS
                        or getattr(e, "domain", None) == SELF_DOMAIN))


def _foreign_clash(clash: Sequence[str]) -> bool:
    """Kolizja wyłącznie ze znanymi integracjami (falownika albo `modbus`) — po ich wyłączeniu adres jest wolny."""
    return bool(clash) and all(d in CONFLICT_DOMAINS for d in clash)


def direct_option(report: ProbeReport | None, profiles: Sequence, clash: Sequence[str] = (), *,
                  held_by=None) -> dict:
    """Opcja „bezpośrednio”.

    * Sonda rozpoznała falownik i oferta bez odmowy → dostępne: `direct_ok` (profil i sekcja `modbus`
      zweryfikowane) albo `direct_profile_draft_trial` (profil roboczy — drabina od próby bez zapisu).
    * Jedyną przeszkodą jest znana integracja na tym adresie (próba bez niej udana) → dostępne z
      `direct_requires_disable_integration`: po wyłączeniu integracji kolizja znika (wyłączone wpisy są pomijane),
      a wybór uruchamia wyszukiwanie i ustawia cel.
    * Bez sondy (`report` None): `held_by` = profil falownika, którego port trzyma integracja w HA (adres
      lokalny) — sonda tego adresu nie dotyka, więc bezpośrednio da się dopiero po wyłączeniu integracji.
    * Inaczej (nieustalona kolizja, inny wpis Volcast, nieudana próba, nic w sieci) → `direct_unavailable`."""
    if report is not None:
        if offer_reason(report, profiles, clash) is None:
            profile = _profile(profiles, report.identity.profile_id)
            return _option(DIRECT, True, DIRECT_OK if direct_verified(profile) else DIRECT_DRAFT_TRIAL)
        if _foreign_clash(clash) and offer_reason(report, profiles) is None:
            return _option(DIRECT, True, DIRECT_REQUIRES_DISABLE)
        return _option(DIRECT, False, DIRECT_UNAVAILABLE)
    if held_by is not None:
        return _option(DIRECT, True, DIRECT_REQUIRES_DISABLE)
    return _option(DIRECT, False, DIRECT_UNAVAILABLE)


def control_offer(rec, entities: dict, direct: dict) -> dict:
    """Pola oferty kroku `control_mode`: `options` (encje, bezpośrednio), `recommended` i `reason`.

    `recommended` = ścieżka rekomendacji zrzutowana na ofertę (`offer_path`), tylko gdy ta opcja jest dostępna
    (zalecenie nie trafia na kartę, której nie da się wybrać); `reason` = kod rekomendacji z listy zamkniętej
    — zostaje także bez `recommended`."""
    out: dict = {"options": [entities, direct]}
    if rec is None:
        return out
    path = offer_path(rec.path)
    if path is not None and any(o["id"] == path and o["available"] for o in out["options"]):
        out["recommended"] = path
    if rec.reason in REASONS:
        out["reason"] = rec.reason
    return out


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
    """Profile wbudowane i z feedu (blokujące — wołać przez `async_add_executor_job`): najpierw
    zweryfikowane z pakietu, potem reszta pakietu, na końcu feed (`probe_order`)."""
    out = []
    for pid in builtin_ids():
        try:
            out.append(load_builtin(pid))
        except ProfileError as err:
            _LOGGER.warning("Volcast profile %s rejected: %s", pid, err)
    return probe_order(out)


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
