# Force Sync Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Samonaprawa luk produkcji po restarcie HA (startup reconcile obejmuje dzisiejszy dzień) + ręczny force sync (button + serwis `volcast.sync_production`).

**Architecture:** Rozszerzenie istniejącego `DailyReconciler` o `reconcile_recent()` (wczoraj + dziś, z wykluczeniem bieżącej godziny) wywoływane ze startupu, z nowej encji button i z nowego serwisu HA. Zero zmian backendowych — `submit-production` upsertuje po `(user_id, production_date, hour)` i honoruje `is_reconciliation: true`.

**Tech Stack:** Python (HA custom integration), pytest + pytest-asyncio (harness w `tests/conftest.py` stubuje moduły HA), HACS.

**Design doc:** `docs/plans/2026-07-24-force-sync-design.md`
**Branch:** `claude/force-sync` (baza: `claude/haeo-compat-pv-estimate`)
**Repo root:** `C:\Users\yakon\Documents\Volter\volcast-ha-integration`

**Uruchamianie testów** (z repo root):

```powershell
python -m pytest tests/ -v
```

**Konwencje repo:** komentarze/docstringi mieszane PL/EN (nowe piszemy po polsku tam, gdzie sąsiedni kod jest polski), commity bez push do `main`, wszystko na branchu `claude/force-sync`.

---

## Task 1: Seam czasu `_now_local` + wykluczenie bieżącej godziny dla dzisiejszego dnia

Reconciler dla `target_date == today` nie może wysłać bieżącej (niedokończonej) godziny — jej właścicielem jest live tracker (flush o :05). Wzorzec seam-a do mrożenia czasu skopiowany z `production._utcnow_date()` (patrz `tests/test_production.py::test_mark_accepted_persists_to_store`).

**Files:**
- Modify: `custom_components/volcast/reconciler.py`
- Test: `tests/test_reconciler.py`

**Step 1: Write the failing test**

Dopisz na końcu `tests/test_reconciler.py` (sekcja z nagłówkiem jak istniejące):

```python
# ---------------------------------------------------------------------------
# reconcile_day(today) — wykluczenie bieżącej godziny (force sync)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_today_excludes_current_and_future_hours(monkeypatch):
    """Reconciling TODAY must not submit the current (in-progress) hour nor later.

    The live tracker owns the current hour (flush at :05 past the hour).
    Submitting a partial value would show garbage in the app until the
    backend upsert corrects it an hour later.
    """
    from custom_components.volcast import reconciler as reconciler_mod
    from custom_components.volcast.http_retry import RetryResult

    tracker = _make_real_tracker()
    reconciler = _make_reconciler(tracker=tracker)

    # Zamrożony czas lokalny: 2026-07-24 13:40 Europe/Warsaw (tz z FakeHass)
    fixed_now = datetime(2026, 7, 24, 13, 40, tzinfo=ZoneInfo("Europe/Warsaw"))
    monkeypatch.setattr(reconciler_mod, "_now_local", lambda tz: fixed_now)
    target = fixed_now.date()  # DZIŚ

    # Statystyki raportują godziny 6..15 — 13 to bieżąca (partial LTS),
    # 14-15 to szum. Godziny 6..10 już dostarczone przez live tracker.
    stats = {h: 1.0 for h in range(6, 16)}
    tracker._accepted = {target.isoformat(): list(range(6, 11))}

    with patch.object(DailyReconciler, "_fetch_ha_statistics", return_value=stats), \
         patch("custom_components.volcast.reconciler.http_with_retry") as mock_http, \
         patch("custom_components.volcast.reconciler.async_get_clientsession",
               return_value=MagicMock()):
        mock_http.return_value = RetryResult(
            success=True, status=200, attempts=1,
            data={"accepted": 2, "rejected": 0, "rejections": []},
        )
        result = await reconciler.reconcile_day(target)

    submitted_hours = [
        r["hour"] for r in mock_http.call_args.kwargs["payload"]["readings"]
    ]
    assert submitted_hours == [11, 12], (
        "only gap hours strictly before the current hour may be submitted"
    )
    assert result.submitted == 2


@pytest.mark.asyncio
async def test_reconcile_yesterday_still_submits_all_hours(monkeypatch):
    """Wykluczenie bieżącej godziny dotyczy TYLKO dzisiejszego dnia —
    wczorajszy dzień wysyła wszystkie luki niezależnie od pory."""
    from custom_components.volcast import reconciler as reconciler_mod
    from custom_components.volcast.http_retry import RetryResult

    tracker = _make_real_tracker()
    reconciler = _make_reconciler(tracker=tracker)

    fixed_now = datetime(2026, 7, 24, 13, 40, tzinfo=ZoneInfo("Europe/Warsaw"))
    monkeypatch.setattr(reconciler_mod, "_now_local", lambda tz: fixed_now)
    target = fixed_now.date() - timedelta(days=1)  # WCZORAJ

    stats = {h: 1.0 for h in range(6, 19)}  # 13 godzin, w tym >= 13
    tracker._accepted = {}

    with patch.object(DailyReconciler, "_fetch_ha_statistics", return_value=stats), \
         patch("custom_components.volcast.reconciler.http_with_retry") as mock_http, \
         patch("custom_components.volcast.reconciler.async_get_clientsession",
               return_value=MagicMock()):
        mock_http.return_value = RetryResult(
            success=True, status=200, attempts=1,
            data={"accepted": 13, "rejected": 0, "rejections": []},
        )
        result = await reconciler.reconcile_day(target)

    submitted_hours = [
        r["hour"] for r in mock_http.call_args.kwargs["payload"]["readings"]
    ]
    assert submitted_hours == list(range(6, 19))
    assert result.submitted == 13
```

