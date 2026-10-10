"""Runner drabiny weryfikacji urządzenia w HA (maszyna stanów: `core/control/ladder.py`).

Krok drabiny (`async_step`) idzie po każdym cyklu wykonawcy (sygnał `SIGNAL_CONTROL_UPDATED`) i w chwili
`next_at` (koniec próby, koniec okna — `async_track_point_in_utc_time`):

* 1 identyfikacja — tryb bezpośredni: tożsamość urządzenia pod adresem potwierdzona w cyklu wykonawcy;
  encje: encja trybu z odczytem (szczebel trywialny);
* 2 odczyt — czytelny tryb z profilu i SoC;
* 3 próba bez zapisu — „co bym zapisał” z decyzji wykonawcy (każda nowa decyzja z zapisami raz), obcy
  zapis = stop: encje — zmiana stanu encji klucza zapisu z aktorem (użytkownik, automatyzacja), nie
  z naszym kontekstem; bezpośrednio — drugi klient na łączu (`ContentionMonitor`, `conn.conflict`) albo
  zmiana rejestru zapisu bez naszego zapisu;
* 4 zapis kontrolny — `executor.async_control_write` (ponowny zapis bieżącego trybu, odczyt zwrotny);
* 5 okno próbne — plan zastępczy wykonawcy (jeden slot ładowania z sieci `window_power_w`, ta sama
  ścieżka zapisu i `apply_guards`), pomiar mocy ładowania i SoC z odczytu po każdym cyklu; po oknie
  powrót do trybu bazowego.

Zgoda drabiny (szczeble 4–5) = `executor.verification_can_write()` i droga zapisu (`writing_supported`);
inaczej drabina czeka przed zapisem kontrolnym (`rung 4, waiting`). Profil okien czasowych kończy na
zapisie kontrolnym (okno próbne tylko dla modelu trybu i nastawy).

Migracja: pierwszy start po aktualizacji (brak rekordu), profil i wpis zweryfikowane, a magazyn pokazuje
sterowanie tym urządzeniem (`executor.verification_migration_ok`) → rekord `verified` z `migrated`.

Tryb „tylko plan” (`executor.plan_only`, wybór własnego sterownika): `async_park` po cichu odstawia
drabinę (niezweryfikowaną) do `idle` na szczeblu startowym — bez stopu, zgłoszenia i natychmiastowej
telemetrii (tylko sygnał zmiany stanu), z powrotem do trybu bazowego, jeśli trwało okno; zegar
anulowany, a kroki i zdarzenia (obce zapisy, zgoda, konflikty) są pomijane, dopóki tryb trwa.
Wybór Volcast (`async_restart`) rusza drabinę od szczebla startowego. Zweryfikowane urządzenie zostaje
zweryfikowane (nie pisze, więc nie ma czego odstawiać).

Każdy stop: jeden powrót (`executor.async_verification_restore`), zgłoszenie
`verification_stopped_<wpis>` (kod stopu jako parametr tekstu) i natychmiastowa telemetria (`on_urgent`).
Każda zmiana stanu: zapis rekordu w magazynie sterowania i sygnał `SIGNAL_CONTROL_STATE_UPDATED`.
W logach tylko kody — nigdy klucz urządzenia, encje ani adresy.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Awaitable, Callable

import homeassistant.util.dt as dt_util
from homeassistant.core import callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send

from ..const import (DIRECT_POLL_S, DOMAIN, EXECUTOR_INTERVAL_S, ISSUE_VERIFICATION_STOPPED,
                     SIGNAL_CONTROL_STATE_UPDATED, SIGNAL_CONTROL_UPDATED, VERIFY_TRIAL_HOURS, VERIFY_WINDOW_MAX_SOC, VERIFY_WINDOW_MIN, VERIFY_WINDOW_POWER_W)
from ..core.control.cycle import DRY_RUN, WRITE
from ..core.control.ladder import (RUNG_CONTROL_WRITE, RUNG_IDENTIFY, RUNG_READ, RUNG_TRIAL, RUNG_WINDOW,
                                   RUNNING, STOPPED, VERIFIED, WAITING, Ladder, LadderParams, device_key)
from ..core.control.select import control_verified
from ..core.engines.mode_setpoint import SLOT_POWER_KINDS
from ..core.profile import direct_verified
from ..core.slot import Action, Fallback, Schedule, Slot

_LOGGER = logging.getLogger(__name__)
_WARNING = getattr(getattr(ir, "IssueSeverity", None), "WARNING", "warning")
_NO_STATE = ("unavailable", "unknown", "")
SIGNAL_EVERY = timedelta(hours=1)


def default_params(profile) -> LadderParams:
    """Stałe drabiny z `const.py`, nadpisane opcjonalnym blokiem `verification` profilu."""
    base = LadderParams(VERIFY_TRIAL_HOURS, VERIFY_WINDOW_MIN, VERIFY_WINDOW_POWER_W,
                        window=window_capable(profile))
    raw = getattr(profile, "raw", None) or {}
    return base.with_overrides(raw.get("verification"))


def start_rung_for(profile, domain: str | None, *, direct: bool) -> int:
    """Profil (i jego droga zapisu) zweryfikowany → zapis kontrolny (4, bez próby), inaczej identyfikacja (1)."""
    ok = direct_verified(profile) if direct else control_verified(profile, domain)
    return RUNG_CONTROL_WRITE if ok else RUNG_IDENTIFY


def writing_supported(profile, kind: str) -> bool:
    """Czy drabina ma czym zrobić zapis kontrolny (szczebel 4): tryb i nastawa — zapisywalny klucz `mode`;
    okna czasowe — słowo SoC programu (tylko tryb bezpośredni). Okno próbne to osobny warunek
    (`window_capable`, parametr `window` drabiny)."""
    if profile is None:
        return False
    write = profile.raw.get("write") or {}
    if profile.control_model == "time_window":
        return kind == "direct" and ((write.get("tou_program") or {}).get("soc") or {}).get("addr") is not None
    return "mode" in write


def window_capable(profile) -> bool:
    """Okno próbne (szczebel 5): model trybu i nastawy z bezpiecznym wymuszonym ładowaniem z sieci
    (możliwość `force_charge_from_grid`) i nastawą mocy ze slotu. Bez tego drabina kończy na zapisie
    kontrolnym."""
    if profile is None or profile.control_model != "mode_setpoint":
        return False
    if (profile.raw.get("capabilities") or {}).get("force_charge_from_grid") is not True:
        return False
    try:
        return profile.intent("charge_grid")["power"] in SLOT_POWER_KINDS
    except (KeyError, TypeError, ValueError):
        return False


def window_schedule(now, *, minutes: int, power_w: int, base: Schedule | None) -> Schedule:
    """Plan zastępczy okna: jeden slot ładowania z sieci; rezerwa SoC z planu chmury (bez planu — domyślna)."""
    fallback = base.fallback if base is not None else Fallback()
    slot = Slot(start=now, end=now + timedelta(minutes=minutes), action=Action.CHARGE, charge_source="grid",
                power_w=float(power_w))
    return Schedule("verification-window", now, (slot,), fallback, True)


def would_write_flat(decision) -> dict | None:
    """Decyzja z zapisami (na sucho albo wykonana) — to, co byśmy zapisali; inaczej None."""
    if decision is None or decision.status not in (DRY_RUN, WRITE) or not decision.writes:
        return None
    return dict(decision.flat or {})


def _number(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


class VerificationRunner:
    def __init__(self, hass, entry, executor, *, params: LadderParams, start_rung: int, salt: bytes,
                 on_urgent: Callable[[], Awaitable[Any]] | None = None, utcnow=dt_util.utcnow,
                 track_point: Callable | None = None) -> None:
        self._hass = hass
        self._entry = entry
        self._ex = executor
        self._params = params
        self._start = start_rung
        self._salt = bytes(salt)
        self._on_urgent = on_urgent
        self._utcnow = utcnow
        self._track_point = track_point
        self.ladder: Ladder | None = None
        self._unsubs: list[Callable[[], None]] = []
        self._timer: Callable[[], None] | None = None
        self._timer_at = None
        self._busy = False
        self._again = False
        self._stopped = False
        self._last_would: dict | None = None
        self._seen: dict | None = None               # rejestry zapisu z poprzedniego kroku (bezpośrednio)
        self._seen_write_end: float | None = None
        self._signalled_at = None                    # ostatni sygnał zmiany stanu (limit dla samego postępu)

    # ── odczyt dla wykonawcy i telemetrii ──

    @property
    def _issue_id(self) -> str:
        return f"{ISSUE_VERIFICATION_STOPPED}_{self._entry.entry_id}"

    def _key(self) -> str | None:
        identity = getattr(self._ex.io, "identity", lambda: None)()
        return device_key(self._salt, identity) if identity else None

    def plan_allowed(self) -> bool:
        """Plan steruje tylko zweryfikowanym urządzeniem — tym, które jest pod adresem teraz."""
        lad = self.ladder
        return lad is not None and lad.verified and lad.device_key == self._key()

    def payload(self) -> dict | None:
        """Blok `driver.control.verification` (None = brak identyfikacji urządzenia)."""
        return self.ladder.to_payload() if self.ladder is not None else None

    # ── cykl życia ──

    async def async_start(self) -> None:
        self._ex.verification = self
        key = self._key()
        record = self._ex.verification_record
        if record:
            self.ladder = Ladder.from_record(record, self._params)
            if self.ladder is not None and record.get("rung") == RUNG_WINDOW and record.get("state") == RUNNING:
                # Okno przerwane restartem (drabina wraca przed zapis kontrolny): powrót do trybu bazowego
                # w obu trybach — wymuszone ładowanie z okna nie może zostać na falowniku.
                await self._ex.async_verification_restore(force=True)
        if self.ladder is None and key is not None:
            self.ladder = Ladder(self._start, self._params, device_key=key)
            if not record and self._start == RUNG_CONTROL_WRITE and self._ex.verification_migration_ok():
                # Pierwszy start po aktualizacji przy trwającym sterowaniu tym urządzeniem (profil i wpis
                # zweryfikowane): bez odcinania sterowania na czas drabiny.
                self.ladder.mark_migrated(self._utcnow())
                _LOGGER.info("Volcast verification: device already controlled before the update — kept verified")
                await self._ex.async_save_verification(self.ladder.to_record())
        unsub = self._ex.io.subscribe_foreign(self.async_on_state_event)
        if unsub is not None:
            self._unsubs.append(unsub)
        self._unsubs.append(async_dispatcher_connect(
            self._hass, SIGNAL_CONTROL_UPDATED.format(entry_id=self._entry.entry_id), self._on_cycle))
        await self.async_step()

    async def async_stop(self) -> None:
        self._stopped = True
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self._cancel_timer()
        # Bramka planu zostaje przy wykonawcy (zatrzymywanym zaraz po runnerze) — bez okna i bez kroków.

    @callback
    def _on_cycle(self) -> None:
        if not self._stopped:
            self._hass.async_create_task(self.async_step())

    async def _async_timer(self, _now=None) -> None:
        self._timer = self._timer_at = None
        await self.async_step()

    # ── tryb „tylko plan” ──

    def _parked(self) -> bool:
        return getattr(self._ex, "plan_only", False) is True

    async def async_park(self) -> None:
        """Wybór własnego sterownika (wołający najpierw włącza `plan_only`): drabina w `idle` po cichu."""
        self._cancel_timer()
        lad = self.ladder
        if lad is None or lad.verified:
            return
        before = self._mark()
        window = lad.window_running
        lad.park(self._utcnow(), self._start)
        self._last_would = self._seen = None
        if window:
            await self._ex.async_verification_restore()
        await self._after(before)

    async def async_restart(self) -> None:
        """Wybór Volcast po trybie „tylko plan”: drabina od szczebla startowego (nie od odstawionego)."""
        lad = self.ladder
        if lad is not None and not lad.verified and not self._parked():
            before = self._mark()
            lad.park(self._utcnow(), self._start)
            self._last_would = self._seen = None
            await self._after(before)
        await self.async_step()

    # ── zdarzenia z zewnątrz (aplikacja, konflikty) ──

    async def async_abort(self) -> None:
        await self._event(lambda lad, now: lad.abort(now))

    async def async_retry(self) -> None:
        await self._event(lambda lad, now: lad.retry(now))

    async def async_conflict(self) -> None:
        await self._event(lambda lad, now: lad.conflict(now))

    async def async_on_state_event(self, event) -> None:
        """Zmiana stanu encji klucza zapisu (tryb encji): obca = z aktorem, nie nasza, inna wartość."""
        try:
            data = getattr(event, "data", None) or {}
            new, old = data.get("new_state"), data.get("old_state")
            if new is None or old is None or new.state == old.state or new.state in _NO_STATE:
                return
            ctx = getattr(new, "context", None) or getattr(event, "context", None)
            if self._ex.io.writer.is_ours(getattr(ctx, "id", None)):
                return
            if not (getattr(ctx, "user_id", None) or getattr(ctx, "parent_id", None)):
                return                               # odświeżenie integracji, nie czyjś zapis
        except Exception as err:  # noqa: BLE001 — obserwator nie może wywrócić pętli zdarzeń HA
            _LOGGER.warning("Volcast verification: settings change check failed (%s)", type(err).__name__)
            return
        await self._event(lambda lad, now: lad.foreign_write(now))

    async def _event(self, apply: Callable[[Ladder, Any], None]) -> None:
        if self.ladder is None or self._stopped or self._parked():
            return
        before = self._mark()
        apply(self.ladder, self._utcnow())
        await self._after(before)

    # ── krok drabiny ──

    async def async_step(self) -> None:
        if self._stopped or self._parked():
            return
        if self._busy:
            self._again = True                       # krok w toku (np. powrót wywołał cykl) — powtórzy się
            return
        self._busy = True
        try:
            for _ in range(3):
                self._again = False
                try:
                    await self._step()
                except Exception as err:  # noqa: BLE001 — drabina nie psuje sterowania
                    _LOGGER.warning("Volcast verification step failed (%s)", type(err).__name__)
                if not self._again:
                    break
        finally:
            self._busy = False

    async def _step(self) -> None:
        key = self._key()
        if key is None:
            return                                   # bez identyfikacji urządzenia drabina stoi
        now = self._utcnow()
        if self.ladder is None:
            self.ladder = Ladder(self._start, self._params, device_key=key)
        lad = self.ladder
        before = self._mark()
        if lad.device_key != key:
            lad.device_changed(key, now)
            self._last_would = self._seen = None
        lad.consent(self._can_write(), now)
        lad.tick(now)
        rd = self._ex.io.read(now)
        if lad.window_running:
            lad.window_sample(self._charge_w(rd), _number(rd.readings.get("soc")), now)
        if lad.state.state == RUNNING and lad.state.rung == RUNG_IDENTIFY and self._identified(rd):
            lad.identify_ok(now)
        if lad.state.state == RUNNING and lad.state.rung == RUNG_READ and self._readable(rd):
            lad.read_ok(now)
        if lad.state.rung == RUNG_TRIAL and lad.state.state == RUNNING:
            self._observe_trial(lad, rd, now)
        if lad.state.state == RUNNING and lad.state.rung == RUNG_CONTROL_WRITE:
            result = await self._ex.async_control_write()
            if self._parked():
                return                               # wybór własnego sterownika w trakcie zapisu — bez stopu
            lad.write_result(result, now)
        if lad.state.rung == RUNG_WINDOW and lad.state.state == WAITING and self._window_ok(rd):
            lad.window_open(now)
            if lad.window_running:
                self._ex.start_verification_window(window_schedule(
                    now, minutes=self._params.window_minutes, power_w=self._params.window_power_w,
                    base=self._ex.schedule))
        await self._after(before)

    def _can_write(self) -> bool:
        return bool(self._ex.verification_can_write()) and writing_supported(self._ex.profile, self._ex.io.kind)

    def _identified(self, rd) -> bool:
        if self._ex.io.kind == "direct":
            return bool(self._ex.io.identity_confirmed())
        return rd.raw_mode is not None and rd.raw_mode not in _NO_STATE

    @staticmethod
    def _readable(rd) -> bool:
        return isinstance(rd.readings.get("mode"), str) and _number(rd.readings.get("soc")) is not None

    def _write_keys(self) -> set[str]:
        profile = self._ex.profile
        return set((profile.raw.get("write") or {}) if profile is not None else ())

    def _observe_trial(self, lad: Ladder, rd, now) -> None:
        flat = would_write_flat(self._ex.last_decision)
        if flat is not None and flat != self._last_would:
            lad.would_write()
        if flat is not None:
            self._last_would = flat
        if self._ex.io.kind != "direct":
            return                                   # encje: obcy zapis przychodzi zdarzeniem
        if getattr(getattr(self._ex.io, "conn", None), "conflict", False):
            lad.foreign_write(now)
            return
        seen = {k: v for k, v in rd.readings.items() if k in self._write_keys()}
        write_end = self._ex.last_write_end
        if self._seen is not None and write_end == self._seen_write_end \
                and any(k in seen and seen[k] != v for k, v in self._seen.items()):
            lad.foreign_write(now)
            return
        self._seen, self._seen_write_end = seen, write_end

    def _window_ok(self, rd) -> bool:
        """Dobre okno: zgoda, SoC ≤ sufitu i świeży odczyt (młodszy niż dwa interwały odpytywania).
        Okno zastępuje plan chmury na swój czas zaraz po zgodzie — świadomie (weryfikacja przed sterowaniem)."""
        soc = _number(rd.readings.get("soc"))
        age = _number(getattr(rd, "soc_age_s", None))
        return (self._can_write() and self._params.window and soc is not None and soc <= VERIFY_WINDOW_MAX_SOC
                and age is not None and age <= self._fresh_limit_s())

    def _fresh_limit_s(self) -> float:
        if self._ex.io.kind == "direct":
            poll = _number(getattr(getattr(self._ex.io, "conn", None), "poll_s", None))
            return 2.0 * (poll if poll is not None else DIRECT_POLL_S)
        return 2.0 * EXECUTOR_INTERVAL_S

    def _charge_w(self, rd) -> float | None:
        """Moc ładowania baterii (dodatnia = ładuje): odwrócony znak odczytu rdzenia (rozładowanie dodatnie)."""
        power = _number(rd.readings.get("battery_power_w"))
        source = getattr(rd, "source", None)
        if power is None and source is not None:
            power = _number(getattr(source, "values", {}).get("battery_power_w"))
        return -power if power is not None else None

    # ── po zmianie ──

    def _mark(self) -> tuple | None:
        lad = self.ladder
        if lad is None:
            return None
        s = lad.state
        return (lad.device_key, s.rung, s.state, s.since, s.next_at, s.stop_reason, s.would_write,
                s.foreign_writes, s.hours_done, s.measured_w)

    async def _after(self, before: tuple | None) -> None:
        lad = self.ladder
        if lad is None:
            return
        self._schedule_timer(lad.state.next_at)
        after = self._mark()
        if after == before:
            return
        prev_state = before[2] if before else None
        prev_rung = before[1] if before else None
        state = lad.state.state
        if state == STOPPED and prev_state != STOPPED:
            _LOGGER.warning("Volcast verification stopped at step %s (%s)", lad.state.rung, lad.state.stop_reason)
            await self._ex.async_verification_restore()
            ir.async_create_issue(self._hass, DOMAIN, self._issue_id, is_fixable=False, severity=_WARNING,
                                  translation_key=ISSUE_VERIFICATION_STOPPED,
                                  translation_placeholders={"reason": lad.state.stop_reason or ""})
        elif state == VERIFIED and prev_state != VERIFIED and prev_rung == RUNG_WINDOW:
            _LOGGER.info("Volcast verification passed — returning the inverter to its baseline after the test")
            await self._ex.async_verification_restore()
        if prev_state == STOPPED and state != STOPPED:
            ir.async_delete_issue(self._hass, DOMAIN, self._issue_id)
        await self._ex.async_save_verification(lad.to_record())
        # Sygnał (i telemetria) przy zmianie szczebla, stanu, stopu albo urządzenia; sam postęp liczników
        # (np. godziny próby) — najwyżej raz na `SIGNAL_EVERY`.
        now = self._utcnow()
        if before is None or (after[0], after[1], after[2], after[5]) != (before[0], before[1], before[2], before[5]) \
                or self._signalled_at is None or now - self._signalled_at >= SIGNAL_EVERY:
            self._signalled_at = now
            async_dispatcher_send(self._hass, SIGNAL_CONTROL_STATE_UPDATED.format(entry_id=self._entry.entry_id))
        if state == STOPPED and prev_state != STOPPED and self._on_urgent is not None:
            try:
                await self._on_urgent()
            except Exception as err:  # noqa: BLE001 — telemetria nie blokuje drabiny
                _LOGGER.warning("Volcast verification: immediate report failed (%s)", type(err).__name__)

    # ── zegar ──

    def _schedule_timer(self, when) -> None:
        if when == self._timer_at:
            return
        self._cancel_timer()
        if when is None or self._stopped:
            return
        track = self._track_point
        if track is None:
            from homeassistant.helpers.event import async_track_point_in_utc_time as track
        self._timer = track(self._hass, self._async_timer, when)
        self._timer_at = when

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer()
        self._timer = self._timer_at = None

