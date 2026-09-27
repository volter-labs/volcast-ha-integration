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

Tryb bezpośredni (`DirectIO`, rejestry falownika):
* bramka weryfikacji = profil i jego sekcja `modbus` zweryfikowane i nie próba; próba liczy decyzję
  na sucho własną ścieżką (bramki decyzji z `control_mode` = tryb bezpośredni, zawsze bez zapisu),
  a przy własności z wcześniejszej sesji nie liczy nic (`trial_while_owned`);
* każda decyzja, zapis i powrót wymagają potwierdzonej tożsamości urządzenia pod adresem (inaczej
  `BLOCKED identity`, także bez powrotu — pisalibyśmy do cudzego falownika);
* kolizja na łączu zatrzymuje zapisy planu (`bus_conflict`), nigdy powrotu do trybu bazowego;
* rozjazd odczytu względem naszego ostatniego zapisu liczony raz na cykl sterowania, tylko z odczytu
  rozpoczętego po końcu naszego ostatniego zapisu; drugi rozjazd tego samego klucza w 30 min przy
  niezmienionej wartości planu = przejęcie (pauza jak w trybie encji); zmiana wartości planu kasuje
  historię rozjazdów klucza;
* budżet NVM liczy każdą wysłaną ramkę (także powrotu) i jest trwały w magazynie; decyzja `RESTORE`
  (wyczerpany budżet przy trybie wymuszonym) idzie przez wykonawcę grupowego tylko przy własności;
* okna czasowe: sekwencja OFF → programy → ON, migawka programów właściciela zapisana przed
  pierwszym zapisem, powrót do niej po przerwanej sekwencji i po utracie prawa.

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
from ..core.control.conflict import drifted_keys
from ..core.control.cycle import (BLOCKED, DRY_RUN, ERROR, IDLE, WRITE, ControlMemory, CycleDecision, Gates, Limits,
                                  Telemetry, commit, decide_cycle, same_value)
from ..core.control.group_writes import GROUP_KEYS, GroupReport, async_run_group_writes, order_group
from ..core.control.readings import RawState, normalize_readings
from ..core.control.select import ProfileChoice, control_verified
from ..core.control.takeover import FOREIGN_PAUSE_S, is_foreign_change
from ..core.control.tou_cycle import EN_KEY, commit_tou, decide_tou_cycle, safety_off_decision
from ..core.control.tou_writes import (ENABLE, TOU_WORD, TouReport, _snapshot_programs, async_run_tou_writes,
                                       tou_restore_writes, tou_snapshot)
from ..core.engines.time_window import baseline_programs, compress
from ..core.guard_state import WriteBudget
from ..core.params import Params
from ..core.profile import direct_verified
from ..core.slot import InvalidSchedule, Schedule, parse_schedule
from .device_io import NO_READING, DeviceIO, DirectIO, EntityIO, Reading
from .store import ControlState, ControlStore

_LOGGER = logging.getLogger(__name__)

RESTORE = "restore"
_NO_READING = NO_READING
# Ile razy z rzędu cykl powtarza się po tikach zgłoszonych w jego trakcie.
_MAX_RERUNS = 2
# Ile ostatnich obcych zmian trzymamy w atrybutach (lokalnie).
_FOREIGN_KEEP = 20
# Rezerwa SoC programów bazowych okien czasowych, gdy nie ma planu (powrót bez migawki).
_DEFAULT_RESERVE = 10.0
# Nazwy klas decyzji z blokadą budżetu NVM (zgłoszenie w Naprawach).
_BUDGET_NOTES = ("nvm_budget", "nvm_budget_restore_ineffective")
# W prawdziwym HA stała z rejestru zgłoszeń; atrapa testowa jej nie ma.
_WARNING = getattr(getattr(ir, "IssueSeverity", None), "WARNING", "warning")


_Reading = Reading                 # dawna nazwa (odczyt jednego cyklu)