**Step 2: Run tests to verify they fail**

```powershell
python -m pytest tests/test_reconciler.py::test_reconcile_today_excludes_current_and_future_hours tests/test_reconciler.py::test_reconcile_yesterday_still_submits_all_hours -v
```

Expected: FAIL — `AttributeError: ... has no attribute '_now_local'` (seam jeszcze nie istnieje).

**Step 3: Write minimal implementation**

W `custom_components/volcast/reconciler.py`:

(a) Po `MIN_REPORT_KWH = 0.001` dodaj seam:

```python
def _now_local(tz) -> datetime:
    """Bieżący czas w strefie tz. Module-level seam for test monkeypatching
    (ten sam wzorzec co production._utcnow_date)."""
    return datetime.now(tz)
```

(b) W `_reconcile_day_impl` zamień pierwszą linię:

```python
        today = datetime.now(self._tz).date()
```

na:

```python
        now_local = _now_local(self._tz)
        today = now_local.date()
```

(c) W pętli budującej `missing` dodaj wykluczenie jako PIERWSZY warunek:

```python
        is_today = target_date == today
        missing: list[dict] = []
        for hour, kwh in sorted(hourly_stats.items()):
            if is_today and hour >= now_local.hour:
                # Bieżąca (niedokończona) godzina należy do live trackera —
                # flush o :05 po pełnej godzinie. Częściowa wartość wisiałaby
                # w aplikacji do czasu korekty upsertem.
                continue
            if hour in accepted_hours:
                continue
            ...  # reszta bez zmian
```

**Step 4: Run tests to verify they pass — plus cały plik na regresje**

```powershell
python -m pytest tests/test_reconciler.py -v
```

Expected: wszystkie PASS (istniejące testy używają realnego `datetime.now` przez seam — zachowanie identyczne).

**Step 5: Commit**

```powershell
git add custom_components/volcast/reconciler.py tests/test_reconciler.py
git commit -m "feat(reconciler): exclude current hour when reconciling today (TDD)"
```

---

## Task 2: `reconcile_recent()` — wczoraj + dziś

**Files:**
- Modify: `custom_components/volcast/reconciler.py`
- Test: `tests/test_reconciler.py`

**Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_reconcile_recent_runs_yesterday_then_today(monkeypatch):
    """reconcile_recent uzgadnia wczoraj, potem dziś — kolejność celowa,
    żeby diagnostyka _last_target_date kończyła na dzisiejszym dniu."""
    from custom_components.volcast import reconciler as reconciler_mod

    fixed_now = datetime(2026, 7, 24, 13, 40, tzinfo=ZoneInfo("Europe/Warsaw"))
    monkeypatch.setattr(reconciler_mod, "_now_local", lambda tz: fixed_now)

    reconciler = _make_reconciler()
    calls: list[date] = []

    async def fake_reconcile_day(target):
        calls.append(target)
        return ReconcileResult(success=True)

    with patch.object(reconciler, "reconcile_day", side_effect=fake_reconcile_day):
        results = await reconciler.reconcile_recent()

    assert calls == [date(2026, 7, 23), date(2026, 7, 24)]
    assert len(results) == 2
    assert all(r.success for r in results)
```

**Step 2: Run test to verify it fails**

```powershell
python -m pytest tests/test_reconciler.py::test_reconcile_recent_runs_yesterday_then_today -v
```

Expected: FAIL — `AttributeError: 'DailyReconciler' object has no attribute 'reconcile_recent'`.

**Step 3: Write minimal implementation**

W `DailyReconciler`, bezpośrednio po metodzie `reconcile_day`:

```python
    async def reconcile_recent(self) -> list[ReconcileResult]:
        """Uzgodnij wczoraj + dziś (w tej kolejności). Idempotentne.

        Używane przez: startup HA, button `sync_now`, serwis
        `volcast.sync_production` bez daty. Kolejność celowa — diagnostyka
        `_last_target_date` kończy na dzisiejszym dniu, co po ręcznym syncu
        daje bardziej użyteczną wartość w sensorze.
        """
        today = _now_local(self._tz).date()
        results: list[ReconcileResult] = []
        for target in (today - timedelta(days=1), today):
            results.append(await self.reconcile_day(target))
        return results
```

**Step 4: Run tests to verify they pass**

```powershell
python -m pytest tests/test_reconciler.py -v
```

Expected: PASS.

**Step 5: Commit**

```powershell
git add custom_components/volcast/reconciler.py tests/test_reconciler.py
git commit -m "feat(reconciler): reconcile_recent() — yesterday + today (TDD)"
```

---

## Task 3: Startup HA uzgadnia D-1 + D-0

**Files:**
- Modify: `custom_components/volcast/__init__.py` (funkcja `_setup_reconciler`, wewnętrzne `_on_started`)
- Test: `tests/test_reconciler.py`

**Step 1: Write the failing test**

Dopisz w sekcji `_setup_reconciler — trigger wiring`:

```python
@pytest.mark.asyncio
async def test_setup_reconciler_startup_runs_reconcile_recent():
    """On-startup hook wywołuje reconcile_recent (D-1 + D-0), nie samo D-1.

    Restart HA to dokładnie moment powstawania luk (update systemu) —
    dzisiejsze braki muszą się uzupełnić od razu, nie o 00:30.
    """
    from custom_components.volcast import _setup_reconciler

    hass = FakeHass(is_running=False)
    entry = _RecordingConfigEntry()
    tracker = _make_setup_tracker(hass)

    with patch("custom_components.volcast.async_track_time_change",
               return_value=lambda: None):
        _setup_reconciler(
            hass=hass, entry=entry, tracker=tracker,
            energy_entity="sensor.pv_energy", api_key="k", submit_url="http://x",
        )

    _, listener = hass.bus.listeners[0]
    with patch.object(DailyReconciler, "reconcile_recent",
                      new=AsyncMock()) as mock_recent:
        await listener(None)

    assert mock_recent.await_count == 1
```

**Step 2: Run test to verify it fails**

```powershell
python -m pytest tests/test_reconciler.py::test_setup_reconciler_startup_runs_reconcile_recent -v
```

Expected: FAIL — `reconcile_recent` nie jest wywoływane (listener woła `reconcile_day`), `await_count == 0`.

**Step 3: Write minimal implementation**

W `custom_components/volcast/__init__.py` zamień `_on_started` (linie ~155-163) — usuń stary komentarz o D-2 i podmień ciało:

```python
    # Na startupie HA — uzgodnij wczoraj + dziś. Restart HA to dokładnie
    # moment, w którym powstają luki (update systemu = restart). Idempotentne:
    # godziny już dostarczone i bieżąca godzina (własność live trackera) są
    # pomijane wewnątrz reconcile_recent/reconcile_day.
    async def _on_started(_event=None):
        await reconciler.reconcile_recent()
