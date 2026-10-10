"""Drabina weryfikacji urządzenia — czysta maszyna stanów (bez HA, bez zegara: czas podaje wołający).

Szczeble (kontrakt sterowania, blok `driver.control.verification`):
1 identyfikacja urządzenia, 2 odczyt, 3 próba bez zapisu (`trial_hours`; licznik „co bym zapisał”,
obcy zapis = stop), 4 zapis kontrolny (ponowny zapis bieżącego trybu + odczyt zwrotny), 5 okno próbne
(wymuszone ładowanie `window_power_w` przez `window_minutes`; średnia moc odchylona ≤ 30 % i SoC rośnie
→ `verified`). Szczeble 4–5 tylko przy zgodzie (`consent(True)`); bez niej drabina czeka na 3 (`waiting`).
Po próbie przy zgodzie — od razu szczebel 4; okno (5) rusza dopiero na sygnał wołającego (`window_open`),
który zna warunki dobrego okna (SoC poniżej sufitu, świeży odczyt).

Stany: `idle` (nic się nie dzieje — nowa drabina albo nowe urządzenie), `running`, `waiting`,
`stopped` (z `stop_reason` z listy zamkniętej), `verified`. `retry` wraca na szczebel stopu
(szczebel 4–5 bez zgody → 3 `waiting`; stop identyfikacji → szczebel startowy).

Wynik należy do urządzenia: `device_key` = skrót identyfikacji z solą instalacji (nie numer seryjny).
Inny klucz → nowa drabina `idle` od szczebla startowego; zmiana w trakcie drabiny → stop
`identify_changed` (ponowienie zaczyna od szczebla startowego).

Moc w oknie to moc ŁADOWANIA baterii (dodatnia = ładuje) — znak odczytu odwraca wołający.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

RUNG_NONE, RUNG_IDENTIFY, RUNG_READ, RUNG_TRIAL, RUNG_CONTROL_WRITE, RUNG_WINDOW = 0, 1, 2, 3, 4, 5
START_RUNGS = (RUNG_IDENTIFY, RUNG_TRIAL)
IDLE, RUNNING, WAITING, STOPPED, VERIFIED = "idle", "running", "waiting", "stopped", "verified"
STATES = (IDLE, RUNNING, WAITING, STOPPED, VERIFIED)
FOREIGN_WRITE, READBACK_MISMATCH, WINDOW_DEVIATION = "foreign_write", "readback_mismatch", "window_deviation"
CONTROLLER_CONFLICT, READ_FAILED, IDENTIFY_CHANGED = "controller_conflict", "read_failed", "identify_changed"
USER_ABORT, CONSENT_REVOKED = "user_abort", "consent_revoked"
STOP_REASONS = (FOREIGN_WRITE, READBACK_MISMATCH, WINDOW_DEVIATION, CONTROLLER_CONFLICT, READ_FAILED,
                IDENTIFY_CHANGED, USER_ABORT, CONSENT_REVOKED)
# Zredagowane opisy stopu dla człowieka (EN, ≤ 120 znaków) — bez nazw encji, adresów i seriali.
STOP_DETAILS = {
    FOREIGN_WRITE: "An inverter setting was changed outside Volcast during the test.",
    READBACK_MISMATCH: "The inverter did not keep the value written in the control test.",
    WINDOW_DEVIATION: "The test charge did not reach the expected power or the battery level did not rise.",
    CONTROLLER_CONFLICT: "Another controller is managing the inverter.",
    READ_FAILED: "The inverter values could not be read back.",
    IDENTIFY_CHANGED: "A different inverter was identified during the test.",
    USER_ABORT: "The test was stopped by the user.",
    CONSENT_REVOKED: "Control permission was withdrawn during the test.",
}
MAX_DETAIL = 120
MAX_DEVIATION_PCT = 30.0
READ_TIMEOUT = timedelta(minutes=10)          # szczebel 2 bez spójnego odczytu → `read_failed`
RECORD_VERSION = 1
_KEY_LEN = 32
_HEX = frozenset("0123456789abcdef")
# Granice nadpisań z profilu (schemat profilu, blok `verification`).
OVERRIDE_RANGES = {"trial_hours": (1, 72), "window_minutes": (5, 60), "window_power_w": (100, 3000)}


@dataclass(frozen=True)
class LadderParams:
    trial_hours: int
    window_minutes: int
    window_power_w: int

    def with_overrides(self, block: Mapping[str, Any] | None) -> "LadderParams":
        """Nadpisania z opcjonalnego bloku `verification` profilu; pole złe albo spoza zakresu — pominięte."""
        changes = {}
        for key, (lo, hi) in OVERRIDE_RANGES.items():
            v = (block or {}).get(key) if isinstance(block, Mapping) else None
            if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi:
                changes[key] = v
        return replace(self, **changes)


@dataclass
class LadderState:
    rung: int
    state: str
    since: datetime | None = None
    next_at: datetime | None = None
    stop_reason: str | None = None
    stop_detail: str | None = None
    would_write: int = 0
    foreign_writes: int = 0
    hours_done: float = 0.0
    trial_started: datetime | None = None     # próba ruszyła (licznik szczebla 3 w bloku)
    target_w: int | None = None               # okno ruszyło (blok `window`)
    measured_w: int | None = None
    deviation_pct: float | None = None
    samples: list = field(default_factory=list)   # (moc ładowania W, SoC %) — tylko w pamięci


def device_key(salt: bytes, identity: str) -> str:
    """Klucz urządzenia: 32 znaki hex (małe litery, co najmniej jedna litera) ze skrótu soli i identyfikacji."""
    key = hashlib.sha256(bytes(salt) + b"|" + identity.encode("utf-8")).hexdigest()[:_KEY_LEN]
    if key.isdigit():
        key = "a" + key[1:]                       # kontrakt odrzuca klucz z samych cyfr
    return key


def valid_device_key(key: Any) -> bool:
    return (isinstance(key, str) and 16 <= len(key) <= _KEY_LEN and set(key) <= _HEX
            and not key.isdigit())


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or len(raw) > 40:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else None


class Ladder:
    def __init__(self, start_rung: int, params: LadderParams, *, device_key: str) -> None:
        if start_rung not in START_RUNGS:
            raise ValueError(f"start rung must be one of {START_RUNGS}")
        self.start_rung = start_rung
        self.params = params
        self.device_key = device_key
        self.consent_given = False
        self.state = LadderState(rung=start_rung, state=IDLE)

    # ── odczyt ──

    @property
    def verified(self) -> bool:
        return self.state.state == VERIFIED

    @property
    def window_running(self) -> bool:
        return self.state.rung == RUNG_WINDOW and self.state.state == RUNNING

    def _active(self) -> bool:
        return self.state.state in (RUNNING, WAITING)

    # ── przejścia ──

    def _enter(self, rung: int, state: str, now: datetime, *, next_at: datetime | None = None) -> None:
        s = self.state
        s.rung, s.state, s.since, s.next_at = rung, state, now, next_at
        s.stop_reason = s.stop_detail = None
        if rung == RUNG_TRIAL and state == RUNNING:
            s.trial_started, s.would_write, s.foreign_writes, s.hours_done = now, 0, 0, 0.0
            s.next_at = now + timedelta(hours=self.params.trial_hours)
        if rung == RUNG_WINDOW:
            s.samples = []
            if state == RUNNING:
                s.target_w, s.measured_w, s.deviation_pct = self.params.window_power_w, None, None
                s.next_at = now + timedelta(minutes=self.params.window_minutes)

    def _stop(self, reason: str, now: datetime) -> None:
        s = self.state
        s.state, s.since, s.next_at = STOPPED, now, None
        s.stop_reason, s.stop_detail = reason, STOP_DETAILS[reason][:MAX_DETAIL]
        s.samples = []

    def _after_trial(self, now: datetime) -> None:
        if self.consent_given:
            self._enter(RUNG_CONTROL_WRITE, RUNNING, now)
        else:
            self._enter(RUNG_TRIAL, WAITING, now)

    def tick(self, now: datetime) -> None:
        s = self.state
        if s.state == IDLE:
            self._enter(s.rung, RUNNING, now)
            return
        if s.state != RUNNING:
            return
        if s.rung == RUNG_READ and s.since is not None and now - s.since >= READ_TIMEOUT:
            self._stop(READ_FAILED, now)
        elif s.rung == RUNG_TRIAL and s.trial_started is not None:
            done = (now - s.trial_started).total_seconds() / 3600.0
            s.hours_done = round(min(max(done, 0.0), float(self.params.trial_hours)), 1)
            if s.next_at is not None and now >= s.next_at:
                s.hours_done = float(self.params.trial_hours)
                self._after_trial(now)
        elif s.rung == RUNG_WINDOW and s.next_at is not None and now >= s.next_at:
            self._judge_window(now)

    def identify_ok(self, now: datetime) -> None:
        if self.state.rung == RUNG_IDENTIFY and self.state.state == RUNNING:
            self._enter(RUNG_READ, RUNNING, now)

    def read_ok(self, now: datetime) -> None:
        if self.state.rung == RUNG_READ and self.state.state == RUNNING:
            self._enter(RUNG_TRIAL, RUNNING, now)

    def would_write(self, n: int = 1) -> None:
        if self.state.rung == RUNG_TRIAL and self.state.state == RUNNING:
            self.state.would_write += max(int(n), 0)

    def foreign_write(self, now: datetime) -> None:
        """Obcy zapis stopuje szczebel w toku (próba, zapis kontrolny, okno); drabina czekająca na zgodę
        albo na okno nie zbiera dowodów — właściciel może zmieniać swój falownik bez napraw."""
        if self.state.state == RUNNING and self.state.rung >= RUNG_TRIAL:
            self.state.foreign_writes += 1
            self._stop(FOREIGN_WRITE, now)

    def consent(self, given: bool, now: datetime) -> None:
        self.consent_given = bool(given)
        s = self.state
        if given and s.rung == RUNG_TRIAL and s.state == WAITING:
            self._enter(RUNG_CONTROL_WRITE, RUNNING, now)
        elif not given and self._active() and s.rung >= RUNG_CONTROL_WRITE:
            self._stop(CONSENT_REVOKED, now)

    def write_result(self, readback_equal: bool | None, now: datetime) -> None:
        """Wynik zapisu kontrolnego: True = odczyt zwrotny równy, False = różny, None = odczyt nieudany."""
        if not (self.state.rung == RUNG_CONTROL_WRITE and self.state.state == RUNNING):
            return
        if readback_equal is True:
            self._enter(RUNG_WINDOW, WAITING, now)
        else:
            self._stop(READBACK_MISMATCH if readback_equal is False else READ_FAILED, now)

    def window_open(self, now: datetime) -> None:
        if self.state.rung == RUNG_WINDOW and self.state.state == WAITING and self.consent_given:
            self._enter(RUNG_WINDOW, RUNNING, now)

    def window_sample(self, charge_w: float | None, soc: float | None, now: datetime) -> None:
        if not self.window_running or not _finite(charge_w) or not _finite(soc):
            return
        self.state.samples.append((float(charge_w), float(soc)))

    def _judge_window(self, now: datetime) -> None:
        s = self.state
        target = float(s.target_w or self.params.window_power_w)
        if not s.samples or target <= 0:
            self._stop(WINDOW_DEVIATION, now)
            return
        mean = sum(p for p, _ in s.samples) / len(s.samples)
        s.measured_w = int(round(mean))
        s.deviation_pct = round(abs(mean - target) / target * 100.0, 1)
        rising = s.samples[-1][1] > s.samples[0][1]
        if s.deviation_pct <= MAX_DEVIATION_PCT and rising:
            s.state, s.since, s.next_at, s.samples = VERIFIED, now, None, []
        else:
            self._stop(WINDOW_DEVIATION, now)

    def conflict(self, now: datetime) -> None:
        if self._active():
            self._stop(CONTROLLER_CONFLICT, now)

    def abort(self, now: datetime) -> None:
        if self._active():
            self._stop(USER_ABORT, now)

    def retry(self, now: datetime) -> None:
        s = self.state
        if s.state != STOPPED:
            return
        rung = self.start_rung if s.stop_reason == IDENTIFY_CHANGED else s.rung
        if rung >= RUNG_CONTROL_WRITE and not self.consent_given:
            self._enter(RUNG_TRIAL, WAITING, now)
        elif rung == RUNG_WINDOW:
            self._enter(RUNG_WINDOW, WAITING, now)
        else:
            self._enter(rung, RUNNING, now)

    def device_changed(self, new_key: str, now: datetime) -> None:
        if new_key == self.device_key:
            return
        mid = self._active() or self.state.state == STOPPED
        self.device_key = new_key
        self.state = LadderState(rung=self.start_rung, state=IDLE, since=now)
        if mid:
            self._stop(IDENTIFY_CHANGED, now)

    # ── blok kontraktu ──

    def to_payload(self) -> dict:
        s = self.state
        out: dict = {"device_key": self.device_key, "rung": s.rung, "state": s.state,
                     "since": iso(s.since) if s.since is not None else None}
        if out["since"] is None:
            out.pop("since")
        if s.next_at is not None:
            out["next_at"] = iso(s.next_at)
        if s.state == STOPPED and s.stop_reason:
            out["stop_reason"] = s.stop_reason
            if s.stop_detail:
                out["stop_detail"] = s.stop_detail[:MAX_DETAIL]
        if s.trial_started is not None:
            out["trial"] = {"would_write": s.would_write, "foreign_writes": s.foreign_writes,
                            "hours_done": s.hours_done}
        if s.target_w is not None:
            window: dict = {"target_w": int(s.target_w)}
            if s.measured_w is not None:
                window["measured_w"] = s.measured_w
            if s.deviation_pct is not None:
                window["deviation_pct"] = s.deviation_pct
            out["window"] = window
        return out

    # ── magazyn ──

    def to_record(self) -> dict:
        s = self.state
        return {"v": RECORD_VERSION, "device_key": self.device_key, "start": self.start_rung, "rung": s.rung,
                "state": s.state, "since": iso(s.since) if s.since else None,
                "next_at": iso(s.next_at) if s.next_at else None, "stop_reason": s.stop_reason,
                "would_write": s.would_write, "foreign_writes": s.foreign_writes, "hours_done": s.hours_done,
                "trial_started": iso(s.trial_started) if s.trial_started else None,
                "target_w": s.target_w, "measured_w": s.measured_w, "deviation_pct": s.deviation_pct}

    @classmethod
    def from_record(cls, raw: Any, params: LadderParams) -> "Ladder | None":
        """Drabina z magazynu; zły kształt → None. Okno przerwane restartem czeka na nowe okno
        (próbki nie przeżywają restartu)."""
        if not isinstance(raw, Mapping) or raw.get("v") != RECORD_VERSION:
            return None
        key, start, rung, state = raw.get("device_key"), raw.get("start"), raw.get("rung"), raw.get("state")
        if not valid_device_key(key) or start not in START_RUNGS or state not in STATES:
            return None
        if not isinstance(rung, int) or isinstance(rung, bool) or not RUNG_NONE <= rung <= RUNG_WINDOW:
            return None
        since = _parse_iso(raw.get("since"))
        if (raw.get("since") is not None and since is None) or (state != IDLE and since is None):
            return None
        reason = raw.get("stop_reason")
        if (state == STOPPED) != (reason in STOP_REASONS):
            return None
        lad = cls(start, params, device_key=key)
        s = lad.state
        s.rung, s.state, s.since = rung, state, since
        s.next_at = _parse_iso(raw.get("next_at"))
        if state == STOPPED:
            s.stop_reason, s.stop_detail = reason, STOP_DETAILS[reason]
        s.would_write = _count(raw.get("would_write"))
        s.foreign_writes = _count(raw.get("foreign_writes"))
        hours = raw.get("hours_done")
        s.hours_done = float(hours) if _finite(hours) and hours >= 0 else 0.0
        s.trial_started = _parse_iso(raw.get("trial_started"))
        target = raw.get("target_w")
        s.target_w = target if isinstance(target, int) and not isinstance(target, bool) and target > 0 else None
        measured = raw.get("measured_w")
        s.measured_w = measured if isinstance(measured, int) and not isinstance(measured, bool) else None
        dev = raw.get("deviation_pct")
        s.deviation_pct = float(dev) if _finite(dev) else None
        if rung == RUNG_WINDOW and state == RUNNING:
            s.state, s.next_at = WAITING, None          # okno przerwane — nowe okno, nowe próbki
        if s.state == RUNNING and rung == RUNG_TRIAL and s.next_at is None:
            return None                                  # próba bez końca nie ruszy dalej
        return lad


def valid_record(raw: Any) -> bool:
    """Czy rekord drabiny z magazynu da się wczytać (parametry nie wpływają na kształt)."""
    return Ladder.from_record(raw, LadderParams(1, 5, 100)) is not None


def _finite(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _count(v: Any) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0
