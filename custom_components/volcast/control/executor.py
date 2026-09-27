"""Wykonawca planu w trybie encji — jedyny pisarz do falownika w tej integracji.

Cykl co 60 s (i po każdej zmianie planu/zgody/przełącznika): odczyt encji z jednostkami
→ `decide_cycle` (czysty rdzeń) → zapis usługami tylko przy statusie WRITE. Stan trwały
(plan, zgoda, przełącznik, własność, migawka) w `ControlStore`. Restart/reload nie
przywraca trybu bazowego — robi to wyłącznie utrata prawa (zgoda False, przełącznik OFF,
tryb wyłączony w opcjach), i tylko gdy to my zmienialiśmy nastawy.

Zasady wykonania:
* każdy zapis (cykl i powrót do trybu bazowego) idzie przez wykonawcę grupowego
  (`async_run_group_writes`) — tryb i moc razem albo wcale, z cofnięciem pierwszego
  członka, gdy drugi się nie zapisał;
* jeden cykl naraz: tik zgłoszony w trakcie cyklu nie startuje drugiego zapisu, tylko
  prosi o powtórkę zaraz po bieżącym;
* przed pierwszym zapisem migawka nastaw do trybu bazowego musi być pełna i zapisana —
  inaczej żadnego zapisu (klucza bez migawki nigdy byśmy nie przywrócili);
* powrót do trybu bazowego: najpierw sam tryb bazowy (neutralny, nie potrzebuje
  warunków — hamulec właściciela nie może zależeć od innej encji), potem każda
  pozostała nastawa z migawki niezależnie; własność zostaje, dopóki wszystko nie dojdzie,
  a każdy tik ponawia brakujące. Czytelnej opcji trybu spoza profilu nie nadpisujemy
  (ktoś inny wybrał tryb — zostaje jego);
* własność i migawka są związane z profilem i encją trybu (`owner`); migawki innego
  falownika albo mapowania nie wpisujemy w nowe encje;
* zatrzymany wykonawca nie zaczyna zapisów i nie nadpisuje magazynu (poza powrotem do
  trybu bazowego w toku); nieczytelny magazyn wyłącza wykonawcę — start ze stanem
  domyślnym zgubiłby własność i nigdy nie przywrócił trybu bazowego;
* w logach tylko klucze parametrów i nazwy klas wyjątków — nigdy `entity_id` ani treść
  wyjątku (bywa w nich numer seryjny albo adres hosta);
* wejścia (plan, zgoda, przełącznik) nie rzucają: błąd magazynu zostawia stan w pamięci
  i trafia do logu.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Callable, Mapping
from zoneinfo import ZoneInfo

import homeassistant.util.dt as dt_util
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from ..const import (CONTROL_MODE_ENTITIES, DOMAIN, ERROR_ISSUE_AFTER, EXECUTOR_INTERVAL_S,
                     OPT_CONTROL_MODE, SIGNAL_CONTROL_UPDATED, STOP_WRITE_TIMEOUT_S)
from ..core.control.baseline import baseline_params, needs_restore, snapshot_missing, take_snapshot
from ..core.control.cycle import (BLOCKED, ERROR, WRITE, ControlMemory, CycleDecision, EntityContext,
                                  Gates, Limits, Telemetry, commit, decide_cycle, same_value)
from ..core.control.entity_fit import control_writes, fit_params
from ..core.control.group_writes import GROUP_KEYS, GroupReport, async_run_group_writes, order_group
from ..core.control.readings import RawState, normalize_readings
from ..core.control.select import ProfileChoice, control_verified
from ..core.engines.time_window import compress
from ..core.slot import InvalidSchedule, Schedule, parse_schedule
from .store import ControlState, ControlStore

_LOGGER = logging.getLogger(__name__)

RESTORE = "restore"
_NO_READING = ("unavailable", "unknown", "")
# Ile razy z rzędu cykl powtarza się po tikach zgłoszonych w jego trakcie.
_MAX_RERUNS = 2
# W prawdziwym HA stała z rejestru zgłoszeń; atrapa testowa jej nie ma.
_WARNING = getattr(getattr(ir, "IssueSeverity", None), "WARNING", "warning")


@dataclass(frozen=True)
class _Reading:
    """Odczyt encji jednego cyklu."""
    readings: dict[str, float | str]      # znormalizowane (migawka, powrót do bazowego)
    raw_mode: str | None                  # surowa opcja trybu (obca opcja ≠ brak odczytu)
    units: dict[str, str | None]
    attrs: dict[str, dict]                # atrybuty encji, także `options` wyboru trybu
    soc_age_s: float

    @property
    def foreign_mode(self) -> bool:
        """Czytelna opcja trybu, której profil nie zna."""
        return self.raw_mode is not None and self.raw_mode not in _NO_READING \
            and "mode" not in self.readings

    def for_cycle(self) -> dict[str, float | str]:
        """Odczyty dla cyklu: tryb jako surowa opcja — cykl sam rozpozna obcą."""
        out = dict(self.readings)
        if self.raw_mode is not None:
            out["mode"] = self.raw_mode
        return out


class VolcastExecutor:
    def __init__(self, hass, entry, *, choice: ProfileChoice | None, mapped: Mapping[str, str],
                 rated_power_w: float | None, store: ControlStore, writer,
                 clock: Callable[[], float] = time.monotonic, utcnow=dt_util.utcnow,
                 stop_timeout_s: float = STOP_WRITE_TIMEOUT_S) -> None:
        self._hass = hass
        self._entry = entry
        self._choice = choice
        self._profile = choice.profile if choice else None
        self._domain = choice.integration_domain if choice else None
        self._mapped = dict(mapped) if self._domain else {}
        self._rated = rated_power_w
        self._store = store
        self._writer = writer
        self._clock = clock
        self._utcnow = utcnow
        self._stop_timeout_s = stop_timeout_s
        self._memory = ControlMemory.for_profile(self._profile) if self._profile else None
        self._state = ControlState()
        self.schedule: Schedule | None = None
        self.last_decision: CycleDecision | None = None
        self.tou_preview: dict | None = None
        self.foreign_changes: list[dict] = []
        self._prev_soc: tuple[float, float] | None = None
        self._errors = 0
        self._logged: tuple[str, str] | None = None
        self._lock = asyncio.Lock()
        self._rerun = False
        self._stopped = False
        self._started = False
        self._restore_failed: tuple[str, ...] | None = None   # ostatnio zalogowane (bez powtórek co tik)
        self._disabled = False
        self._unsub: list[Callable[[], None]] = []

    # ── stan dla encji i telemetrii ───────────────────────────────────────
    @property
    def raw_plan(self) -> dict | None:
        return self._state.plan_raw

    @property
    def consent(self) -> bool | None:
        return self._state.consent

    @property
    def local_switch(self) -> bool:
        return self._state.local_switch

    @property
    def paused(self) -> bool:
        return bool(self._memory and self._memory.paused_until is not None
                    and self._clock() < self._memory.paused_until)

    @property
    def history_imported_at(self) -> str | None:
        return self._state.history_imported_at

    async def async_mark_history_imported(self, when_iso: str) -> None:
        # Ten sam obiekt stanu i ten sam magazyn co reszta wykonawcy — dwa niezależne
        # zapisy do jednego `Store` nadpisywałyby sobie pola. Wykonawca zatrzymany albo
        # wyłączony nie pisze do magazynu (następca po przeładowaniu ma własny stan).
        self._state.history_imported_at = when_iso
        if self._disabled or self._stopped:
            return
        await self._store.async_save(self._state)

    def exec_summary(self) -> dict:
        d = self.last_decision
        out = {"decision": d.summary() if d else None, "consent": self._state.consent,
               "local_switch": self._state.local_switch, "paused": self.paused,
               "foreign_changes": len(self.foreign_changes),
               "profile": self._profile.id if self._profile else None}
        if self.tou_preview is not None:
            out["tou_preview"] = {k: self.tou_preview.get(k) for k in ("lost_value_pln", "merges", "error")
                                  if k in self.tou_preview}
        return out

    # ── cykl życia ────────────────────────────────────────────────────────
    async def async_start(self) -> None:
        if self._started:
            return
        self._started = True
        try:
            self._state = await self._store.async_load()
        except Exception as err:  # noqa: BLE001 — zły albo przyszły format magazynu
            self._disabled = True
            self.last_decision = CycleDecision(ERROR, "store_unreadable")
            _LOGGER.error("Volcast control disabled: saved control state could not be read (%s)",
                          type(err).__name__)
            return
        if self._drop_foreign_owner():
            await self._async_save("control state")
        if self._state.plan_raw is not None:
            try:
                self.schedule = parse_schedule(self._state.plan_raw)
            except InvalidSchedule:
                self._state.plan_raw = None      # zły plan z magazynu = brak planu
        self._unsub.append(async_track_time_interval(
            self._hass, self._async_timer, timedelta(seconds=EXECUTOR_INTERVAL_S)))

    async def async_stop(self) -> None:
        """Bez przywracania; czeka na zapis w toku najwyżej `stop_timeout_s`."""
        self._stopped = True
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()
        if not self._lock.locked():
            return
        try:
            await asyncio.wait_for(self._lock.acquire(), self._stop_timeout_s)
        except asyncio.TimeoutError:
            _LOGGER.error("Volcast control: write still in progress at stop — giving up waiting")
            return
        self._lock.release()

    def _owner(self) -> dict:
        return {"profile": self._profile.id if self._profile else "", "domain": self._domain or "",
                "mode_entity": self._mapped.get("mode", "")}

    def _drop_foreign_owner(self) -> bool:
        """Migawka z innego profilu albo innej encji trybu nie trafia w nowe encje.

        Własność bez powiązania (zapisana przed jego wprowadzeniem) uznajemy za własną.
        """
        if not self._state.owned or not self._state.owner or self._state.owner == self._owner():
            return False
        _LOGGER.warning("Volcast control: saved baseline settings belong to a different inverter "
                        "profile or mode entity — not reusing them; check the inverter settings")
        self._state.owned = False
        self._state.snapshot = {}
        self._state.owner = {}
        return True

    async def _async_timer(self, _now=None) -> None:
        await self.async_tick()

    # ── wejścia ───────────────────────────────────────────────────────────
    async def async_on_plan(self, raw: dict, schedule: Schedule) -> None:
        self._state.plan_raw = raw
        self.schedule = schedule
        await self._async_save("plan")
        self._notify()

    async def async_set_consent(self, value: bool) -> None:
        if not isinstance(value, bool) or value == self._state.consent:
            return       # wartość innego typu nie zmienia stanu
            return
        self._state.consent = value
        _LOGGER.warning("Volcast account consent for inverter control: %s",
                        "granted" if value else "withdrawn")
        await self._async_save("consent")
        self._notify()

    async def async_on_auth_failure(self, count: int) -> None:
        if count >= 2:
            await self.async_set_consent(False)

    async def async_set_local_switch(self, on: bool) -> None:
        self._state.local_switch = bool(on)
        await self._async_save("local switch")
        self._notify()

    async def async_restore_now(self) -> None:
        """Usuwanie wpisu: przywróć tryb bazowy, jeśli to my zmienialiśmy nastawy."""
        if self._disabled:
            return
        try:
            async with self._lock:
                if self._state.owned and self._profile and self._domain and self._memory:
                    await self._restore(self._read(self._utcnow()))
        except Exception as err:  # noqa: BLE001 — usuwanie wpisu nie może się wywrócić
            _LOGGER.error("Volcast control: return to the baseline mode failed (%s)", type(err).__name__)

    async def _async_save(self, what: str, *, force: bool = False) -> bool:
        """Zapis stanu; wyłączony wykonawca nigdy, zatrzymany tylko z `force` (powrót w toku)."""
        if self._disabled or (self._stopped and not force):
            return False
        try:
            await self._store.async_save(self._state)
        except Exception as err:  # noqa: BLE001 — stan zostaje w pamięci; zapis przy następnej zmianie
            _LOGGER.warning("Volcast control: could not save the %s (%s)", what, type(err).__name__)
            return False
        return True

    # ── cykl ──────────────────────────────────────────────────────────────
    async def async_tick(self) -> None:
        if self._stopped or self._disabled:
            return
        if self._lock.locked():
            # Zapis w toku (także przywracanie przy usuwaniu): drugi cykl nie startuje
            # równolegle — bieżący powtórzy się zaraz po sobie.
            self._rerun = True
            return
        async with self._lock:
            for _ in range(1 + _MAX_RERUNS):
                self._rerun = False
                try:
                    await self._tick_locked()
                except Exception as err:  # noqa: BLE001 — pętla nie może umrzeć
                    _LOGGER.error("Volcast control cycle failed (%s)", type(err).__name__)
                    self.last_decision = CycleDecision(ERROR, "exception:tick")
                    self._count(self.last_decision)
                if not self._rerun or self._stopped:
                    break
        self._notify()

    def _read(self, now_utc) -> _Reading:
        raw: dict[str, RawState] = {}
        units: dict[str, str | None] = {}
        attrs: dict[str, dict] = {}
        soc_state = None
        raw_mode = None
        for key, eid in self._mapped.items():
            st = self._hass.states.get(eid)
            if st is None:
                continue
            unit = st.attributes.get("unit_of_measurement")
            raw[key], units[key] = RawState(st.state, unit), unit
            attrs[eid] = dict(st.attributes)
            if key == "mode" and isinstance(st.state, str):
                raw_mode = st.state
            if key == "soc":
                soc_state = st
        readings = normalize_readings(raw, self._profile, self._domain) if self._domain else {}
        return _Reading(readings, raw_mode, units, attrs, self._age(soc_state, now_utc))

    def _age(self, st, now_utc) -> float:
        if st is None:
            return math.inf
        ts = getattr(st, "last_reported", None) or getattr(st, "last_updated", None)
        if ts is None:
            return math.inf
        age = (now_utc - ts).total_seconds()
        return 0.0 if -5.0 < age < 0.0 else age

    async def _tick_locked(self) -> None:
        now_mono, now_utc = self._clock(), self._utcnow()
        self._update_tou_preview(now_utc)
        if self._profile is None or self._memory is None:
            self.last_decision = CycleDecision("idle", "no_profile")
            return
        rd = self._read(now_utc)
        gates = Gates(consent=self._state.consent, local_switch=self._state.local_switch,
                      control_mode=self._entry.options.get(OPT_CONTROL_MODE),
                      verified=control_verified(self._profile, self._domain))
        if needs_restore(owned=self._state.owned, consent=gates.consent,
                         local_switch=gates.local_switch, control_mode=gates.control_mode) \
                and not self.paused and self._domain:
            await self._restore(rd)
            return
        readings = rd.readings
        soc = readings.get("soc")
        soc = soc if isinstance(soc, float) else None
        temp = readings.get("battery_temp_c")
        prev_soc, gap = (self._prev_soc[0], now_mono - self._prev_soc[1]) if self._prev_soc else (None, None)
        decision = decide_cycle(
            profile=self._profile, schedule=self.schedule, now_utc=now_utc, now_mono=now_mono,
            tele=Telemetry(soc=soc, soc_age_s=rd.soc_age_s,
                           battery_temp_c=temp if isinstance(temp, float) else None,
                           previous_soc=prev_soc, previous_soc_gap_s=gap),
            limits=Limits(rated_power_w=float(self._rated or 0.0)),
            ents=EntityContext(domain=self._domain or "", mapped=self._mapped, units=rd.units,
                               attrs=rd.attrs, readings=rd.for_cycle()),
            gates=gates, memory=self._memory)
        if soc is not None:
            self._prev_soc = (soc, now_mono)
        if decision.status == WRITE and not self._state.owned:
            decision = await self._async_take_ownership(decision, readings)
            if decision.status == WRITE and not self._gates_open():
                # Zgoda, przełącznik albo zatrzymanie zmieniły się w trakcie zapisu migawki.
                decision = replace(decision, status=BLOCKED, reason="gates_changed")
        if decision.status == WRITE:
            report = await async_run_group_writes(
                decision.writes, self._writer.async_write, restore=decision.restore,
                ambiguous_safe=decision.restore_ambiguous_safe, on_exception=self._log_write_exception)
            commit(decision, report, self._memory, now_mono)
            self._log_report(decision, report)
        self.last_decision = decision
        self._count(decision)

    async def _async_take_ownership(self, decision: CycleDecision,
                                    readings: Mapping[str, float | str]) -> CycleDecision:
        """Migawka nastaw PRZED pierwszym zapisem; niepełna albo niezapisana = żadnego zapisu."""
        snapshot = take_snapshot(readings)
        missing = snapshot_missing(snapshot, self._mapped)
        if missing:
            return replace(decision, status=BLOCKED, reason="baseline_unknown", unmapped=missing)
        self._state.snapshot = snapshot
        self._state.owned = True
        self._state.owner = self._owner()
        if not await self._async_save("baseline snapshot"):
            # Bez trwałej migawki restart nie wiedziałby, co przywrócić — nie piszemy.
            self._state.owned = False
            self._state.snapshot = {}
            self._state.owner = {}
            return replace(decision, status=ERROR, reason="store_failed")
        return decision

    def _gates_open(self) -> bool:
        return (self._state.consent is True and self._state.local_switch and not self._stopped
                and self._entry.options.get(OPT_CONTROL_MODE) == CONTROL_MODE_ENTITIES)

    async def _restore(self, rd: _Reading) -> None:
        """Powrót do trybu bazowego: najpierw sam tryb, potem każda pozostała nastawa.

        Tryb bazowy jest neutralny i nie potrzebuje warunków, więc nie czeka na żadną
        inną encję. Obie części idą przez wykonawcę grupowego; własność zostaje, dopóki
        wszystko nie dojdzie — każdy tik ponawia tylko to, czego falownik jeszcze nie ma.
        """
        now_mono = self._clock()
        params = baseline_params(self._profile, self._state.snapshot)
        fitted, _, unfit = fit_params(params, self._profile, self._domain, self._mapped, rd.units, rd.attrs)
        target = fitted.flatten()
        readings = rd.readings
        # To, co falownik już ma, nie jedzie (NVM) — ta sama zasada co w cyklu.
        keys = [k for k, v in target.items()
                if k not in unfit and not (k in readings and same_value(readings[k], v))]
        mode_kept = rd.foreign_mode and "mode" in keys
        if mode_kept:
            keys.remove("mode")          # ktoś wybrał tryb spoza profilu — zostaje jego
        group_writes, _ = control_writes(fitted, self._profile, self._domain, self._mapped,
                                         keys=[k for k in keys if k in GROUP_KEYS], units=rd.units)
        rest_writes, _ = control_writes(fitted, self._profile, self._domain, self._mapped,
                                        keys=[k for k in keys if k not in GROUP_KEYS], units=rd.units)
        # Moc (gdyby profil ją kiedyś miał w stanie bazowym) po trybie: tryb bazowy ją ignoruje.
        reports = []
        for writes in (order_group(group_writes, power_first=False), rest_writes):
            if writes:
                reports.append(await async_run_group_writes(writes, self._writer.async_write,
                                                            on_exception=self._log_write_exception))
        self._account_restore(target, reports, now_mono)
        writes = [*group_writes, *rest_writes]
        failed = [k for r in reports for k in (*r.failed, *r.unsupported, *r.restore_failed)]
        if failed or any(r.group_skipped for r in reports):
            self.last_decision = CycleDecision(ERROR, "restore_failed", writes=writes, flat=target)
            if tuple(failed) != self._restore_failed:
                _LOGGER.warning("Volcast control: return to the baseline incomplete (not applied: %s) "
                                "— retrying every cycle", failed)
            self._restore_failed = tuple(failed)
            self._count(self.last_decision)
            return
        self._restore_failed = None
        lost = [*snapshot_missing(self._state.snapshot, self._mapped), *unfit]
        if lost:
            _LOGGER.warning("Volcast control: could not return %s to the value from before control "
                            "(no saved value or outside the entity range) — check them on the inverter",
                            lost)
        self._state.owned = False
        self._state.snapshot = {}
        self._state.owner = {}
        self._memory.last_written.clear()
        await self._async_save("baseline state", force=True)
        self.last_decision = CycleDecision(RESTORE, "baseline_mode_kept" if mode_kept else "baseline",
                                           writes=writes, flat=target, takeover=mode_kept)
        if mode_kept:
            _LOGGER.warning("Volcast control: settings returned to their baseline; the inverter mode "
                            "was changed outside Volcast and is left as it is")
        else:
            _LOGGER.warning("Volcast control: inverter returned to its baseline mode")
        self._count(self.last_decision)

    def _account_restore(self, target: Mapping[str, float | str], reports: list[GroupReport],
                         now_mono: float) -> None:
        """Pamięć po powrocie: I-6 od zapisu, niepewne klucze, kierunek po zmianie trybu."""
        memory = self._memory
        for report in reports:
            memory.throttle.record(target, report.written, now_mono)
            memory.uncertain -= set(report.written)
            memory.uncertain |= set(report.ambiguous)
            memory.throttle.mark_unknown(report.ambiguous, now_mono)
            # Tryb neutralny: ostatni kierunek na falowniku nieznany — następna zmiana
            # kierunkowa liczy się w budżecie I-8, w którąkolwiek stronę.
            if "mode" in report.written or "mode" in report.ambiguous:
                memory.limiter.mark_unknown()

    def _log_report(self, decision: CycleDecision, report: GroupReport) -> None:
        """Wynik zapisu grupowego — każdy przypadek osobnym komunikatem, same klucze."""
        failed = list(report.failed)
        if report.restored:
            _LOGGER.warning("Volcast control: %s failed — %s returned to its previous value",
                            failed, report.restored)
        for key in report.restore_failed:
            if key in decision.restore:
                _LOGGER.error("Volcast control: %s failed and returning %s to its previous value "
                              "also failed — the group waits before the next attempt", failed, key)
            else:
                # Świadomie bez cofnięcia (nie było bezpiecznej poprzedniej wartości albo budżet
                # zmian kierunku mieścił tylko jedną zmianę) — to nie jest nieudane cofnięcie.
                _LOGGER.warning("Volcast control: %s failed; %s stays at the new value — no return "
                                "to the previous value was planned for this change", failed, key)
        if report.restore_held:
            _LOGGER.warning("Volcast control: %s kept — the outcome of %s is unknown (it may have "
                            "been applied); the next cycle re-reads the inverter and corrects",
                            report.restore_held, failed)
        if report.ambiguous and not report.error:
            _LOGGER.info("Volcast control: uncertain write outcome for %s", report.ambiguous)
        if (failed or report.unsupported) and not report.error:
            _LOGGER.info("Volcast control: failed=%s unsupported=%s group_held=%s",
                         failed, report.unsupported, report.group_skipped)

    def _update_tou_preview(self, now_utc) -> None:
        if not (self._profile and self._profile.control_model == "time_window" and self.schedule
                and self._rated):
            return
        try:
            res = compress(self.schedule, now_utc, self._profile, soc_reserve=self.schedule.fallback.soc_reserve,
                           rated_power_w=float(self._rated), tz=ZoneInfo(self._hass.config.time_zone))
        except Exception as err:  # noqa: BLE001 — podgląd nigdy nie psuje cyklu
            self.tou_preview = {"error": type(err).__name__}
            return
        self.tou_preview = {
            "lost_value_pln": round(res.lost_value_pln, 2), "merges": len(res.merges),
            "programs": [{"start": f"{p.start_min // 60:02d}:{p.start_min % 60:02d}",
                          "power_w": round(p.power_w), "soc": round(p.soc), "grid_charge": p.grid_charge}
                         for p in res.programs]}

    def _count(self, d: CycleDecision) -> None:
        key = (d.status, d.reason)
        if key != self._logged:
            self._logged = key
            _LOGGER.info("Volcast control: %s (%s)", d.status, d.reason)
        issue = f"control_error_{self._entry.entry_id}"
        if d.status == ERROR:
            self._errors += 1
            if self._errors == ERROR_ISSUE_AFTER:
                ir.async_create_issue(self._hass, DOMAIN, issue, is_fixable=False, severity=_WARNING,
                                      translation_key="control_error")
        else:
            if self._errors >= ERROR_ISSUE_AFTER:
                ir.async_delete_issue(self._hass, DOMAIN, issue)
            self._errors = 0

    def _log_write_exception(self, key: str, err: BaseException) -> None:
        # Sam klucz i klasa — treść wyjątku bywa z adresem hosta albo numerem seryjnym.
        _LOGGER.warning("Volcast control: write of %s raised %s", key, type(err).__name__)

    def _notify(self) -> None:
        async_dispatcher_send(self._hass, SIGNAL_CONTROL_UPDATED.format(entry_id=self._entry.entry_id))