```

**Step 4: Run test to verify it passes — plus cały plik**

```powershell
python -m pytest tests/test_reconciler.py -v
```

Expected: PASS. Uwaga: `test_setup_reconciler_skips_listener_cancel_after_started_fired` patchuje `DailyReconciler.reconcile_day` — nadal przechodzi, bo realny `reconcile_recent` woła spatchowany `reconcile_day`.

**Step 5: Commit**

```powershell
git add custom_components/volcast/__init__.py tests/test_reconciler.py
git commit -m "feat(init): startup reconcile covers yesterday AND today (TDD)"
```

---

## Task 4: Stuby conftest dla serwisów

Serwis (Task 5) i jego testy potrzebują `hass.services` i `homeassistant.exceptions` w harnessie.

**Files:**
- Modify: `tests/conftest.py`

**Step 1: Dodaj stub modułu `homeassistant.exceptions`**

Po bloku `_make_module("homeassistant.const", {...})` dodaj:

```python
# --- homeassistant.exceptions ---
class _FakeServiceValidationError(Exception):
    """Stub for ServiceValidationError."""


_make_module("homeassistant.exceptions", {
    "ServiceValidationError": _FakeServiceValidationError,
    "HomeAssistantError": Exception,
})
```

Oraz w `_make_module("homeassistant.core", {...})` dopisz klucz:

```python
    "ServiceCall": MagicMock(),
```

**Step 2: Dodaj `_FakeServices` + podłącz do `FakeHass`**

Nad klasą `FakeHass` dodaj:

```python
class _FakeServices:
    """Stub for hass.services — records registrations."""

    def __init__(self):
        self.registered: dict[tuple[str, str], Any] = {}

    def has_service(self, domain: str, service: str) -> bool:
        return (domain, service) in self.registered

    def async_register(self, domain: str, service: str, handler, schema=None):
        self.registered[(domain, service)] = handler

    def async_remove(self, domain: str, service: str) -> None:
        self.registered.pop((domain, service), None)
```

W `FakeHass.__init__` dopisz:

```python
        self.services = _FakeServices()
```

**Step 3: Run full suite — stuby nie mogą nic zepsuć**

```powershell
python -m pytest tests/ -v
```

Expected: wszystkie istniejące testy PASS.

**Step 4: Commit**

```powershell
git add tests/conftest.py
git commit -m "test(conftest): stub hass.services + homeassistant.exceptions"
```

---

## Task 5: Serwis `volcast.sync_production`

Domain-level serwis: bez `date` → `reconcile_recent()` na wszystkich entries; z `date` → `reconcile_day(date)`. Rejestrowany raz (guard `has_service`), usuwany przy unloadzie ostatniego entry.

**Files:**
- Modify: `custom_components/volcast/const.py`
- Modify: `custom_components/volcast/__init__.py`
- Create: `tests/test_services.py`

**Step 1: Write the failing tests**

Utwórz `tests/test_services.py`:

```python
"""Tests for the volcast.sync_production service."""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.conftest import FakeHass


class _FakeServiceCall:
    """Minimal ServiceCall double — only .data is used by the handler."""

    def __init__(self, data: dict | None = None):
        self.data = data or {}


def _register(hass: FakeHass):
    """Zarejestruj serwis i zwróć handler z fake rejestru."""
    from custom_components.volcast import _async_register_services
    from custom_components.volcast.const import DOMAIN, SERVICE_SYNC_PRODUCTION

    _async_register_services(hass)
    return hass.services.registered[(DOMAIN, SERVICE_SYNC_PRODUCTION)]


def _hass_with_reconciler(reconciler) -> FakeHass:
    hass = FakeHass()
    hass.data["volcast"] = {"entry1": {"reconciler": reconciler}}
    return hass


