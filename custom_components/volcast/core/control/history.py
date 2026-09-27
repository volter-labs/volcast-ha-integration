"""Statystyki godzinowe rekordera (`change`) → godziny importu historii zużycia.

Tylko pełne godziny z przeszłości (bieżąca jest niepełna), do 60 dni wstecz, jednostki
energii przeliczone na kWh; ujemna zmiana (wyzerowany licznik) i śmieci pomijane.
Znacznik godziny to początek godziny w UTC z jawną strefą (`...Z`) — chmura odrzuca
napis bez strefy.

Chmura przyjmuje w jednym żądaniu najwyżej `MAX_HOURS` godzin i ~1 MB ciała, więc
wysyłka idzie partiami (`batches`). Moduł jest czysty — bez Home Assistanta i I/O.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from typing import Mapping

HISTORY_DAYS = 60
MAX_HOURS = 1500
# Limit chmury to 1 000 000 bajtów ciała — zostawiamy zapas.
MAX_BATCH_BYTES = 900_000
_TO_KWH = {"kWh": 1.0, "Wh": 0.001, "MWh": 1000.0}
_SOURCE = "ha_recorder"


def _start(v) -> datetime | None:
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc) if v.tzinfo else None
    if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
        try:
            return datetime.fromtimestamp(v, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _index(rows: list[dict], unit: str | None) -> dict[datetime, float]:
    factor = _TO_KWH.get(unit or "")
    if factor is None:
        return {}
    out: dict[datetime, float] = {}
    for r in rows or ():
        if not isinstance(r, dict):
            continue
        start, change = _start(r.get("start")), r.get("change")
        if start is None or isinstance(change, bool) or not isinstance(change, (int, float)):
            continue
        if not math.isfinite(change) or change < 0 or start.minute or start.second or start.microsecond:
            continue
        out[start] = change * factor
    return out


def hours_from_statistics(load_rows: list[dict], *, load_unit: str | None, now_utc: datetime,
                          extra: Mapping[str, tuple[list[dict], str | None]] | None = None) -> list[dict]:
    """Godziny do wysłania: `{"start", "load_kwh", <klucze z extra>?}` rosnąco po czasie.

    `extra` to serie opcjonalne (`pv_kwh`, `import_kwh`, `export_kwh`) → (wiersze, jednostka);
    seria w jednostce nie-energii albo bez wiersza dla danej godziny po prostu nie trafia
    do tej godziny — chmura czyta brak jako „nieznane", nie jako zero.
    """
    hi = now_utc.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    lo = hi - timedelta(days=HISTORY_DAYS)
    load = _index(load_rows, load_unit)
    series = {name: _index(rows, unit) for name, (rows, unit) in (extra or {}).items()}
    out: list[dict] = []
    for start in sorted(load):
        if not (lo <= start < hi):
            continue
        hour = {"start": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "load_kwh": round(load[start], 4)}
        for name, idx in series.items():
            if start in idx:
                hour[name] = round(idx[start], 4)
        out.append(hour)
    return out[-MAX_HOURS:]


def batches(hours: list[dict], *, max_hours: int = MAX_HOURS,
            max_bytes: int = MAX_BATCH_BYTES) -> list[list[dict]]:
    """Podział na partie mieszczące się w limitach jednego żądania (kolejność zachowana).

    Rozmiar liczony jak serializacja klienta HTTP (`json.dumps` z domyślnymi separatorami).
    """
    envelope = len(json.dumps({"source": _SOURCE, "hours": []}).encode())
    out: list[list[dict]] = []
    cur: list[dict] = []
    size = envelope
    for hour in hours:
        item = len(json.dumps(hour).encode()) + 2          # + separator ", "
        if cur and (len(cur) >= max_hours or size + item > max_bytes):
            out.append(cur)
            cur, size = [], envelope
        cur.append(hour)
        size += item
    if cur:
        out.append(cur)
    return out


# Słowa wykluczające czujnik z roli „zużycie domu": to energia PV, sieci albo baterii.
_NOT_HOUSE_LOAD = ("pv", "solar", "grid", "export", "import", "battery", "charge", "feed",
                   "production", "generation", "yield", "backup", "ups")
_SUM_STATE_CLASSES = ("total", "total_increasing")


def house_load_candidate(energy_sensors) -> str | None:
    """Jedyny jednoznaczny czujnik energii zużycia domu z raportu wykrywania, inaczej None.

    Jednoznaczny = `sensor.` z jednostką energii i statystyką sumy, w nazwie słowo zużycia
    (`LOAD_HINTS`) i żadne słowo PV/sieci/baterii. Zero albo kilka takich — None: wybór
    zostaje dla właściciela (lepiej brak historii niż historia z niewłaściwego licznika).
    """
    from ..discovery.known import LOAD_HINTS

    found: list[str] = []
    for row in energy_sensors or ():
        if not isinstance(row, dict):
            continue
        eid = row.get("entity_id")
        if not isinstance(eid, str) or not eid.startswith("sensor."):
            continue
        if row.get("unit") not in _TO_KWH or row.get("state_class") not in _SUM_STATE_CLASSES:
            continue
        name = eid.lower()
        if any(h in name for h in LOAD_HINTS) and not any(x in name for x in _NOT_HOUSE_LOAD):
            found.append(eid)
    return found[0] if len(found) == 1 else None
