"""Raport weryfikacji huba `modbus` przed zgodą na sterowanie — czysta logika, bez Home Assistanta.

Dwa pytania: (1) czy pod hubem jest urządzenie z profilu (rejestr 0 w `identify.expect`), (2) czy odczyty z rejestrów
huba zgadzają się z istniejącymi encjami użytkownika dla tych samych wielkości (SoC, moc baterii). Rozjazd =
raport i brak sterowania; brak encji użytkownika = „nie da się porównać” (raport, bez blokady).

Dlaczego brak encji nie blokuje: użytkownik, którego JEDYNYM źródłem danych jest hub (typowy Deye bez drugiej
integracji), nie miałby jak włączyć sterowania, a identyfikacja urządzenia i drabina weryfikacji zapisów
(odczyt zwrotny, kopia TOU) i tak zabezpieczają zapis. Brak porównania jest widoczny w raporcie (`cannot_compare`
albo `partial`) — blokujemy tylko to, co udowodnione rozjazdem.

Tolerancje (jednostki kanoniczne wtyczki: SoC w %, moc baterii w W, dodatnia = rozładowanie; encja użytkownika
jest po przeliczeniu jednostki i po fladze odwrócenia znaku baterii):
  * SoC: |hub − użytkownik| ≤ 5 punktów % — dwa źródła (BMS i falownik) zaokrąglają i odświeżają się osobno;
    SoC spoza 0–100 w hubie = rozjazd (zła skala albo adres);
  * moc baterii: |hub − użytkownik| ≤ max(300 W, 15 % większej z wartości) — próbki z różnych chwil (zmienna
    moc), skala 10× albo odwrócony znak wychodzą daleko poza pasmo. Odwrócony znak (suma w paśmie, różnica nie)
    dostaje znacznik `sign_inverted`.

Raport nie zawiera entity_id ani nazwy huba — tylko kody werdyktów i liczby (może iść do telemetrii).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from ..modbus.identity import identity_info
from ..registers import RegisterError, RegisterImage, decode, read_values

OK = "ok"
PARTIAL = "partial"                      # część wielkości zgodna, reszty nie było z czym porównać
CANNOT_COMPARE = "cannot_compare"        # żadnej wielkości nie dało się porównać (brak encji użytkownika)
MISMATCH = "mismatch"
IDENTITY_MISMATCH = "identity_mismatch"
BLOCKING = frozenset({MISMATCH, IDENTITY_MISMATCH})

SOC = "soc"
BATTERY_POWER = "battery_power_w"
QUANTITIES = (SOC, BATTERY_POWER)

SOC_TOLERANCE_PCT = 5.0
POWER_TOLERANCE_FLOOR_W = 300.0
POWER_TOLERANCE_REL = 0.15


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _tolerance(key: str, hub: float, user: float) -> float:
    if key == SOC:
        return SOC_TOLERANCE_PCT
    return max(POWER_TOLERANCE_FLOOR_W, POWER_TOLERANCE_REL * max(abs(hub), abs(user)))


def _compare(key: str, hub: float | None, user: float | None) -> dict[str, Any]:
    out: dict[str, Any] = {"hub": hub, "user": user, "deviation": None, "tolerance": None, "verdict": CANNOT_COMPARE}
    if key == BATTERY_POWER:
        out["sign_inverted"] = False
    if hub is None:
        return out
    if key == SOC and not 0 <= hub <= 100:
        out.update(verdict=MISMATCH, tolerance=SOC_TOLERANCE_PCT)
        return out
    if user is None:
        return out
    tol = _tolerance(key, hub, user)
    dev = abs(hub - user)
    out.update(deviation=round(dev, 3), tolerance=round(tol, 3), verdict=OK if dev <= tol else MISMATCH)
    if key == BATTERY_POWER and out["verdict"] == MISMATCH and abs(hub) > tol and abs(hub + user) <= tol:
        out["sign_inverted"] = True
    return out


def _device_type(profile, image: RegisterImage) -> float | None:
    spec = profile.raw.get("identify", {}).get("registers", {}).get("device_type")
    if not spec:
        return None
    try:
        return _num(decode(spec, image))
    except RegisterError:
        return None


def build_report(profile, image: RegisterImage, user: Mapping[str, float | None], *, now: datetime) -> dict[str, Any]:
    """Raport z obrazu rejestrów huba i wartości użytkownika ({wielkość: liczba | None}, kanoniczne)."""
    identity_ok = bool(identity_info(profile, image)["matched"])
    hub_values = read_values(profile.raw.get("read", {}), image)
    quantities = {k: _compare(k, _num(hub_values.get(k)), _num(user.get(k))) for k in QUANTITIES}
    verdicts = {q["verdict"] for q in quantities.values()}
    if not identity_ok:
        verdict = IDENTITY_MISMATCH
    elif MISMATCH in verdicts:
        verdict = MISMATCH
    elif verdicts == {OK}:
        verdict = OK
    elif OK in verdicts:
        verdict = PARTIAL
    else:
        verdict = CANNOT_COMPARE
    dt = _device_type(profile, image)
    return {"verdict": verdict, "identity_ok": identity_ok,
            "device_type": int(dt) if dt is not None and float(dt).is_integer() else dt,
            "checked_at": now.isoformat(), "quantities": quantities}


def blocks_control(report: Mapping[str, Any] | None) -> bool:
    """Raport z rozjazdem (albo innym urządzeniem pod adresem) wyklucza włączenie sterowania."""
    return bool(report) and report.get("verdict") in BLOCKING