def test_register_services_is_idempotent():
    """Podwójna rejestracja (2 config entries) nie nadpisuje handlera."""
    from custom_components.volcast import _async_register_services
    from custom_components.volcast.const import DOMAIN, SERVICE_SYNC_PRODUCTION

    hass = FakeHass()
    _async_register_services(hass)
    first = hass.services.registered[(DOMAIN, SERVICE_SYNC_PRODUCTION)]
    _async_register_services(hass)
    assert hass.services.registered[(DOMAIN, SERVICE_SYNC_PRODUCTION)] is first


@pytest.mark.asyncio
async def test_sync_without_date_calls_reconcile_recent():
    reconciler = MagicMock()
    reconciler.reconcile_recent = AsyncMock()
    hass = _hass_with_reconciler(reconciler)
    handler = _register(hass)

    await handler(_FakeServiceCall())

    reconciler.reconcile_recent.assert_awaited_once()
    reconciler.reconcile_day = getattr(reconciler, "reconcile_day", None)


@pytest.mark.asyncio
async def test_sync_with_date_calls_reconcile_day():
    reconciler = MagicMock()
    reconciler.reconcile_day = AsyncMock()
    hass = _hass_with_reconciler(reconciler)
    handler = _register(hass)

    await handler(_FakeServiceCall({"date": "2026-07-23"}))

    reconciler.reconcile_day.assert_awaited_once_with(date(2026, 7, 23))


@pytest.mark.asyncio
async def test_sync_with_date_object_calls_reconcile_day():
    """Selector date w HA może dostarczyć datetime.date, nie str."""
    reconciler = MagicMock()
    reconciler.reconcile_day = AsyncMock()
    hass = _hass_with_reconciler(reconciler)
    handler = _register(hass)

    await handler(_FakeServiceCall({"date": date(2026, 7, 23)}))

    reconciler.reconcile_day.assert_awaited_once_with(date(2026, 7, 23))


@pytest.mark.asyncio
async def test_sync_invalid_date_raises_validation_error():
    from homeassistant.exceptions import ServiceValidationError

    reconciler = MagicMock()
    hass = _hass_with_reconciler(reconciler)
    handler = _register(hass)

    with pytest.raises(ServiceValidationError):
        await handler(_FakeServiceCall({"date": "not-a-date"}))


@pytest.mark.asyncio
async def test_sync_no_reconcilers_raises_validation_error():
    """Entry bez production trackingu (brak energy_entity) → czytelny błąd."""
    from homeassistant.exceptions import ServiceValidationError

    hass = FakeHass()
    hass.data["volcast"] = {"entry1": {"reconciler": None}}
    handler = _register(hass)

    with pytest.raises(ServiceValidationError):
        await handler(_FakeServiceCall())


@pytest.mark.asyncio
async def test_sync_iterates_all_entries():
    """Serwis bez daty działa na wszystkich config entries z reconcilerem."""
    rec1, rec2 = MagicMock(), MagicMock()
    rec1.reconcile_recent = AsyncMock()
    rec2.reconcile_recent = AsyncMock()
    hass = FakeHass()
    hass.data["volcast"] = {
        "entry1": {"reconciler": rec1},
        "entry2": {"reconciler": rec2},
    }
    handler = _register(hass)

    await handler(_FakeServiceCall())

    rec1.reconcile_recent.assert_awaited_once()
    rec2.reconcile_recent.assert_awaited_once()
