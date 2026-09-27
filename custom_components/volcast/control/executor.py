"""Wykonawca planu — jedyny pisarz do falownika w tej integracji.

Odczyt, cel zapisu, pisarz, własność i obserwacja obcych zmian pochodzą z obiektu
wejścia/wyjścia urządzenia (`device_io.DeviceIO`); domyślnie tryb encji (`EntityIO`).

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
  prosi o powtórkę zaraz po bieżącym. Blokada bywa wspólna dla kolejnych wykonawców tego
  samego wpisu (`lock`): po przeładowaniu nowy wykonawca czeka, aż stary skończy zapis
  w toku — nawet gdy `async_stop` starego już się poddał;
* przed pierwszym zapisem migawka nastaw do trybu bazowego musi być pełna i zapisana —
  inaczej żadnego zapisu (klucza bez migawki nigdy byśmy nie przywrócili);
* powrót do trybu bazowego: najpierw sam tryb bazowy (neutralny, nie potrzebuje
  warunków — hamulec właściciela nie może zależeć od innej encji), potem każda
  pozostała nastawa z migawki niezależnie; własność zostaje, dopóki wszystko nie dojdzie,
  a każdy tik ponawia brakujące. Wraca klucz, którego OSTATNI zapis był nasz
  (`restore_keys`); klucz zmieniony potem przez właściciela zostaje jego (`taken_over`),
  dopóki sami go znowu nie zapiszemy. Tryb i moc to jedna grupa: nasz zapis mocy albo
  trybu = tryb wraca do bazowego, chyba że właściciel przejął tryb już PO naszym zapisie
  albo falownik pokazuje opcję spoza profilu. Powrót rusza od razu po cofnięciu zgody
  albo wyłączeniu przełącznika, także w pauzie;
* własność i migawka są związane z profilem i encją trybu (`owner`) — po identyfikatorze
  rejestru encji (`mode_uid`, przeżywa zmianę entity_id), a przy rekordzie bez niego po
  entity_id; migawki innego falownika albo mapowania nie wpisujemy w nowe encje;
* zatrzymany wykonawca nie zaczyna zapisów i nie nadpisuje magazynu (poza powrotem do
  trybu bazowego w toku); nieczytelny magazyn wyłącza wykonawcę — start ze stanem
  domyślnym zgubiłby własność i nigdy nie przywrócił trybu bazowego;
* w logach tylko klucze parametrów i nazwy klas wyjątków — nigdy `entity_id` ani treść
  wyjątku (bywa w nich numer seryjny albo adres hosta);
* wejścia (plan, zgoda, przełącznik) nie rzucają: błąd magazynu zostawia stan w pamięci
  i trafia do logu.

Obca zmiana nastaw (przejęcie): zdarzenie zmiany stanu encji klucza zapisu z aktorem
(użytkownik, automatyzacja), nie z naszym kontekstem, z wartością inną niż nasz ostatni
zapis — albo tryb falownika ustawiony na czytelną opcję spoza profilu (sygnał poziomu,
także bez aktora i także gdy cykl zatrzymał się wcześniej na innej blokadzie). Skutek:
pauza 30 min (bez zapisów planu), klucz wypada z `restore_keys`, wpis w `foreign_changes`
(lokalnie, z `entity_id`), zgłoszenie w Naprawach (z `entity_id` jako parametrem tekstu),
w logu sam klucz. Sygnał poziomu działa raz na epizod i tylko przy otwartym sterowaniu
(nie w próbie na sucho); epizod kończy odczyt trybu z profilu. Zgłoszenie znika po
pauzie, gdy epizod się skończył. Pauza nie przeżywa restartu (zegar monotoniczny).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from datetime import timedelta
from typing import Callable, Mapping
from zoneinfo import ZoneInfo

import homeassistant.util.dt as dt_util
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from ..const import (DOMAIN, ERROR_ISSUE_AFTER, EXECUTOR_INTERVAL_S, OPT_CONTROL_MODE, SIGNAL_CONTROL_UPDATED,
                     STOP_WRITE_TIMEOUT_S)
from ..core.control.baseline import baseline_params, needs_restore, snapshot_missing, take_snapshot
from ..core.control.cycle import (BLOCKED, ERROR, WRITE, ControlMemory, CycleDecision, Gates, Limits, Telemetry,
                                  commit, decide_cycle, same_value)
from ..core.control.group_writes import GROUP_KEYS, GroupReport, async_run_group_writes, order_group
from ..core.control.readings import RawState, normalize_readings
from ..core.control.select import ProfileChoice, control_verified
from ..core.control.takeover import FOREIGN_PAUSE_S, is_foreign_change
from ..core.engines.time_window import compress
from ..core.slot import InvalidSchedule, Schedule, parse_schedule
from .device_io import NO_READING, DeviceIO, EntityIO, Reading
from .store import ControlState, ControlStore

_LOGGER = logging.getLogger(__name__)

RESTORE = "restore"
_NO_READING = NO_READING
# Ile razy z rzędu cykl powtarza się po tikach zgłoszonych w jego trakcie.
_MAX_RERUNS = 2
# Ile ostatnich obcych zmian trzymamy w atrybutach (lokalnie).
_FOREIGN_KEEP = 20
# W prawdziwym HA stała z rejestru zgłoszeń; atrapa testowa jej nie ma.
_WARNING = getattr(getattr(ir, "IssueSeverity", None), "WARNING", "warning")


_Reading = Reading                 # dawna nazwa (odczyt jednego cyklu)


class VolcastExecutor:
    def __init__(self, hass, entry, *, choice: ProfileChoice | None, mapped: Mapping[str, str],
                 rated_power_w: float | None, store: ControlStore, writer=None,
                 clock: Callable[[], float] = time.monotonic, utcnow=dt_util.utcnow,
                 stop_timeout_s: float = STOP_WRITE_TIMEOUT_S, lock: asyncio.Lock | None = None,
                 mode_unique_id: str | None = None, io: DeviceIO | None = None) -> None:
        self._hass = hass
        self._entry = entry
        self._choice = choice
        self._profile = choice.profile if choice else None
        self._domain = choice.integration_domain if choice else None
        self._mapped = dict(mapped) if self._domain else {}
        self._rated = rated_power_w
        self._store = store
        # io=None → tryb encji z (choice, mapped, writer) — dotychczasowe wywołania bez zmian
        self.io: DeviceIO = io if io is not None else EntityIO(
            hass, self._profile, self._domain, self._mapped, writer, mode_unique_id=mode_unique_id)
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
        self._lock = lock if lock is not None else asyncio.Lock()
        self._running = False
        self._rerun = False
        self._stopped = False
        self._frozen = False                          # przed przeładowaniem: bez cykli, powrót dozwolony
        self._started = False
        self._foreign_issue_open = False
        self._foreign_episode = False                 # tryb spoza profilu na falowniku
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
    def owned(self) -> bool:
        """Czy to my zmienialiśmy nastawy falownika (jest co przywracać)."""
        return self._state.owned

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

    def _paused_for_s(self) -> int:
        """Ile sekund pauzy zostało (częste zmiany właściciela ją przedłużają)."""
        if not self.paused:
            return 0
        return max(0, round(self._memory.paused_until - self._clock()))

    def exec_summary(self) -> dict:
        d = self.last_decision
        out = {"decision": d.summary() if d else None, "consent": self._state.consent,
               "local_switch": self._state.local_switch, "paused": self.paused,
               "paused_for_s": self._paused_for_s(),
               "last_foreign_key": self.foreign_changes[-1]["key"] if self.foreign_changes else None,
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
            # Falownik może zostać przy naszej ostatniej komendzie — właściciel musi to wiedzieć.
            self._create_issue(f"control_error_{self._entry.entry_id}", "control_error")
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
        # Pauza nie przeżywa restartu — zgłoszenie z poprzedniego przebiegu jest nieaktualne.
        ir.async_delete_issue(self._hass, DOMAIN, self._foreign_issue_id)
        unsub = self.io.subscribe_foreign(self.async_on_state_event)
        if unsub is not None:
            self._unsub.append(unsub)

    async def async_stop(self) -> None:
        """Bez przywracania; czeka na zapis w toku najwyżej `stop_timeout_s`."""
        self._stopped = True
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()
        # Wpis rozładowany albo wyłączony: zgłoszenia tego przebiegu nie mają już właściciela.
        self._foreign_issue_open = False
        ir.async_delete_issue(self._hass, DOMAIN, self._foreign_issue_id)
        ir.async_delete_issue(self._hass, DOMAIN, f"control_error_{self._entry.entry_id}")
        if not self._lock.locked():
            return
        try:
            await asyncio.wait_for(self._lock.acquire(), self._stop_timeout_s)
        except asyncio.TimeoutError:
            _LOGGER.error("Volcast control: write still in progress at stop — giving up waiting")
            return
        self._lock.release()

    def freeze(self) -> None:
        """Koniec cykli przed przeładowaniem po zmianie mapowania; `async_restore_now` działa dalej.

        Zdejmuje licznik i obserwatora; cykl już czekający na blokadę też nic nie zapisze.
        """
        self._frozen = True
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()

    @property
    def _foreign_issue_id(self) -> str:
        return f"foreign_control_{self._entry.entry_id}"

    def _write_keys(self) -> tuple[str, ...]:
        if self._profile is None:
            return ()
        return tuple((self._profile.raw.get("write_policy") or {}).get("order") or ())

    @property
    def _writer(self):
        return self.io.writer

    def _owner(self) -> dict:
        return self.io.owner()

    def _owner_matches(self, record: Mapping[str, str]) -> bool:
        return self.io.owner_matches(record)

    def _drop_foreign_owner(self) -> bool:
        """Migawka z innego profilu albo innej encji trybu nie trafia w nowe encje.

        Własność bez powiązania (zapisana przed jego wprowadzeniem) uznajemy za własną.
        Pasujący rekord w starszym kształcie (bez `mode_uid`) albo ze starym entity_id jest
        uaktualniany. Zwraca True, gdy stan się zmienił i trzeba go zapisać.
        """
        if not self._state.owned or not self._state.owner:
            return False
        record = self._state.owner
        if self._owner_matches(record):
            fresh = self._owner()
            if "mode_uid" not in fresh and record.get("mode_uid"):
                fresh["mode_uid"] = record["mode_uid"]
            if fresh == record:
                return False
            self._state.owner = fresh
            return True
        _LOGGER.warning("Volcast control: saved baseline settings belong to a different inverter "
                        "profile or mode entity — not reusing them; check the inverter settings")
        self._state.owned = False
        self._state.snapshot = {}
        self._state.owner = {}
        self._state.restore_keys = None
        self._state.taken_over = []
        return True

    async def _async_timer(self, _now=None) -> None:
        await self.async_tick()

    # ── obca zmiana nastaw ────────────────────────────────────────────────
    async def async_on_state_event(self, event) -> None:
        """Zmiana stanu encji klucza zapisu — przejęcie, jeśli zmienił ją ktoś inny."""
        try:
            if self._on_state_event(event):
                await self._async_save("control state")
        except Exception as err:  # noqa: BLE001 — obserwator nie może wywrócić pętli zdarzeń HA
            _LOGGER.warning("Volcast control: settings change check failed (%s)", type(err).__name__)

    def _on_state_event(self, event) -> bool:
        """True, gdy zmiana była obca (stan do zapisania)."""
        if self._memory is None or not self._domain or self._stopped or self._disabled:
            return False
        data = event.data or {}
        eid = data.get("entity_id")
        new = data.get("new_state")
        key = next((k for k in self._write_keys() if self._mapped.get(k) == eid), None)
        if key is None or new is None:
            return False
        ctx = getattr(new, "context", None) or getattr(event, "context", None)
        raw_state = new.state
        value = normalize_readings(
            {key: RawState(raw_state, new.attributes.get("unit_of_measurement"))},
            self._profile, self._domain).get(key)
        foreign_option = (key == "mode" and value is None and isinstance(raw_state, str)
                          and raw_state not in _NO_READING)
        if foreign_option:
            value = "?" + raw_state          # czytelna opcja spoza profilu ≠ brak odczytu
        if not is_foreign_change(ours=self._writer.is_ours(getattr(ctx, "id", None)),
                                 has_actor=bool(getattr(ctx, "user_id", None)
                                                or getattr(ctx, "parent_id", None)),
                                 new_value=value, last_written=self._memory.last_written.get(key)):
            return False
        if foreign_option:
            self._foreign_episode = True     # sygnał poziomu nie powtórzy tego epizodu
        _LOGGER.warning("Volcast control paused for 30 min: %s changed outside Volcast", key)
        self._pause_for_foreign(key, eid)
        return True

    def _pause_for_foreign(self, key: str, eid: str | None) -> None:
        self._memory.paused_until = self._clock() + FOREIGN_PAUSE_S
        self._take_over_key(key)
        self.foreign_changes = (self.foreign_changes + [
            {"key": key, "entity_id": eid, "at": self._utcnow().isoformat()}])[-_FOREIGN_KEEP:]
        if not self._foreign_issue_open:
            self._foreign_issue_open = True
            # Parametr tekstu Napraw zostaje lokalnie w UI — to nie jest log.
            self._create_issue(self._foreign_issue_id, "foreign_control", {"entity_id": eid or ""})
        self._notify()

    def _take_over_key(self, key: str) -> None:
        """Klucz zmieniony przez właściciela: nie wraca do migawki, dopóki go znowu nie zapiszemy."""
        if not self._state.owned:
            return
        if key not in self._state.taken_over:
            self._state.taken_over.append(key)
        if self._state.restore_keys is not None and key in self._state.restore_keys:
            self._state.restore_keys.remove(key)

    def _note_written(self, keys) -> bool:
        """Klucze zapisane (albo może zapisane) przez nas; True, gdy stan się zmienił.

        Ostatni zapis klucza jest nasz, więc wraca on do powrotu — także gdy właściciel
        przejął go wcześniej. Zapis członka grupy tryb+moc kończy przejęcie trybu sprzed
        tego zapisu: nasza nastawa zmieniła znaczenie trybu, który zostawił właściciel.
        """
        keys = list(dict.fromkeys(keys))
        if not keys:
            return False
        freed = set(keys)
        if freed & set(GROUP_KEYS):
            freed |= set(GROUP_KEYS)
        before = (list(self._state.taken_over), None if self._state.restore_keys is None
                  else list(self._state.restore_keys))
        self._state.taken_over = [k for k in self._state.taken_over if k not in freed]
        if self._state.restore_keys is not None:
            # (stan sprzed pola: None = powrót obejmuje całą migawkę, nic do dopisania)
            self._state.restore_keys.extend(k for k in keys if k not in self._state.restore_keys)
        return before != (self._state.taken_over, self._state.restore_keys)

    def _update_foreign_episode(self, rd: Reading) -> None:
        """Epizod trybu spoza profilu kończy dopiero odczyt trybu z profilu (brak odczytu — nic)."""
        if "mode" in rd.readings:
            self._foreign_episode = False

    def _signal_foreign_mode(self, rd: Reading, live: bool, takeover: bool) -> bool:
        """Sygnał poziomu: raz na epizod, tylko przy otwartym sterowaniu; True, gdy wystąpił."""
        if not (rd.foreign_mode or takeover) or not live or self._foreign_episode:
            return False
        self._foreign_episode = True
        _LOGGER.warning("Volcast control paused for 30 min: the inverter mode was set to an option "
                        "outside the profile")
        self._pause_for_foreign("mode", self.io.entity_of("mode"))
        return True

    def _close_foreign_issue(self) -> None:
        if self._foreign_issue_open and not self.paused and not self._foreign_episode:
            self._foreign_issue_open = False
            ir.async_delete_issue(self._hass, DOMAIN, self._foreign_issue_id)

    # ── wejścia ───────────────────────────────────────────────────────────
    async def async_on_plan(self, raw: dict, schedule: Schedule) -> None:
        self._state.plan_raw = raw
        self.schedule = schedule
        await self._async_save("plan")
        self._notify()

    async def async_set_consent(self, value: bool) -> None:
        if not isinstance(value, bool) or value == self._state.consent:
            return       # wartość innego typu nie zmienia stanu
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
                    await self._restore(self.io.read(self._utcnow()))
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
        if self._stopped or self._disabled or self._frozen:
            return
        if self._running:
            # Cykl tego wykonawcy w toku: drugi nie startuje równolegle — bieżący
            # powtórzy się zaraz po sobie.
            self._rerun = True
            return
        self._running = True
        try:
            await self._async_tick_locked()
        finally:
            self._running = False
        self._notify()

    async def _async_tick_locked(self) -> None:
        # Blokadę może trzymać przywracanie przy usuwaniu albo poprzedni wykonawca
        # tego wpisu (zapis w toku po przeładowaniu) — czekamy, nie piszemy równolegle.
        async with self._lock:
            if self._stopped or self._frozen:
                return
            for _ in range(1 + _MAX_RERUNS):
                self._rerun = False
                try:
                    await self._tick_locked()
                except Exception as err:  # noqa: BLE001 — pętla nie może umrzeć
                    _LOGGER.error("Volcast control cycle failed (%s)", type(err).__name__)
                    self.last_decision = CycleDecision(ERROR, "exception:tick")
                    self._count(self.last_decision)
                self._close_foreign_issue()
                if not self._rerun or self._stopped or self._frozen:
                    break

    def _read(self, now_utc) -> Reading:
        return self.io.read(now_utc)

    def _age(self, st, now_utc) -> float:
        return EntityIO.age(st, now_utc)

    async def _tick_locked(self) -> None:
        now_mono, now_utc = self._clock(), self._utcnow()
        self._update_tou_preview(now_utc)
        if self._profile is None or self._memory is None:
            self.last_decision = CycleDecision("idle", "no_profile")
            return
        rd = self.io.read(now_utc)
        self._update_foreign_episode(rd)
        gates = Gates(consent=self._state.consent, local_switch=self._state.local_switch,
                      control_mode=self._entry.options.get(OPT_CONTROL_MODE),
                      verified=control_verified(self._profile, self._domain))
        if needs_restore(owned=self._state.owned, consent=gates.consent,
                         local_switch=gates.local_switch, control_mode=gates.control_mode) \
                and self._domain:
            # Także w pauzie: powrót nie rusza kluczy, które zmienił właściciel.
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
            gates=gates, memory=self._memory, **self.io.cycle_input(rd))
        if soc is not None:
            self._prev_soc = (soc, now_mono)
        # Z własnego odczytu, nie tylko z decyzji — wcześniejsza blokada cyklu go nie zasłoni.
        if self._signal_foreign_mode(rd, self._gates_open() and gates.verified, decision.takeover):
            await self._async_save("control state")
        if decision.status == WRITE and self.paused:
            decision = replace(decision, status=BLOCKED, reason="paused")
        if decision.status == WRITE and not self._state.owned:
            decision = await self._async_take_ownership(decision, readings)
            if decision.status == WRITE and not self._gates_open():
                # Zgoda, przełącznik, pauza albo zatrzymanie zmieniły się w trakcie zapisu migawki.
                decision = replace(decision, status=BLOCKED, reason="gates_changed")
        if decision.status == WRITE:
            report = await async_run_group_writes(
                decision.writes, self._writer.async_write, restore=decision.restore,
                ambiguous_safe=decision.restore_ambiguous_safe, on_exception=self._log_write_exception)
            commit(decision, report, self._memory, now_mono)
            self._log_report(decision, report)
            if self._note_written([*report.written, *report.ambiguous, *report.restored]):
                await self._async_save("control state")
        self.last_decision = decision
        self._count(decision)

    async def _async_take_ownership(self, decision: CycleDecision,
                                    readings: Mapping[str, float | str]) -> CycleDecision:
        """Migawka nastaw PRZED pierwszym zapisem; niepełna albo niezapisana = żadnego zapisu."""
        snapshot = take_snapshot(readings)
        missing = snapshot_missing(snapshot, self.io.snapshot_keys())
        if missing:
            return replace(decision, status=BLOCKED, reason="baseline_unknown", unmapped=missing)
        self._state.snapshot = snapshot
        self._state.owned = True
        self._state.owner = self._owner()
        self._state.restore_keys = []
        self._state.taken_over = []
        if not await self._async_save("baseline snapshot"):
            # Bez trwałej migawki restart nie wiedziałby, co przywrócić — nie piszemy.
            self._state.owned = False
            self._state.snapshot = {}
            self._state.owner = {}
            self._state.restore_keys = None
            self._state.taken_over = []
            return replace(decision, status=ERROR, reason="store_failed")
        return decision

    def _gates_open(self) -> bool:
        return (self._state.consent is True and self._state.local_switch and not self._stopped
                and not self.paused
                and self._entry.options.get(OPT_CONTROL_MODE) == self.io.kind)

    async def _restore(self, rd: Reading) -> None:
        """Powrót do trybu bazowego: najpierw sam tryb, potem każda pozostała nastawa.

        Tryb bazowy jest neutralny i nie potrzebuje warunków, więc nie czeka na żadną
        inną encję. Obie części idą przez wykonawcę grupowego; własność zostaje, dopóki
        wszystko nie dojdzie — każdy tik ponawia tylko to, czego falownik jeszcze nie ma.
        """
        now_mono = self._clock()
        params = baseline_params(self._profile, self._state.snapshot)
        fitted, unfit = self.io.restore_fit(rd, params)
        target = fitted.flatten()
        readings = rd.readings
        # Tylko to, co sami zapisaliśmy i czego właściciel potem nie zmienił (None = cała migawka).
        allowed = None if self._state.restore_keys is None else set(self._state.restore_keys)
        if allowed is not None and allowed & set(GROUP_KEYS) and "mode" not in self._state.taken_over:
            allowed.add("mode")          # tryb i moc to jedna grupa — nasza moc = nasz tryb
        owner_kept = [k for k in target if k in self._state.taken_over]
        # To, co falownik już ma, nie jedzie (NVM) — ta sama zasada co w cyklu.
        keys = [k for k, v in target.items()
                if k not in unfit and (allowed is None or k in allowed) and k not in owner_kept
                and not (k in readings and same_value(readings[k], v))]
        mode_kept = "mode" in owner_kept or (rd.foreign_mode and "mode" in keys)
        if "mode" in keys and rd.foreign_mode:
            keys.remove("mode")          # ktoś wybrał tryb spoza profilu — zostaje jego
        group_writes = self.io.restore_writes(fitted, [k for k in keys if k in GROUP_KEYS], rd)
        rest_writes = self.io.restore_writes(fitted, [k for k in keys if k not in GROUP_KEYS], rd)
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
        lost = [k for k in (*snapshot_missing(self._state.snapshot, self.io.snapshot_keys()), *unfit)
                if (allowed is None or k in allowed) and k not in owner_kept]
        if owner_kept:
            _LOGGER.info("Volcast control: %s left as set by the owner", owner_kept)
        if lost:
            _LOGGER.warning("Volcast control: could not return %s to the value from before control "
                            "(no saved value or outside the entity range) — check them on the inverter",
                            lost)
        self._state.owned = False
        self._state.snapshot = {}
        self._state.owner = {}
        self._state.restore_keys = None
        self._state.taken_over = []
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
                self._create_issue(issue, "control_error")
        else:
            if self._errors >= ERROR_ISSUE_AFTER:
                ir.async_delete_issue(self._hass, DOMAIN, issue)
            self._errors = 0

    def _create_issue(self, issue_id: str, translation_key: str,
                      placeholders: dict[str, str] | None = None) -> None:
        kwargs = {"translation_placeholders": placeholders} if placeholders else {}
        ir.async_create_issue(self._hass, DOMAIN, issue_id, is_fixable=False, severity=_WARNING,
                              translation_key=translation_key, **kwargs)

    def _log_write_exception(self, key: str, err: BaseException) -> None:
        # Sam klucz i klasa — treść wyjątku bywa z adresem hosta albo numerem seryjnym.
        _LOGGER.warning("Volcast control: write of %s raised %s", key, type(err).__name__)

    def _notify(self) -> None:
        async_dispatcher_send(self._hass, SIGNAL_CONTROL_UPDATED.format(entry_id=self._entry.entry_id))
