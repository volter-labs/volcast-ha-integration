"""Onboarding na żywo po parowaniu: postęp publikowany do sesji parowania (strona
i telefon pokazują to samo), wybory zdalne (sposób sterowania, źródło cen) i pierwszy
plan. Działa niezależnie od cyklu życia wpisu (zmiana opcji przeładowuje wpis) —
sterowanie odczytuje zawsze świeżo przez `runtime()`. Nigdy nie rzuca; w logu tylko
nazwy klas wyjątków.

Detale kroków to frazy po angielsku, które strona i aplikacja tłumaczą z jednej tabeli
(„N slots", „queued", „planner unavailable", „none found", „read only"…). Znaki
niewidoczne czyści chmura — tu niczego z tego powodu nie odrzucamy.

Pierwszy plan zawsze ma jawny wynik (`plan_outcome`), nigdy ogólne „gotowe":
- plan z co najmniej jednym slotem → `done "N slots"`;
- cooldown (plan zlecony przed chwilą) → `active "queued"` i ponowienie po cooldownie
  (najwyżej `_PLAN_ATTEMPTS` prób, potem `done "queued"` — krok nie wisi);
- planer pominął (np. rynek jeszcze nieobsługiwany) → `done "queued"`;
- odmowa planera (`success` nie True, także brak poziomu konta), 0 albo brak slotów,
  planer nieosiągalny → `error "planner unavailable"` (nieosiągalny: z ponowieniem).

Encję ceny proponujemy tylko wtedy, gdy JUŻ TERAZ daje pełną serię
(`has_usable_prices_now`); inaczej „none found" — właściciel wybiera inne źródło.

Wybory zdalne:
- stosujemy je najwyżej RAZ na sesję: zastosowany wybór (sesja, wartość, czas) trafia do
  danych wpisu razem ze zmianą opcji, w jednej aktualizacji. Nowy przebieg (np. po
  restarcie HA w oknie 30 min) go nie powtarza — późniejsza zmiana w opcjach wygrywa;
- gdy wpis właśnie się przeładowuje (brak `runtime()`), wybór sposobu sterowania czeka
  na następne odpytanie — nigdy nie przepada na stałe;
- inne źródło cen niż HA ustawia aplikacja/chmura — krok cen kończy się bez zmian tutaj.
Nieudana publikacja postępu jest ponawiana z wycofaniem (5 s, podwajane do 60 s).

Połączenie bezpośrednie: gdy wykrywanie nie znalazło integracji falownika w HA, onboarding szuka
falownika w sieci (tylko odczyt); znaleziony daje kroki „<Marka> <model> (direct)”, moc i możliwości
(rejestry, które istnieją i dają się odczytać). Krok `control_mode` w stanie `choice` niesie szczegół
`options: entities`, `options: direct` albo `options: entities,direct` (starsi klienci) oraz ofertę:
`options` (obie ścieżki z `available` i kodem opisu), `recommended` i `reason` z rekomendacji ścieżki
(`direct_search.control_offer`). Zdalny wybór `direct` uruchamia wyszukiwanie (gdy go jeszcze nie było —
także przy integracji falownika w HA: sonda pomija adresy innych integracji bez ramki) i ustawia cel
bezpośredni; gdy się nie da — krok `error` z jednym z trzech tekstów. Adres falownika nigdy nie trafia
do postępu.

Jedno zastosowanie (kontrakt sterowania §4): wybór, który kreator zastosował, dostaje w zapisie
`applied_choices` znacznik (`cloud/control_choice`), a ta sama ścieżka z planu jest wtedy bez działania;
gdy plan zastosował ścieżkę wcześniej (stan już taki, jak wybrany), wybór z kreatora kończy krok bez wywołań.

Czujnik zużycia domu: gdy opcja jest pusta, a wśród WŁASNYCH encji zmapowanego
falownika (`ControlRuntime.inverter_entities`) raport wykrywania ma DOKŁADNIE jeden
jednoznaczny licznik zużycia domu (`house_load_candidate`), ustawiamy go i importujemy
historię; inaczej krok zostaje wyborem właściciela. Wybór zapisujemy jak wybór zdalny —
raz na sesję, więc wyczyszczony przez właściciela nie wraca po restarcie HA.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Mapping
from zoneinfo import ZoneInfo

import homeassistant.util.dt as dt_util

from .cloud.control_choice import APPLIED_CHOICES, WIZARD_MARK, wizard_mark
from .const import (CONF_PAIRING, CONTROL_MODE_DIRECT, CONTROL_MODE_ENTITIES, OPT_CONTROL_MODE, OPT_DIRECT_TARGET,
                    OPT_DIRECT_TRIAL, OPT_LOAD_ENERGY, OPT_PRICE_BUY, OPT_PRICE_CURRENCY)
from .control import direct_search as ds
from .control.runtime import readings_missing
from .core.control.caps import entity_mode_options, entity_mode_ready
from .core.control.history import house_load_candidate
from .core.control.recommend import recommend
from .core.prices import has_usable_prices_now

_LOGGER = logging.getLogger(__name__)
STEP_KEYS = ("account", "inverter", "installation", "capabilities", "control_mode", "prices",
             "consumption", "first_plan")
_STATES = frozenset({"pending", "active", "done", "choice", "error"})
_MAX_DETAIL = 200
_WRITE_KEYS = frozenset({"mode", "power_w", "soc_min", "soc_max", "export_limit_w", "export_limit_enabled"})
# Kod błędu planera z chmury (jak `KOD_BLEDU` po stronie serwera).
_ERROR_CODE = re.compile(r"[a-z_]{1,64}")
# Cooldown `request_plan` po stronie chmury to 2 min — ponawiamy tuż po nim.
_PLAN_RETRY_S = 125.0
_PLAN_ATTEMPTS = 3
_GONE = ("expired", "gone", "disabled")
_APPLIED = APPLIED_CHOICES
_PLAN_ONLY = "plan_only"
# Zapis „zastosowano" dla automatycznie wybranego licznika zużycia domu.
_AUTO_LOAD = "load_energy"
_REPOST_MIN_S = 5.0
_REPOST_MAX_S = 60.0
_PRICE_SOURCE = re.compile(r"[A-Za-z0-9_:.-]{1,64}")

PLAN_UNAVAILABLE = "planner unavailable"
QUEUED = "queued"
NONE_FOUND = "none found"
NOT_READY = "inverter control entities not found"
READ_ONLY_NO_READINGS = "read only, no readings"
NO_READINGS = "no readings"


def progress_payload(steps: Mapping[str, tuple[str, str | None]], offer: Mapping | None = None) -> list[dict]:
    """Kroki postępu; `offer` (pola `options`/`recommended`/`reason`) tylko przy `control_mode` w stanie `choice`."""
    out: list[dict] = []
    for key in STEP_KEYS:
        if key not in steps:
            continue
        state, detail = steps[key]
        item = {"key": key, "state": state if state in _STATES else "error"}
        if detail:
            item["detail"] = str(detail)[:_MAX_DETAIL]
        if key == "control_mode" and item["state"] == "choice" and offer:
            item.update(offer)
        out.append(item)
    return out


def plan_outcome(res: Any) -> str:
    """Jawny wynik `request_plan`: ok | cooldown | skipped | failed:<kod>
    (`no_readings` ustawia onboarding sam, bez wołania planera)."""
    if not isinstance(res, dict):
        return "failed:unreachable"
    skipped = res.get("skipped")
    if skipped == "cooldown":
        return "cooldown"
    if res.get("success") is not True:
        err = res.get("error")
        return f"failed:{err}" if isinstance(err, str) and _ERROR_CODE.fullmatch(err) else "failed:planner_failed"
    if isinstance(skipped, str) and skipped:
        return "skipped"
    n = res.get("slots_count")
    if isinstance(n, bool) or not isinstance(n, int) or n <= 0:
        return "failed:no_slots"            # 0/brak slotów to odmowa planera, nie plan
    return "ok"


def _plan_only(rt) -> bool:
    return getattr(getattr(rt, "executor", None), "plan_only", False) is True


def _kw(watts: float | None) -> str | None:
    if not watts:
        return None
    return f"{watts / 1000.0:g} kW"


class Onboarding:
    def __init__(self, hass, entry_id: str, *, client, session, live_until: datetime,
                 runtime: Callable[[], object | None], report: Callable[[], dict | None],
                 import_history: Callable[[], Awaitable[dict | None]], utcnow=dt_util.utcnow,
                 sleep=asyncio.sleep, discovery_wait_s: float = 45.0, choice_poll_s: float = 5.0,
                 search: Callable[[], Awaitable[list]] | None = None, profiles=()) -> None:
        self._hass, self._entry_id = hass, entry_id
        self._client, self._session, self._live_until = client, session, live_until
        self._runtime, self._report, self._import_history = runtime, report, import_history
        self._utcnow, self._sleep = utcnow, sleep
        self._discovery_wait_s, self._choice_poll_s = discovery_wait_s, choice_poll_s
        self._steps: dict[str, tuple[str, str | None]] = {k: ("pending", None) for k in STEP_KEYS}
        self._price_candidate: str | None = None
        self._plan_attempts = 0
        self._plan_retry_at: datetime | None = None
        self._dirty = False
        self._repost_delay = _REPOST_MIN_S
        self._repost_at: datetime | None = None
        self.first_plan_outcome: str | None = None
        self._search = search
        self._profiles = list(profiles)
        self._direct_report = None             # raport sondy z rozpoznanym falownikiem (bez integracji w HA)
        self._inverter_integration = False
        self._offer: dict | None = None        # oferta kroku `control_mode` (gdy w stanie `choice`)
        self._searched = False                 # wyszukiwanie falownika już było (start albo wybór `direct`)
        self._refused: tuple | None = None     # stan wpisów-kolizji przy ostatniej odmowie `direct` (kolizja)

    async def async_run(self) -> None:
        try:
            await self._run()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 — onboarding nigdy nie psuje wpisu
            _LOGGER.warning("Volcast onboarding stopped (%s)", type(err).__name__)

    # ── pomocnicze ─────────────────────────────────────────────────────────
    async def _set(self, key: str, state: str, detail: str | None = None) -> None:
        if self._steps.get(key) == (state, detail):
            return
        self._steps[key] = (state, detail)
        await self._publish()

    async def _publish(self) -> None:
        ok = await self._client.async_progress(self._session, progress_payload(self._steps, self._offer))
        if ok is True:
            self._dirty, self._repost_at, self._repost_delay = False, None, _REPOST_MIN_S
            return
        # Nieudana publikacja — ponowimy w pętli, coraz rzadziej, gdy chmura ciągle odrzuca.
        self._dirty = True
        self._repost_at = self._utcnow() + timedelta(seconds=self._repost_delay)
        self._repost_delay = min(self._repost_delay * 2, _REPOST_MAX_S)

    def _state(self, key: str) -> str:
        return self._steps[key][0]

    def _live(self) -> bool:
        return self._utcnow() < self._live_until

    def _entry(self):
        return self._hass.config_entries.async_get_entry(self._entry_id)

    def _options(self) -> Mapping[str, Any]:
        entry = self._entry()
        return entry.options if entry is not None else {}

    def _tz(self):
        get_default = getattr(dt_util, "get_default_time_zone", None)
        return get_default() if get_default is not None else ZoneInfo(self._hass.config.time_zone)

    def _applied(self) -> dict[str, Any]:
        """Wybory zdalne już zastosowane w TEJ sesji (z danych wpisu — przeżywają restart)."""
        entry = self._entry()
        pairing = (getattr(entry, "data", None) or {}).get(CONF_PAIRING) if entry is not None else None
        rec = pairing.get(_APPLIED) if isinstance(pairing, dict) else None
        if not isinstance(rec, dict) or rec.get("session_id") != self._session.session_id:
            return {}
        choices = rec.get("choices")
        return dict(choices) if isinstance(choices, dict) else {}

    def _commit_choices(self, patch: dict, applied: dict[str, str], marks: Mapping[str, dict] | None = None) -> None:
        """Opcje i zapis „zastosowano" w JEDNEJ aktualizacji wpisu (jedno przeładowanie). `marks` = znacznik
        „zastosowano z kreatora” (`cloud/control_choice`) przy wyborze, który kreator naprawdę zastosował."""
        entry = self._entry()
        if entry is None or not (patch or applied):
            return
        data = dict(getattr(entry, "data", None) or {})
        pairing = dict(data.get(CONF_PAIRING) or {})
        choices = self._applied()
        at = self._utcnow().isoformat()
        for key, value in applied.items():
            choices[key] = {"value": value, "at": at}
            if marks and key in marks:
                choices[key][WIZARD_MARK] = dict(marks[key])
        pairing[_APPLIED] = {"session_id": self._session.session_id, "choices": choices}
        data[CONF_PAIRING] = pairing
        update: dict[str, Any] = {"data": data}
        if patch:
            options = {**entry.options, **patch}
            update["options"] = {k: v for k, v in options.items() if v is not None}
        self._hass.config_entries.async_update_entry(entry, **update)

    def _control_ready(self, rt) -> bool:
        """Ta sama reguła co w opcjach (`entity_mode_ready`)."""
        return entity_mode_ready(getattr(rt, "choice", None), getattr(rt, "mapped", None) or {})

    def _usable_price(self, entity_id: str | None) -> bool:
        if not entity_id:
            return False
        st = self._hass.states.get(entity_id)
        if st is None:
            return False
        currency = (self._options().get(OPT_PRICE_CURRENCY) or "").strip().upper() or None
        try:
            return has_usable_prices_now(st.attributes, currency, self._tz(), self._utcnow())
        except Exception as err:  # noqa: BLE001 — zła encja = nieużywalna
            _LOGGER.debug("Volcast onboarding: price entity check failed (%s)", type(err).__name__)
            return False

    async def _wait_report(self) -> dict | None:
        waited = 0.0
        while (report := self._report()) is None and waited < self._discovery_wait_s:
            await self._sleep(2.0)
            waited += 2.0
        return report

    # ── przebieg ───────────────────────────────────────────────────────────
    async def _run(self) -> None:
        await self._set("account", "done")
        await self._set("inverter", "active")
        report = await self._wait_report() or {}
        rt = self._runtime()
        await self._inverter_step(report)
        ident = self._direct_report.identity if self._direct_report is not None else None
        rated = getattr(rt, "rated_power_w", None) or (ident.rated_power_w if ident is not None else None)
        await self._set("installation", "done", _kw(rated))
        if self._control_ready(rt):
            await self._set("capabilities", "done", ", ".join(sorted(set(rt.mapped) & _WRITE_KEYS)))
        elif self._direct_report is not None:
            caps = ds.display_capabilities(self._direct_report.capabilities, self._direct_report.unreadable)
            await self._set("capabilities", "done", ", ".join(caps) or "read only")
        else:
            await self._set("capabilities", "done", "read only")
        mode = self._options().get(OPT_CONTROL_MODE)
        if mode in (CONTROL_MODE_ENTITIES, CONTROL_MODE_DIRECT):
            await self._set("control_mode", "done", mode)
        elif "control_mode" in self._applied():
            await self._set("control_mode", "done")      # zastosowany wcześniej; opcje wygrywają
        else:
            self._offer = await self._control_offer(rt, report)
            await self._set("control_mode", "choice", self._options_token(rt))
        await self._prices_step(report)
        await self._consumption_step(report)
        await self._request_plan()
        await self._loop()

    def _options_token(self, rt) -> str | None:
        """Szczegół wyboru: rekomendacja ścieżki, gdy już policzona (`recommended: …`); inaczej które
        sposoby sterowania są dostępne (`direct` tylko, gdy da się go zaoferować)."""
        rec = getattr(rt, "recommendation", None)
        if rec is not None:
            return ds.recommendation_text(rec)
        tokens = []
        if self._control_ready(rt):
            tokens.append("entities")
        if self._direct_report is not None and ds.offer_reason(self._direct_report, self._profiles) is None:
            tokens.append("direct")
        return f"options: {','.join(tokens)}" if tokens else None

    async def _control_offer(self, rt, report: Mapping) -> dict | None:
        """Oferta kroku `control_mode`: obie ścieżki z kodem opisu, `recommended` i `reason`.

        Rekomendacja z runtime (`async_update_recommendation`), a gdy jeszcze nie policzona — ta sama funkcja
        (`recommend`) na tym, co onboarding już wie. Bez sondy (integracja w HA) „bezpośrednio” jest możliwe po
        wyłączeniu integracji tylko wtedy, gdy TA integracja (wybranego profilu) trzyma falownik pod adresem
        lokalnym. Błąd liczenia oferty → bez oferty (zostaje sam szczegół kroku), onboarding biegnie dalej."""
        try:
            return await self._compute_offer(rt, report)
        except Exception as err:  # noqa: BLE001 — oferta nie psuje onboardingu
            _LOGGER.debug("Volcast onboarding: control offer failed (%s)", type(err).__name__)
            return None

    async def _compute_offer(self, rt, report: Mapping) -> dict:
        rep = self._direct_report
        clash: tuple[str, ...] = ()
        if rep is not None:
            clash = await ds.async_clash(self._hass, self._entry_id, rep.candidate.host)
        choice = getattr(rt, "choice", None)
        mapped = getattr(rt, "mapped", None) or {}
        domain = getattr(choice, "integration_domain", None)
        held = bool(domain) and any(isinstance(i, Mapping) and i.get("domain") == domain
                                    and isinstance(i.get("host"), str) and i["host"]
                                    for i in report.get("inverters") or ())
        held_by = choice.profile if rep is None and held else None
        rec = getattr(rt, "recommendation", None)
        if rec is None:
            try:
                rec = recommend(report, self._profiles, rep, ds.offer_reason(rep, self._profiles, clash), mapped,
                                clash, choice=choice)
            except Exception as err:  # noqa: BLE001 — bez rekomendacji oferta i tak wychodzi
                _LOGGER.debug("Volcast onboarding: recommendation failed (%s)", type(err).__name__)
        return ds.control_offer(rec, ds.entities_option(choice, mapped),
                                ds.direct_option(rep, self._profiles, clash, held_by=held_by))

    async def _direct_search(self) -> None:
        """Sonda sieci: na starcie tylko bez integracji falownika w HA (inaczej to byłby drugi klient); po
        wyborze `direct` także z nią — sonda pomija adresy innych integracji bez wysłania ramki."""
        self._searched = True
        if self._search is None:
            return
        try:
            reports = await self._search()
        except Exception as err:  # noqa: BLE001 — sonda nigdy nie psuje onboardingu
            _LOGGER.debug("Volcast onboarding: inverter search failed (%s)", type(err).__name__)
            return
        hits = ds.found(reports or [])
        self._direct_report = hits[0] if hits else None

    async def _inverter_step(self, report: dict) -> None:
        inverters = report.get("inverters") or []
        self._inverter_integration = bool(inverters)
        if not inverters:
            await self._direct_search()
            if self._direct_report is not None:
                await self._set("inverter", "done", f"{ds.brand_model(self._direct_report, self._profiles)} (direct)")
                return
            await self._set("inverter", "error", "no inverter integration found")
            return
        inv = inverters[0]
        dev = (inv.get("devices") or [{}])[0]
        label = " ".join(x for x in (dev.get("manufacturer"), dev.get("model")) if x) or inv.get("domain")
        rt = self._runtime()
        if ds.found(getattr(rt, "last_probe", None) or []) and readings_missing(getattr(rt, "telemetry", None)):
            # Falownik odpowiada w sieci, ale port trzyma integracja rdzeniowa i nic z niej nie wychodzi.
            await self._set("inverter", "done", READ_ONLY_NO_READINGS)
            return
        # Adres lokalny falownika zostaje w Home Assistant — chmura go nie odczytuje
        # (pole postępu jest tylko wyświetlane), więc nie ma powodu go wysyłać.
        await self._set("inverter", "done", label)

    async def _prices_step(self, report: dict) -> None:
        current = self._options().get(OPT_PRICE_BUY)
        if self._usable_price(current):
            await self._set("prices", "done", current)
            return
        if "price_source" in self._applied():
            await self._set("prices", "done")            # zastosowany wcześniej; opcje wygrywają
            return
        for cand in report.get("price_entities") or []:
            eid = cand.get("entity_id") if isinstance(cand, dict) else None
            if self._usable_price(eid):
                self._price_candidate = eid
                platform = cand.get("platform")
                await self._set("prices", "choice", f"{platform} ({eid})" if platform else eid)
                return
        # Brak encji albo żadna nie daje dziś pełnej serii — właściciel wybiera inne źródło.
        await self._set("prices", "choice", NONE_FOUND)

    async def _consumption_step(self, report: dict) -> None:
        if not self._options().get(OPT_LOAD_ENERGY):
            candidate = None
            if _AUTO_LOAD not in self._applied():
                # Tylko z własnych encji zmapowanego falownika; po wyczyszczeniu przez
                # właściciela (restart w oknie sesji) nie wybieramy ponownie.
                own = getattr(self._runtime(), "inverter_entities", None) or ()
                candidate = house_load_candidate(report.get("energy_sensors"), inverter_entities=own)
            if candidate is None:
                await self._set("consumption", "choice", "no house energy sensor selected")
                return
            # Zmiana samego czujnika zużycia nie przeładowuje wpisu (słuchacz aktualizacji).
            self._commit_choices({OPT_LOAD_ENERGY: candidate}, {_AUTO_LOAD: candidate})
        await self._set("consumption", "active")
        try:
            imported = await self._import_history()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Volcast onboarding: history import failed (%s)", type(err).__name__)
            imported = None
        if not isinstance(imported, dict):
            await self._set("consumption", "error", "history import failed")
            return
        load = self._options().get(OPT_LOAD_ENERGY)
        days = next((s.get("days_of_statistics") for s in report.get("energy_sensors") or []
                     if isinstance(s, dict) and s.get("entity_id") == load), None)
        ok = isinstance(days, int) and not isinstance(days, bool) and days > 0
        await self._set("consumption", "done", f"{days} days of history" if ok else "imported")

    async def _request_plan(self) -> None:
        self._plan_retry_at = None
        if readings_missing(getattr(self._runtime(), "telemetry", None)):
            # Planer bez odczytów odmówi — nie palimy próby; ponowienie po pierwszej udanej telemetrii.
            self.first_plan_outcome = "no_readings"
            _LOGGER.info("Volcast onboarding: first plan no_readings")
            await self._set("first_plan", "active", NO_READINGS)
            return
        self._plan_attempts += 1
        if self._state("first_plan") == "pending":
            await self._set("first_plan", "active")
        res = await self._client.async_request_plan(self._session)
        outcome = self.first_plan_outcome = plan_outcome(res)
        _LOGGER.info("Volcast onboarding: first plan %s", outcome)
        can_retry = self._plan_attempts < _PLAN_ATTEMPTS
        if outcome == "ok":
            await self._set("first_plan", "done", f"{res['slots_count']} slots")
            # Świeży runtime: wpis mógł się w międzyczasie przeładować.
            fetcher = getattr(self._runtime(), "fetcher", None)
            if fetcher is not None:
                await fetcher.async_refresh()
        elif outcome == "cooldown":
            if can_retry:
                self._plan_retry_at = self._utcnow() + timedelta(seconds=_PLAN_RETRY_S)
                await self._set("first_plan", "active", QUEUED)
            else:
                await self._set("first_plan", "done", QUEUED)
        elif outcome == "skipped":
            await self._set("first_plan", "done", QUEUED)
        else:
            await self._set("first_plan", "error", PLAN_UNAVAILABLE)
            if outcome == "failed:unreachable" and can_retry:
                self._plan_retry_at = self._utcnow() + timedelta(seconds=_PLAN_RETRY_S)

    async def _loop(self) -> None:
        while self._live():
            if self._dirty and (self._repost_at is None or self._utcnow() >= self._repost_at):
                await self._publish()
            if self._plan_retry_at is not None and self._utcnow() >= self._plan_retry_at:
                await self._request_plan()
            waiting = "choice" in (self._state("control_mode"), self._state("prices"))
            if not waiting and self._plan_retry_at is None and not self._dirty:
                return
            if waiting:
                result = await self._client.async_poll(self._session)
                if result.status == "consumed":
                    await self._apply(result.choices)
                elif result.status in _GONE:
                    return
            await self._sleep(self._choice_poll_s)

    async def _direct_reason(self) -> str | None:
        rep = self._direct_report
        if rep is None:
            return ds.CONFLICT if self._inverter_integration else ds.NOT_FOUND
        clash = await ds.async_clash(self._hass, self._entry_id, rep.candidate.host)
        return ds.offer_reason(rep, self._profiles, clash)

    def _already(self, mode: str, rt) -> bool:
        """Stan sterowania już taki, jak wybrany — np. plan (`control.path`) zastosował tę ścieżkę po starcie
        sesji (krok był w stanie `choice`, więc wcześniej jej nie było)."""
        plan_only = _plan_only(rt)
        if mode == _PLAN_ONLY:
            return plan_only
        return self._options().get(OPT_CONTROL_MODE) == mode and not plan_only

    async def _control_choice(self, mode: str, rt, patch: dict) -> tuple[str, str | None, bool] | None:
        """Zastosuj zdalny wybór sposobu sterowania: (stan kroku, szczegół, czy kreator zastosował ścieżkę);
        None = bez zmiany (odmowa `direct` z powodu kolizji, a stan kolizji się nie zmienił)."""
        if self._already(mode, rt):
            self._refused = None
            return "done", mode, False            # drugi kanał tej samej decyzji — bez wywołań
        if mode == CONTROL_MODE_ENTITIES:
            if not self._control_ready(rt):
                return "error", NOT_READY, False
            patch.update(entity_mode_options(rt.choice))
            await self._leave_plan_only(rt)
            return "done", CONTROL_MODE_ENTITIES, True
        if mode == _PLAN_ONLY:
            # Własny sterownik: ta sama obsługa co `path: plan_only` z chmury (tryb „tylko plan”, sterowanie wyłączone).
            if await rt.async_apply_path_choice(_PLAN_ONLY) != "applied":
                return "error", NOT_READY, False
            return "done", _PLAN_ONLY, True
        if self._refused is not None:
            if ds.holders_state(self._hass) == self._refused:
                return None                       # nic się nie zmieniło — bez ponownej sondy i publikacji
            self._refused = None
            if self._direct_report is None:
                self._searched = False            # integracja wyłączona/usunięta: wyszukujemy jeszcze raz
        if self._direct_report is None and not self._searched:
            await self._direct_search()           # wybór właściciela: wyszukujemy teraz (raz na stan kolizji)
        if self._already(mode, rt):
            return "done", mode, False            # plan zastosował ją w trakcie wyszukiwania
        reason = await self._direct_reason()
        if reason in (ds.CONFLICT, ds.IN_USE):
            # Falownik trzyma inna integracja: wybór zostaje otwarty (ta sama oferta), właściciel może wyłączyć
            # integrację i wybrać ponownie albo wybrać inną ścieżkę. Bez zmiany opcji i bez znacznika.
            self._refused = ds.holders_state(self._hass)
            return "choice", ds.REMOTE_TEXT[reason], False
        if reason is not None:
            return "error", ds.REMOTE_TEXT[reason], False
        patch.update({OPT_CONTROL_MODE: CONTROL_MODE_DIRECT,
                      OPT_DIRECT_TARGET: ds.target_from_report(self._direct_report), OPT_DIRECT_TRIAL: None})
        await self._leave_plan_only(rt)
        return "done", CONTROL_MODE_DIRECT, True

    @staticmethod
    async def _leave_plan_only(rt) -> None:
        """Wybór sterowania (encje / bezpośrednio) kończy tryb „tylko plan” — ta sama ścieżka co wybór w opcjach
        (`async_leave_plan_only`: tryb wyłączony i zapisany, drabina od szczebla startowego). Kanał planu tego nie
        zrobi: jego kopię tej samej decyzji znacznik kreatora pomija."""
        if _plan_only(rt):
            await rt.async_leave_plan_only()

    async def _apply(self, choices: Mapping[str, str]) -> None:
        patch: dict[str, str] = {}
        applied: dict[str, str] = {}
        marks: dict[str, dict] = {}
        steps: list[tuple[str, str, str | None]] = []
        mode = choices.get("control_mode")
        executor = None
        if mode in (CONTROL_MODE_ENTITIES, CONTROL_MODE_DIRECT, _PLAN_ONLY) and self._state("control_mode") == "choice":
            rt = self._runtime()
            outcome = await self._control_choice(mode, rt, patch) if rt is not None else None
            if outcome is not None:               # None: wpis się przeładowuje albo odmowa bez zmian — następne odpytanie
                state, detail, by_wizard = outcome
                steps.append(("control_mode", state, detail))
                if state == "done":
                    applied["control_mode"] = mode
                if by_wizard:
                    # Znacznik dla planu: ta sama ścieżka z `control.path` będzie bez działania (kontrakt §4).
                    executor = getattr(self._runtime() or rt, "executor", None)
                    meta = getattr(executor, "control_meta", None)
                    marks["control_mode"] = wizard_mark(meta if isinstance(meta, dict) else None)
                    if isinstance(meta, dict):
                        meta["path_done"] = mode
        source = choices.get("price_source")
        if isinstance(source, str) and self._state("prices") == "choice":
            if source == "ha":
                if self._price_candidate and self._usable_price(self._price_candidate):
                    patch[OPT_PRICE_BUY] = self._price_candidate
                    applied["price_source"] = source
                    steps.append(("prices", "done", self._price_candidate))
                else:
                    # Wybór „ceny z HA", a encja (już) nie daje pełnej serii — nie udajemy sukcesu.
                    steps.append(("prices", "error", NONE_FOUND))
            elif _PRICE_SOURCE.fullmatch(source):
                steps.append(("prices", "done", None))   # źródło ustawia aplikacja/chmura
        self._commit_choices(patch, applied, marks)
        save = getattr(executor, "async_save_control_meta", None) if marks else None
        if save is not None:
            # Po zapisie wpisu (znacznik już widoczny dla planu); `path_done` przeżywa przeładowanie.
            await save()
        for key, state, detail in steps:
            await self._set(key, state, detail)