```

**Step 2: Run tests to verify they fail**

```powershell
python -m pytest tests/test_services.py -v
```

Expected: FAIL — `ImportError: cannot import name '_async_register_services'`.

**Step 3: Write minimal implementation**

(a) `custom_components/volcast/const.py` — dopisz na końcu:

```python
SERVICE_SYNC_PRODUCTION = "sync_production"
ATTR_DATE = "date"
```

(b) `custom_components/volcast/__init__.py`:

Importy — rozszerz istniejące:

```python
from datetime import date, datetime, timedelta
```

```python
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
```

Z `.const` dodatkowo importuj `ATTR_DATE, SERVICE_SYNC_PRODUCTION`.

Nowa funkcja (nad `async_setup_entry`):

```python
def _async_register_services(hass: HomeAssistant) -> None:
    """Zarejestruj domain-level serwis volcast.sync_production (idempotentnie).

    Bez `date` → reconcile_recent() (wczoraj + dziś) na wszystkich entries.
    Z `date` (YYYY-MM-DD lub datetime.date z selectora) → reconcile_day(date);
    daty poza oknem odbija istniejący gate `out_of_window` w reconcile_day.
    """
    if hass.services.has_service(DOMAIN, SERVICE_SYNC_PRODUCTION):
        return

    async def _handle_sync_production(call: ServiceCall) -> None:
        raw_date = call.data.get(ATTR_DATE)
        target: date | None = None
        if raw_date is not None:
            if isinstance(raw_date, date):
                target = raw_date
            else:
                try:
                    target = date.fromisoformat(str(raw_date))
                except ValueError as err:
                    raise ServiceValidationError(
                        f"Invalid date {raw_date!r} — expected YYYY-MM-DD"
                    ) from err

        reconcilers = [
            entry_data["reconciler"]
            for entry_data in hass.data.get(DOMAIN, {}).values()
            if entry_data.get("reconciler") is not None
        ]
        if not reconcilers:
            raise ServiceValidationError(
                "No Volcast entry has production tracking configured "
                "(an energy sensor is required for sync)"
            )
        for reconciler in reconcilers:
            if target is not None:
                await reconciler.reconcile_day(target)
            else:
                await reconciler.reconcile_recent()

    hass.services.async_register(
        DOMAIN, SERVICE_SYNC_PRODUCTION, _handle_sync_production
    )
```

W `async_setup_entry`, bezpośrednio przed `await hass.config_entries.async_forward_entry_setups(...)`:

```python
    _async_register_services(hass)
```

W `async_unload_entry`, po `hass.data[DOMAIN].pop(entry.entry_id)` (wewnątrz `if unload_ok`):

```python
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_SYNC_PRODUCTION)
```

**Step 4: Run tests to verify they pass**

```powershell
python -m pytest tests/test_services.py tests/test_reconciler.py -v
```

Expected: PASS.

**Step 5: Commit**

```powershell
git add custom_components/volcast/const.py custom_components/volcast/__init__.py tests/test_services.py
git commit -m "feat(service): volcast.sync_production with optional date (TDD)"
```

---

## Task 6: Opisy serwisu — services.yaml + strings + translations

Hassfest (CI: `.github/workflows/hassfest.yaml`) waliduje spójność `services.yaml` ↔ `strings.json`.

**Files:**
- Create: `custom_components/volcast/services.yaml`
- Modify: `custom_components/volcast/strings.json`
- Modify: `custom_components/volcast/translations/en.json`

**Step 1: Utwórz `services.yaml`**

```yaml
sync_production:
  fields:
    date:
      required: false
      example: "2026-07-23"
      selector:
        date:
```

**Step 2: Dodaj sekcję `services` do `strings.json`**

Po sekcji `"issues"` (przed `"entity"`), na tym samym poziomie:

```json
  "services": {
    "sync_production": {
      "name": "Sync production",
      "description": "Force-syncs hourly PV production to Volcast using Home Assistant recorder statistics. Fills gaps left by HA downtime (e.g. a system update). Without a date, syncs yesterday and today.",
      "fields": {
        "date": {
          "name": "Date",
          "description": "Specific day to sync (must be within the last ~24-38 hours). Leave empty to sync yesterday and today."
        }
      }
    }
  },
```

**Step 3: Identyczna sekcja w `translations/en.json`** (ten sam JSON, ta sama pozycja — po `"options"`, przed `"entity"`; `translations/en.json` nie ma sekcji `"issues"`).

**Step 4: Walidacja JSON**

```powershell
python -c "import json; json.load(open('custom_components/volcast/strings.json', encoding='utf-8')); json.load(open('custom_components/volcast/translations/en.json', encoding='utf-8')); print('OK')"
```

Expected: `OK`.

**Step 5: Commit**

```powershell
git add custom_components/volcast/services.yaml custom_components/volcast/strings.json custom_components/volcast/translations/en.json
git commit -m "feat(service): services.yaml + strings for sync_production"
```

---

## Task 7: Button `sync_now`

**Files:**
- Modify: `tests/conftest.py` (stub `homeassistant.components.button`)
- Create: `custom_components/volcast/button.py`
- Modify: `custom_components/volcast/__init__.py` (PLATFORMS)
- Modify: `custom_components/volcast/strings.json`, `custom_components/volcast/translations/en.json` (nazwa encji)
- Create: `tests/test_button.py`

**Step 1: Stub w conftest**

Po bloku `homeassistant.components.binary_sensor` dodaj:

```python
# --- homeassistant.components.button ---
class _FakeButtonEntity:
    _attr_has_entity_name = False
    _attr_unique_id = None
    _attr_device_info = None