class VolcastExecutor:
    def __init__(self, hass, entry, *, choice: ProfileChoice | None, mapped: Mapping[str, str],
                 rated_power_w: float | None, store: ControlStore, writer=None,
                 clock: Callable[[], float] = time.monotonic, utcnow=dt_util.utcnow,
                 stop_timeout_s: float = STOP_WRITE_TIMEOUT_S, lock: asyncio.Lock | None = None,
                 mode_unique_id: str | None = None, io: DeviceIO | None = None,
                 on_released: Callable[[], None] | None = None) -> None:
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
        # tryb bezpośredni: to samo `io`, z dostępem do połączenia, rozjazdu i budżetu
        self._direct = self.io if isinstance(self.io, DirectIO) else None
        self._planned: dict[str, float | str] = {}     # wartości planu z ostatniej decyzji (rozjazd)
        self._drift_seen: float | None = None          # odczyt, z którego rozjazd już policzono
        self._last_write_end: float | None = None
        self._tou_restore_pending = False
        self.last_tou_report: TouReport | None = None
        self._conflict_issue_open = False
        self._budget_issue_open = False
        self._snapshot_issue_open = False
        self._safety_cap_issue_open = False
        # rozjazd policzony, ale jeszcze nieusunięty: ta sama wartość na urządzeniu nie liczy się drugi raz
        self._drift_values: dict[str, float | str] = {}
        # klucze przejęte przez właściciela → nasza wartość planu z chwili przejęcia; nie piszemy ich,
        # dopóki plan nie zmieni tej wartości (reszta planu działa dalej)
        self._owner_held: dict[str, float | str | None] = {}
        # wykonawca złożony tylko do powrotu przez poprzedni sposób sterowania: po oddaniu falownika
        # składający przeładowuje wpis (nowy sposób sterowania)
        self._on_released = on_released

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
    def nvm_budget_hit(self) -> bool:
        """Czy budżet zapisów NVM zatrzymał jakikolwiek zapis (licznik dla telemetrii)."""
        budget = self._memory.budget if self._memory is not None else None
        return bool(budget is not None and budget.hit)

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
        self._load_budget()
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

    def _load_budget(self) -> None:
        """Budżet NVM z magazynu (przeżywa restart); w trybie bezpośrednim liczy go pisarz rejestrów."""
        memory = self._memory
        if memory is None:
            return
        if memory.budget is not None and self._state.nvm_log:
            b = memory.budget
            memory.budget = WriteBudget.from_list(self._state.nvm_log, b.per_key, b.total, b.window_s,
                                                  now_wall=self._now_wall())
        if self._direct is not None:
            memory.unsupported |= self._direct.unsupported_seed()
            self._direct.bind_budget(memory.budget, now_wall=self._now_wall)

    def _now_wall(self) -> float:
        return self._utcnow().timestamp()

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
        if self._direct is not None:
            self._conflict_issue_open = self._budget_issue_open = self._safety_cap_issue_open = False
            ir.async_delete_issue(self._hass, DOMAIN, self._safety_cap_issue_id)
            ir.async_delete_issue(self._hass, DOMAIN, self._conflict_issue_id)
            ir.async_delete_issue(self._hass, DOMAIN, self._budget_issue_id)
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

    @property
    def _record_dropped_issue_id(self) -> str:
        return f"control_record_dropped_{self._entry.entry_id}"

    @property
    def _conflict_issue_id(self) -> str:
        return f"direct_conflict_{self._entry.entry_id}"

    @property
    def _budget_issue_id(self) -> str:
        return f"nvm_budget_{self._entry.entry_id}"

    def _io_ready(self) -> bool:
        """Czy jest przez co pisać: tryb encji — integracja falownika; bezpośredni — połączenie."""
        return self._direct is not None or bool(self._domain)

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
        # Falownik mógł zostać przy naszej ostatniej komendzie — sam log to za mało.
        self._create_issue(self._record_dropped_issue_id, "control_record_dropped")
        self._state.owned = False
        self._state.snapshot = {}
        self._state.owner = {}
        self._state.restore_keys = None
        self._state.taken_over = []
        self._state.tou_snapshot = None
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
            if self._direct is not None:
                self._create_issue(self._foreign_issue_id, "foreign_control_direct", {"setting": key})
            else:
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
                if self._state.owned and self._profile and self._io_ready() and self._memory:
                    await self._restore(self.io.read(self._utcnow()))
                    await self._persist_budget(force=True)
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
        direct = self._direct
        rd = self.io.read(now_utc)
        self._update_foreign_episode(rd)
        real_mode = self._entry.options.get(OPT_CONTROL_MODE)
        trial = direct is not None and direct.trial
        verified = (direct_verified(self._profile) and not trial) if direct is not None \
            else control_verified(self._profile, self._domain)
        gates = Gates(consent=self._state.consent, local_switch=self._state.local_switch,
                      control_mode=real_mode, verified=verified)
        if trial and self._state.owned:
            # Próba przy własności z wcześniejszej sesji: pisarz bez zapisu nie odda falownika.
            self._finish(CycleDecision(BLOCKED, "trial_while_owned"))
            return
        if needs_restore(owned=self._state.owned, consent=gates.consent, local_switch=gates.local_switch,
                         control_mode=gates.control_mode, active_mode=self.io.kind) and self._io_ready():
            # Także w pauzie i przy kolizji: powrót nie rusza kluczy, które zmienił właściciel.
            await self._restore(rd)
            await self._persist_budget()
            return
        if direct is not None:
            self._update_conflict_issue()
            if not await direct.async_identity_ok():
                self._finish(CycleDecision(BLOCKED, "identity"))
                return
            if self._note_drift(rd, now_mono):
                await self._async_save("control state")
            if self._tou_restore_pending:
                await self._tou_restore_after_failure()
                await self._persist_budget()
                return
        # Próba: decyzja liczona tak, jakby wybrano tryb bezpośredni, ale zawsze bez zapisu.
        dgates = replace(gates, control_mode=self.io.kind, verified=False) if trial else gates
        tele = self._telemetry(rd, now_mono)
        if direct is not None and self._profile.control_model == "time_window":
            await self._tou_tick(rd, dgates, tele, now_mono, now_utc)
            return
        decision = decide_cycle(
            profile=self._profile, schedule=self.schedule, now_utc=now_utc, now_mono=now_mono, tele=tele,
            limits=Limits(rated_power_w=float(self._rated_power() or 0.0)),
            gates=dgates, memory=self._memory, **self.io.cycle_input(rd))
        if tele.soc is not None:
            self._prev_soc = (tele.soc, now_mono)
        self._forget_changed(decision.flat)
        decision = self._without_owner_held(decision)
        # Z własnego odczytu, nie tylko z decyzji — wcześniejsza blokada cyklu go nie zasłoni.
        if self._signal_foreign_mode(rd, self._gates_open() and gates.verified, decision.takeover):
            await self._async_save("control state")
        decision = self._gate_write(decision)
        if decision.status == WRITE and not self._state.owned:
            decision = await self._async_take_ownership(decision, rd.readings)
            if decision.status == WRITE and not self._gates_open():
                # Zgoda, przełącznik, pauza albo zatrzymanie zmieniły się w trakcie zapisu migawki.
                decision = replace(decision, status=BLOCKED, reason="gates_changed")
        if decision.status == WRITE:
            report = await async_run_group_writes(
                decision.writes, self._writer.async_write, restore=decision.restore,
                ambiguous_safe=decision.restore_ambiguous_safe, on_exception=self._log_write_exception)
            self._end_direct_writes()
            commit(decision, report, self._memory, now_mono)
            self._log_report(decision, report)
            if self._note_written([*report.written, *report.ambiguous, *report.restored]):
                await self._async_save("control state")
        elif decision.status == RESTORE:
            decision = await self._run_budget_restore(decision, now_mono)
        self._finish(decision)
        await self._after_direct_cycle(decision)

    def _telemetry(self, rd: Reading, now_mono: float) -> Telemetry:
        readings = rd.readings
        soc = readings.get("soc")
        soc = soc if isinstance(soc, float) else None
        temp = readings.get("battery_temp_c")
        prev_soc, gap = (self._prev_soc[0], now_mono - self._prev_soc[1]) if self._prev_soc else (None, None)
        return Telemetry(soc=soc, soc_age_s=rd.soc_age_s, battery_temp_c=temp if isinstance(temp, float) else None,
                         previous_soc=prev_soc, previous_soc_gap_s=gap)

    def _rated_power(self) -> float | None:
        if self._rated:
            return self._rated
        return self._direct.rated_power_w() if self._direct is not None else None

    def _finish(self, decision) -> None:
        self.last_decision = decision
        self._count(decision)

    def _gate_write(self, decision):
        """Ostatnie bramki przed zapisem planu: pauza, kolizja na łączu, bramki wykonawcy (tryb = io)."""
        if decision.status != WRITE:
            return decision
        if self.paused:
            return replace(decision, status=BLOCKED, reason="paused")
        if self._direct is not None and self._direct.conn.conflict:
            return replace(decision, status=BLOCKED, reason="bus_conflict")
        if not self._gates_open():
            return replace(decision, status=BLOCKED, reason="gates_closed")
        return decision

    async def _run_budget_restore(self, decision: CycleDecision, now_mono: float) -> CycleDecision:
        """`RESTORE` z cyklu (wyczerpany budżet przy trybie wymuszonym): tryb bazowy przez wykonawcę
        grupowego, tylko przy własności i otwartych bramkach; kolizja go nie blokuje. Własność zostaje."""
        if not self._state.owned:
            return replace(decision, status=BLOCKED, reason="restore_not_owned")
        if not self._gates_open():
            return replace(decision, status=BLOCKED, reason="paused" if self.paused else "gates_closed")
        report = await async_run_group_writes(decision.writes, self._writer.async_write,
                                              on_exception=self._log_write_exception)
        self._end_direct_writes()
        commit(decision, report, self._memory, now_mono)
        self._log_report(decision, report)
        if self._note_written([*report.written, *report.ambiguous]):
            await self._async_save("control state")
        _LOGGER.warning("Volcast control: write budget used up — inverter returned to its baseline mode")
        return decision

    # ── tryb bezpośredni: rozjazd, zgłoszenia, budżet ─────────────────────
    def _end_direct_writes(self) -> None:
        if self._direct is None:
            return
        end = self._clock()
        sent = self._direct.end_writes(end)
        if sent:
            self._last_write_end = end
        for key in sent:
            self._drift_values.pop(EN_KEY if key in (ENABLE, TOU_WORD) else key, None)

    def _forget_changed(self, flat: Mapping[str, float | str]) -> None:
        """Zmieniona wartość planu klucza kasuje jego historię rozjazdów (nowa wartość, nowa historia)."""
        if self._direct is None or not flat:
            return
        for key in {*flat, *self._planned}:
            old, new = self._planned.get(key), flat.get(key)
            if old is None or new is None or not same_value(old, new):
                self._direct.drift.forget(key)
        def changed(key: str) -> bool:
            old, new = self._planned.get(key), flat.get(key)
            return (old is None) != (new is None) or (old is not None and not same_value(old, new))

        group_changed = any(changed(k) for k in GROUP_KEYS)
        for key in list(self._owner_held):
            held, new = self._owner_held[key], flat.get(key)
            if held is None or new is None or not same_value(held, new) \
                    or (key in GROUP_KEYS and group_changed):
                # plan zmienił wartość — klucz znów nasz; moc znaczy coś tylko razem z trybem, więc
                # zmiana planu KTÓREGOKOLWIEK członka grupy zwalnia całą grupę
                del self._owner_held[key]
        self._planned = dict(flat)

    def _held_keys(self) -> set[str]:
        return set(self._owner_held)

    def _without_owner_held(self, decision):
        """Zapisy planu bez kluczy przejętych przez właściciela; grupa tryb+moc idzie razem albo wcale."""
        if self._direct is None or decision.status != WRITE or not self._owner_held:
            return decision
        keys = {w.key for w in decision.writes}
        drop = keys & self._held_keys()
        if not drop:
            return decision
        if drop & set(GROUP_KEYS):
            drop |= set(GROUP_KEYS)
        writes = [w for w in decision.writes if w.key not in drop]
        decision = replace(decision, writes=writes, restore={k: v for k, v in decision.restore.items()
                                                            if k not in drop},
                           notes=(*decision.notes, "owner_kept"))
        if not writes:
            decision = replace(decision, status=IDLE, reason="owner_kept")
        return decision

    def _note_drift(self, rd: Reading, now_mono: float) -> bool:
        """Raz na cykl i raz na odczyt: ZMIANA wartości na urządzeniu względem naszego ostatniego zapisu.

        Ta sama, trwająca wartość liczy się raz (do naszego następnego zapisu). Przejęcie (drugi rozjazd
        w 30 min przy niezmienionym planie) czyni wartość właściciela punktem odniesienia klucza —
        kolejne przejęcie tylko przy NOWEJ zmianie; klucz nie jest pisany, dopóki plan nie zmieni
        jego wartości. True = przejęcie.
        """
        reading = rd.source
        if reading is None or reading.at_mono == self._drift_seen:
            return False
        self._drift_seen = reading.at_mono
        drift = self._direct.drift
        if not drift.usable(reading.at_mono):
            return False
        memory = self._memory
        drifted = drifted_keys(memory.last_written, reading.device)
        for key in [k for k in self._drift_values if k not in drifted]:
            del self._drift_values[key]            # wartość wróciła do naszej — epizod skończony
        taken = False
        for key in drifted:
            if key in memory.uncertain:
                continue                    # nasz zapis o nieznanym wyniku — to nie zmiana właściciela
            value = reading.device[key]
            seen = self._drift_values.get(key)
            if seen is not None and same_value(seen, value):
                continue                    # ta sama wartość co w policzonym już rozjeździe
            self._drift_values[key] = value
            if drift.note_drift(key, now_mono):
                _LOGGER.warning("Volcast control paused for 30 min: %s changed outside Volcast", key)
                memory.last_written[key] = value          # wartość właściciela = punkt odniesienia
                self._drift_values.pop(key, None)
                self._owner_held[key] = self._planned.get(key)
                self._pause_for_foreign(key, None)
                taken = True
        return taken

    def _update_conflict_issue(self) -> None:
        conn = self._direct.conn
        refused_owned = conn.refused() is not None and self._state.owned
        if refused_owned:
            return                          # zgłoszenie odmowy przy własności prowadzi powrót (bez migotania)
        if conn.conflict and not self._conflict_issue_open:
            self._conflict_issue_open = True
            reason = conn.monitor.reason if conn.monitor.state == "conflict" else \
                (conn.static_conflicts[0] if conn.static_conflicts else "unknown")
            _LOGGER.warning("Volcast direct control: another client uses the inverter link (%s) — "
                            "plan writes stopped", reason)
            self._create_issue(self._conflict_issue_id, "direct_conflict", {"reason": str(reason or "unknown")})
        elif not conn.conflict and self._conflict_issue_open:
            self._conflict_issue_open = False
            ir.async_delete_issue(self._hass, DOMAIN, self._conflict_issue_id)

    async def _after_direct_cycle(self, decision) -> None:
        if self._direct is None:
            return
        notes = set(getattr(decision, "notes", ()) or ())
        live = self._gates_open()
        if live and notes & set(_BUDGET_NOTES) and not self._budget_issue_open:
            self._budget_issue_open = True
            _LOGGER.warning("Volcast direct control: inverter memory write budget used up — waiting")
            self._create_issue(self._budget_issue_id, "nvm_budget")
        elif self._budget_issue_open and live and not notes & set(_BUDGET_NOTES) \
                and decision.status in (WRITE, IDLE):
            self._budget_issue_open = False
            ir.async_delete_issue(self._hass, DOMAIN, self._budget_issue_id)
        capped = "tou_safety_off_cap" in notes
        if live and capped and not self._safety_cap_issue_open:
            self._safety_cap_issue_open = True
            _LOGGER.warning("Volcast direct control: the time-of-use schedule was switched off for safety too "
                            "often today — no more switch-offs until the daily count drops")
            self._create_issue(self._safety_cap_issue_id, "tou_safety_off_cap")
        elif self._safety_cap_issue_open and live and not capped:
            self._safety_cap_issue_open = False
            ir.async_delete_issue(self._hass, DOMAIN, self._safety_cap_issue_id)
        await self._persist_budget()

    @property
    def _safety_cap_issue_id(self) -> str:
        return f"tou_safety_off_cap_{self._entry.entry_id}"

    async def _persist_budget(self, *, force: bool = False) -> None:
        """Budżet NVM do magazynu, gdy przybyło ramek (także po powrocie do trybu bazowego)."""
        budget = self._memory.budget if self._memory is not None else None
        if self._direct is None or budget is None:
            return
        log = budget.to_list()
        if log != self._state.nvm_log:
            self._state.nvm_log = log
            await self._async_save("write budget", force=force)

    async def _fresh_direct_reading(self, rd: Reading) -> Reading | None:
        """Odczyt do powrotu: rozpoczęty po końcu naszego ostatniego zapisu. Czeka na odpytywanie w toku
        (ograniczony czas); bez świeżego odczytu None — powrót nie decyduje na starych danych."""
        src = rd.source
        if src is not None and (self._last_write_end is None or src.at_mono > self._last_write_end):
            return rd
        fresh = await self._direct.conn.async_read_fresh(self._last_write_end)
        return self.io.read(self._utcnow()) if fresh is not None else None

    # ── okna czasowe (tryb bezpośredni) ───────────────────────────────────
    async def _tou_tick(self, rd: Reading, gates: Gates, tele: Telemetry, now_mono: float, now_utc) -> None:
        if rd.source is None:
            self._finish(CycleDecision(BLOCKED, "no_reading"))
            return
        snap = self._state.tou_snapshot
        d = decide_tou_cycle(profile=self._profile, schedule=self.schedule, now_utc=now_utc, now_mono=now_mono,
                             tz=self._tz(), tele=tele, limits=Limits(rated_power_w=float(self._rated_power() or 0.0)),
                             reading=rd.source, gates=gates, memory=self._memory,
                             owner_word=snap["tou_word"] if snap else None)
        if tele.soc is not None:
            self._prev_soc = (tele.soc, now_mono)
        self._forget_changed(d.flat)
        d = self._tou_without_owner_held(d, rd.source, gates, now_mono)
        d = self._gate_write(d)
        if d.status == WRITE and not self._state.owned:
            d = await self._async_take_tou_ownership(d, rd)
            if d.status == WRITE and not self._gates_open():
                d = replace(d, status=BLOCKED, reason="gates_changed")
        elif d.status == WRITE and self._state.tou_snapshot is None:
            self._snapshot_lost()               # nigdy nowa migawka przy własności — powrót do programów bazowych
        if d.status == WRITE:
            report = await async_run_tou_writes(d.writes, self._writer.async_write, pre_held=d.pre_held,
                                                on_exception=self._log_write_exception)
            end = self._clock()                     # po końcu sekwencji — odczyt z jej trakcie jest nieświeży
            self._end_direct_writes()
            commit_tou(d, report, self._memory, end, now_wall=None)   # ramki liczy `on_send` pisarza
            self.last_tou_report = report
            changed = self._note_written([*report.written, *report.ambiguous])
            if report.restore_needed:
                _LOGGER.warning("Volcast control: time-of-use rewrite interrupted — restoring the owner's programs")
                self._tou_restore_pending = True
                await self._tou_restore_after_failure(record_decision=False)
            if changed:
                await self._async_save("control state")
        self._finish(d)
        await self._after_direct_cycle(d)

    def _tou_without_owner_held(self, d, reading, gates: Gates, now_mono: float):
        """Pola programów przejęte przez właściciela nie są pisane (zostaje jego wartość), reszta przepisania
        idzie. Gdy tego nie da się zrobić bezpiecznie (przejęty start programu — kolejność startów), a zmiana
        idzie w stronę bezpieczną: wyłączenie harmonogramu (poza budżetem, z limitem); nigdy żywy, nieaktualny
        program ładowania z sieci."""
        if d.status not in (WRITE, DRY_RUN) or not self._owner_held or "tou_safety_off" in d.notes:
            return d

        def key(w) -> str:
            return EN_KEY if w.key in (ENABLE, TOU_WORD) else w.key

        held = {key(w) for w in d.writes} & self._held_keys()
        if not held:
            return d
        programs = [w for w in d.writes if w.key not in (ENABLE, TOU_WORD)]
        if any(k.endswith(".start") for k in held):
            safe = safety_off_decision(d, reading, self._profile, gates, self._memory, now_mono)
            if safe is not None:
                return replace(safe, notes=(*safe.notes, "owner_kept"))
            return replace(d, status=IDLE, reason="owner_kept", writes=[], notes=(*d.notes, "owner_kept"))
        writes = [w for w in d.writes if key(w) not in held or (w.key == ENABLE and w is d.writes[0])]
        kept = [w for w in writes if w.key not in (ENABLE, TOU_WORD)]
        if programs and not kept:
            return replace(d, status=IDLE, reason="owner_kept", writes=[], notes=(*d.notes, "owner_kept"))
        return replace(d, writes=writes, notes=(*d.notes, "owner_kept"))

    def _snapshot_lost(self) -> None:
        if self._snapshot_issue_open:
            return
        self._snapshot_issue_open = True
        _LOGGER.warning("Volcast control: the saved copy of the owner's time-of-use programs is missing — "
                        "returning control will restore baseline programs with the schedule off")
        self._create_issue(f"tou_snapshot_lost_{self._entry.entry_id}", "tou_snapshot_lost")

    def _tz(self):
        name = getattr(getattr(self._hass, "config", None), "time_zone", None)
        try:
            return ZoneInfo(name or "Europe/Warsaw")
        except Exception:  # noqa: BLE001
            return ZoneInfo("Europe/Warsaw")

    async def _async_take_tou_ownership(self, d, rd: Reading):
        """Migawka programów właściciela (surowe słowa) PRZED pierwszym zapisem okien czasowych —
        tylko przy przejęciu (przy własności falownik ma już NASZE programy)."""
        snap = tou_snapshot(rd.source, self._profile)
        if snap is None or self._state.owned:
            return replace(d, status=BLOCKED, reason="baseline_unknown")
        prev = (self._state.owned, dict(self._state.snapshot), dict(self._state.owner),
                self._state.restore_keys, list(self._state.taken_over), self._state.tou_snapshot)
        snapshot = take_snapshot(rd.readings)
        if snapshot_missing(snapshot, self.io.snapshot_keys()):
            return replace(d, status=BLOCKED, reason="baseline_unknown")
        self._state.snapshot = snapshot
        self._state.owned = True
        self._state.owner = self._owner()
        self._state.restore_keys = []
        self._state.taken_over = []
        self._state.tou_snapshot = snap
        if not await self._async_save("baseline snapshot"):
            (self._state.owned, self._state.snapshot, self._state.owner, self._state.restore_keys,
             self._state.taken_over, self._state.tou_snapshot) = prev
            return replace(d, status=ERROR, reason="store_failed")
        return d

    async def _tou_restore_after_failure(self, *, record_decision: bool = True) -> None:
        """Przerwana sekwencja: od razu programy właściciela z migawki (własność zostaje)."""
        rd = await self._fresh_direct_reading(self.io.read(self._utcnow()))
        report = await self._run_tou_restore(rd) if rd is not None else None
        done = report is not None and self._tou_restore_complete(report)
        if done:
            self._tou_restore_pending = False
        if record_decision:
            self._finish(CycleDecision(RESTORE if done else ERROR,
                                       "tou_owner_programs" if done else "restore_failed"))

    def _tou_restore_complete(self, report: TouReport) -> bool:
        return not (report.failed or report.held or report.unsupported or report.ambiguous)

    async def _run_tou_restore(self, rd: Reading) -> TouReport | None:
        """Powrót do programów właściciela (albo bazowych bez migawki); klucze przejęte przez właściciela
        zostają jego. None = brak świeżego odczytu programów."""
        reading = rd.source
        if reading is None:
            return None
        reserve, rated = self._tou_restore_params()
        try:
            writes = tou_restore_writes(self._profile, self._state.tou_snapshot, reading, soc_reserve=reserve,
                                        rated_power_w=rated)
        except Exception as err:  # noqa: BLE001 — brak odczytu programów: następny cykl
            _LOGGER.debug("Volcast control: time-of-use restore not possible now (%s)", type(err).__name__)
            return None
        taken = set(self._state.taken_over)
        if EN_KEY in taken:
            taken |= {ENABLE, TOU_WORD}
        writes = [w for w in writes if w.key not in taken]
        if not writes:
            return TouReport()
        report = await async_run_tou_writes(writes, self._writer.async_write, on_exception=self._log_write_exception)
        end = self._clock()
        self._end_direct_writes()
        self._record_tou_restore(report, end)
        return report

    def _tou_restore_params(self) -> tuple[float, float]:
        reserve = self.schedule.fallback.soc_reserve if self.schedule is not None else _DEFAULT_RESERVE
        return float(reserve), float(self._rated_power() or 0.0)

    def _record_tou_restore(self, report: TouReport, end: float) -> None:
        """Zapisy powrotu są NASZE: pamięć zna wartości, więc odczyt po nich nie jest rozjazdem."""
        memory, snap = self._memory, self._state.tou_snapshot
        if snap is not None:
            programs = _snapshot_programs(self._profile, snap)
        else:
            programs = baseline_programs(self._profile, *self._tou_restore_params())
        flat = Params(tou=programs).flatten()
        ebit = 1 << self._profile.raw["write"]["tou_enable"]["enable_bit"]
        for key in report.written:
            if key in flat:
                memory.last_written[key] = flat[key]
                memory.uncertain.discard(key)
            elif key == ENABLE:
                memory.last_written[EN_KEY] = 0.0
            elif key == TOU_WORD and snap is not None:
                memory.last_written[EN_KEY] = 1.0 if int(snap["tou_word"]) & ebit else 0.0
        for key in report.ambiguous:
            k = EN_KEY if key in (ENABLE, TOU_WORD) else key
            memory.last_written.pop(k, None)
            memory.uncertain.add(k)
        if report.frames:
            memory.tou_write_end = end

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
        # Nowa migawka z bieżących nastaw — zgłoszenie o porzuconym rekordzie jest nieaktualne.
        ir.async_delete_issue(self._hass, DOMAIN, self._record_dropped_issue_id)
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

        Tryb bezpośredni: tylko przy potwierdzonej tożsamości urządzenia (kolizja na łączu powrotu
        nie blokuje), na świeżym odczycie; okna czasowe wracają do programów właściciela.
        """
        if self._direct is not None:
            conn = self._direct.conn
            if conn.client is None and conn.refused() is None:
                # Połączenie jeszcze startuje (w tle) — powrót ruszy w następnym cyklu, bez ostrzeżeń.
                self._finish(CycleDecision(BLOCKED, "connecting"))
                return
            self._update_conflict_issue()          # kolizja powrotu nie blokuje, ale właściciel ma wiedzieć
            if not await self._direct.async_identity_ok():
                refused = conn.refused()
                reason = "identity" if refused is None else "direct_refused"
                if not (self.last_decision and self.last_decision.reason == reason):
                    if refused is None:
                        _LOGGER.warning("Volcast control: return to the baseline waits — the inverter at the "
                                        "saved address is not confirmed")
                    else:
                        _LOGGER.warning("Volcast control: return to the baseline waits — the direct connection "
                                        "was refused (%s)", refused)
                if refused is not None and not self._conflict_issue_open:
                    self._conflict_issue_open = True
                    self._create_issue(self._conflict_issue_id, "direct_conflict", {"reason": refused})
                self._finish(CycleDecision(BLOCKED, reason))
                return
            fresh = await self._fresh_direct_reading(rd)
            if fresh is None:
                # Bez odczytu rozpoczętego po naszym zapisie nie wiemy, co falownik ma — własność zostaje.
                self._finish(CycleDecision(ERROR, "no_fresh_reading"))
                return
            rd = fresh
            if self._profile.control_model == "time_window":
                if self._state.tou_snapshot is None:
                    self._snapshot_lost()
                await self._restore_tou(rd)
                return
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
        # Tryb bezpośredni: o każdym kluczu rozstrzyga świeży odczyt pisarza przed zapisem (bez ramki,
        # gdy rejestr już ma wartość bazową) — nigdy odczyt z cyklu.
        direct = self._direct is not None
        keys = [k for k, v in target.items()
                if k not in unfit and (allowed is None or k in allowed) and k not in owner_kept
                and (direct or not (k in readings and same_value(readings[k], v)))]
        mode_kept = "mode" in owner_kept or (rd.foreign_mode and "mode" in keys)
        if "mode" in keys and rd.foreign_mode:
            keys.remove("mode")          # ktoś wybrał tryb spoza profilu — zostaje jego
        group_writes = self.io.restore_writes(fitted, [k for k in keys if k in GROUP_KEYS], rd)
        rest_writes = self.io.restore_writes(fitted, [k for k in keys if k not in GROUP_KEYS], rd)
        # Moc (gdyby profil ją kiedyś miał w stanie bazowym) po trybie: tryb bazowy ją ignoruje.
        reports = []
        write = getattr(self._writer, "async_write_restore", None) if direct else None
        for writes in (order_group(group_writes, power_first=False), rest_writes):
            if writes:
                reports.append(await async_run_group_writes(writes, write or self._writer.async_write,
                                                            on_exception=self._log_write_exception))
        self._end_direct_writes()
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
        await self._release_ownership()
        self.last_decision = CycleDecision(RESTORE, "baseline_mode_kept" if mode_kept else "baseline",
                                           writes=writes, flat=target, takeover=mode_kept)
        if owner_kept:
            _LOGGER.info("Volcast control: %s left as set by the owner", owner_kept)
        if lost:
            _LOGGER.warning("Volcast control: could not return %s to the value from before control "
                            "(no saved value or outside the entity range) — check them on the inverter",
                            lost)
        if mode_kept:
            _LOGGER.warning("Volcast control: settings returned to their baseline; the inverter mode "
                            "was changed outside Volcast and is left as it is")
        else:
            _LOGGER.warning("Volcast control: inverter returned to its baseline mode")
        self._count(self.last_decision)

    async def _release_ownership(self) -> None:
        self._state.owned = False
        self._state.snapshot = {}
        self._state.owner = {}
        self._state.restore_keys = None
        self._state.taken_over = []
        self._state.tou_snapshot = None
        self._memory.last_written.clear()
        await self._async_save("baseline state", force=True)
        if self._on_released is not None:
            try:
                self._on_released()
            except Exception as err:  # noqa: BLE001 — powrót już się udał
                _LOGGER.warning("Volcast control: reload after the return failed (%s)", type(err).__name__)
        conn = self._direct.conn if self._direct is not None else None
        if conn is not None and conn.allow_conflicted_restore and conn.static_conflicts:
            # Połączenie istniało tylko po to, żeby oddać falownik — inna integracja go używa.
            _LOGGER.warning("Volcast direct connection closed: the inverter is back in its own settings and "
                            "another integration uses it")
            await conn.async_stop()

    async def _restore_tou(self, rd: Reading) -> None:
        """Utrata prawa w trybie okien czasowych: programy i włącznik właściciela z migawki."""
        report = await self._run_tou_restore(rd)
        if report is None or not self._tou_restore_complete(report):
            failed = () if report is None else tuple(dict.fromkeys([*report.failed, *report.held,
                                                                     *report.unsupported]))
            self.last_decision = CycleDecision(ERROR, "restore_failed")
            if failed != self._restore_failed:
                _LOGGER.warning("Volcast control: return to the owner's time-of-use programs incomplete "
                                "(not applied: %s) — retrying every cycle", list(failed))
            self._restore_failed = failed
            self._count(self.last_decision)
            return
        self._restore_failed = None
        self._tou_restore_pending = False
        await self._release_ownership()
        self.last_decision = CycleDecision(RESTORE, "baseline")
        _LOGGER.warning("Volcast control: inverter returned to the owner's time-of-use programs")
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
            if self._direct is not None:
                # Zapisy powrotu są NASZE: odczyt po nich nie może wyglądać na zmianę właściciela.
                for key in report.written:
                    if key in target:
                        memory.last_written[key] = target[key]
                for key in report.ambiguous:
                    memory.last_written.pop(key, None)

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