_make_module("homeassistant.components.button", {
    "ButtonEntity": _FakeButtonEntity,
})
```

**Step 2: Write the failing tests**

Utwórz `tests/test_button.py`:

```python
"""Tests for the Volcast sync-now button."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.conftest import FakeHass


class _FakeConfigEntryForButton:
    entry_id = "test_entry_id"


def _added_entities(hass: FakeHass, entry) -> list:
    """Uruchom async_setup_entry platformy button i zwróć dodane encje."""
    import asyncio

    from custom_components.volcast.button import async_setup_entry

    added: list = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    asyncio.get_event_loop().run_until_complete(
        async_setup_entry(hass, entry, _capture)
    )
    return added


@pytest.mark.asyncio
async def test_setup_adds_button_when_reconciler_present():
    from custom_components.volcast.button import async_setup_entry

    hass = FakeHass()
    reconciler = MagicMock()
    hass.data["volcast"] = {"test_entry_id": {"reconciler": reconciler}}

    added: list = []
    await async_setup_entry(
        hass, _FakeConfigEntryForButton(), lambda ents, **kw: added.extend(ents)
    )

    assert len(added) == 1
    assert added[0]._attr_unique_id == "test_entry_id_sync_now"


@pytest.mark.asyncio
async def test_setup_skips_button_without_reconciler():
    """Bez energy_entity nie ma reconcilera — button nie powstaje."""
    from custom_components.volcast.button import async_setup_entry

    hass = FakeHass()
    hass.data["volcast"] = {"test_entry_id": {"reconciler": None}}

    added: list = []
    await async_setup_entry(
        hass, _FakeConfigEntryForButton(), lambda ents, **kw: added.extend(ents)
    )

    assert added == []


@pytest.mark.asyncio
async def test_press_runs_reconcile_recent():
    from custom_components.volcast.button import VolcastSyncButton

    reconciler = MagicMock()
    reconciler.reconcile_recent = AsyncMock(return_value=[])
    button = VolcastSyncButton(reconciler, "test_entry_id")

    await button.async_press()

    reconciler.reconcile_recent.assert_awaited_once()
```

**Step 3: Run tests to verify they fail**

```powershell
python -m pytest tests/test_button.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'custom_components.volcast.button'`.

**Step 4: Write minimal implementation**

Utwórz `custom_components/volcast/button.py`:

```python
"""Button platform for Volcast — ręczny force sync produkcji."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .reconciler import DailyReconciler

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Volcast buttons from a config entry."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    reconciler: DailyReconciler | None = entry_data.get("reconciler")
    if reconciler is None:
        # Bez energy_entity nie ma czego synchronizować — nie dodawaj buttona.
        return
    async_add_entities([VolcastSyncButton(reconciler, entry.entry_id)])


class VolcastSyncButton(ButtonEntity):
    """Button: ręczne uzgodnienie produkcji (wczoraj + dziś).

    Idempotentne — godziny już dostarczone i bieżąca godzina są pomijane,
    więc wielokrotne klikanie jest bezpieczne (bez luk = zero POSTów).
    """

    _attr_has_entity_name = True
    _attr_translation_key = "sync_now"
    _attr_icon = "mdi:cloud-sync"

    def __init__(self, reconciler: DailyReconciler, entry_id: str) -> None:
        self._reconciler = reconciler
        self._attr_unique_id = f"{entry_id}_sync_now"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry_id)},
            "name": "Volcast Solar Forecast",
            "manufacturer": "Volter Labs",
            "model": "PV Forecast",
            "entry_type": "service",
        }

    async def async_press(self) -> None:
        """Klik → uzgodnij wczoraj + dziś."""
        results = await self._reconciler.reconcile_recent()
        _LOGGER.info(
            "Manual sync pressed: %s",
            "; ".join(
                (r.reason or ("ok" if r.success else "fail"))
                + f" submitted={r.submitted}"
                for r in results
            ) or "no results",
        )
```

W `custom_components/volcast/__init__.py` zamień:

```python
PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]
```

na:

```python
PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON]
```

W `strings.json` ORAZ `translations/en.json`, w sekcji `"entity"` po `"binary_sensor"` dodaj:

```json
    "button": {
      "sync_now": {
        "name": "Sync production now"
      }
    }
```

**Step 5: Run tests to verify they pass**

```powershell
python -m pytest tests/test_button.py -v
python -c "import json; json.load(open('custom_components/volcast/strings.json', encoding='utf-8')); json.load(open('custom_components/volcast/translations/en.json', encoding='utf-8')); print('OK')"
```

Expected: PASS + `OK`.

**Step 6: Commit**

```powershell
git add custom_components/volcast/button.py custom_components/volcast/__init__.py custom_components/volcast/strings.json custom_components/volcast/translations/en.json tests/conftest.py tests/test_button.py
git commit -m "feat(button): sync-now button entity (TDD)"
```

---

## Task 8: README, bump wersji, pełna weryfikacja

**Files:**
- Modify: `README.md`
- Modify: `custom_components/volcast/manifest.json`

**Step 1: Sekcja w README**

Znajdź sekcję o production tracking / reconciliation w `README.md` i dodaj pod nią:

```markdown
### Manual sync (force sync)

If Home Assistant was offline (system update, reboot) and an hour of production
is missing in Volcast, the integration self-heals: on every HA start and daily
at 00:30 it reconciles gaps from recorder statistics (yesterday and today).

To force it manually:

- Press the **Sync production now** button on the Volcast device page, or
- Call the `volcast.sync_production` action (optionally with a `date` within
  the last ~24–38 hours).

Both are safe to repeat — hours already delivered are skipped.

**Limitation:** if HA was fully down during an hour, the recorder may have no
statistics row for it. The energy is not lost — it lands in the next hour's
delta — but no sync can split it back. Daily totals stay correct.
```

Jeśli README nie ma oczywistego miejsca, dodaj sekcję na końcu przed licencją/FAQ — executor decyduje po przeczytaniu pliku.

**Step 2: Bump wersji**

W `custom_components/volcast/manifest.json`: `"version": "1.6.2-beta1"` → `"version": "1.7.0-beta1"` (nowa funkcja = minor bump; beta do czasu testu na staging).

**Step 3: Pełna suita + walidacja JSON**

```powershell
python -m pytest tests/ -v
python -c "import json; json.load(open('custom_components/volcast/manifest.json', encoding='utf-8')); print('OK')"
```

Expected: wszystkie testy PASS, `OK`.

**Step 4: Commit**

```powershell
git add README.md custom_components/volcast/manifest.json
git commit -m "docs+chore: force sync README section, bump to 1.7.0-beta1"
```

---

## Weryfikacja końcowa (Phase 5)

1. `python -m pytest tests/ -v` — 0 failures.
2. Review zmian vs design doc (`docs/plans/2026-07-24-force-sync-design.md`).
3. Push brancha `claude/force-sync` na GitHub (feature branch — dozwolone).
4. Test manualny: HA deva (kontener na NAS) z integracją wskazującą `staging.volcast.app` — klik buttona, wywołanie serwisu z datą, sprawdzenie logów integracji + `pv_actual_production_hourly` na stagingu.
5. Merge do `main` + release HACS — WYŁĄCZNIE na polecenie użytkownika.

## Rollback

Feature branch — `git branch -D claude/force-sync` przed merge. Po release: poprzedni tag HACS pozostaje instalowalny; zmiany nie dotykają backendu ani danych, więc rollback = downgrade wersji integracji.
