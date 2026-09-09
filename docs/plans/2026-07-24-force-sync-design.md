# Force sync — design

**Data:** 2026-07-24
**Status:** approved
**Branch:** `claude/force-sync` (baza: `claude/haeo-compat-pv-estimate`)

## Problem

Gdy HA jest wyłączone podczas update'u systemu, live tracker nie łapie danych tej
godziny (bucket nie powstaje), a mechanizmy samonaprawy nie pokrywają dzisiejszego
dnia: reconciler odpala się o 00:30 i na starcie HA, ale w obu przypadkach uzgadnia
wyłącznie D-1. Luka z dzisiejszego ranka wisi do północy, a użytkownik nie ma żadnego
sposobu, żeby wymusić uzupełnienie ręcznie (integracja nie rejestruje serwisów).

## Decyzje (z brainstormu)

1. **Startup reconcile obejmuje też dzisiejszy dzień** — restart HA to dokładnie
   moment, w którym powstają luki.
2. **Ręczny trigger: button + serwis** — `button.volcast_sync_now` (jeden klik w UI)
   oraz `volcast.sync_production` z opcjonalną datą (automatyzacje, power-userzy).
3. **Zakres ręcznego syncu bez daty: dziś + wczoraj** — jeden klik zamyka każdą lukę
   w oknie akceptacji backendu.

## Zakres zmian

**Tylko `volcast-ha-integration` (dystrybucja przez HACS).** Zero zmian w aplikacji
mobilnej i na serwerze:

- `submit-production` upsertuje po `(user_id, production_date, hour)` — ponowne
  wysłanie tej samej godziny jest bezpieczne,
- flaga `is_reconciliation: true` (PLAN-040) pomija update Kalmana i nowcastu —
  backfill dzisiejszego dnia nie zatruwa kalibracji,
- okno akceptacji backendu: 24h + 14h luzu na strefy czasowe (~38h) — pokrywa
  dziś i wczoraj.

## Architektura

### `reconciler.py`

- Nowa metoda `async reconcile_recent() -> list[ReconcileResult]` — uzgadnia
  **wczoraj, potem dziś** (kolejność celowa: diagnostyka `_last_target_date`
  kończy na dzisiejszym dniu).
- `reconcile_day(target_date)` dla `target_date == today` **wyklucza bieżącą
  godzinę lokalną i późniejsze**. Bieżąca godzina jest w trakcie — jej właścicielem
  jest live tracker (flush o :05 po pełnej godzinie). Bez wykluczenia wysłalibyśmy
  częściową wartość widoczną w aplikacji do czasu korekty upsertem.

### `__init__.py`

- Startup (`_on_started`): `reconcile_recent()` zamiast samego D-1.
- Codzienny przebieg 00:30: bez zmian (D-1).
- Rejestracja serwisu `volcast.sync_production`:
  - bez `date` → `reconcile_recent()` dla wszystkich config entries,
  - z `date` (YYYY-MM-DD) → `reconcile_day(date)`; daty poza oknem odbija
    istniejący gate `out_of_window`.
- `Platform.BUTTON` dołącza do `PLATFORMS`.

### `button.py` (nowy)

- Encja `button.volcast_sync_now` na stronie urządzenia; `async_press()` →
  `reconcile_recent()`. Idempotentne — bez luk nie ma żadnego POSTa, spam
  klikania jest bezpieczny.

### Diagnostyka

Wynik widoczny w istniejących sensorach (Last Reconciliation / Integration
Healthy) — `reconcile_day` już aktualizuje pola `_last_*` na każdej ścieżce.

## Pliki

| Plik | Zmiana |
|---|---|
| `reconciler.py` | `reconcile_recent()` + wykluczenie bieżącej godziny dla dzisiejszego dnia |
| `__init__.py` | startup → `reconcile_recent()`; rejestracja serwisu; `Platform.BUTTON` |
| `button.py` (nowy) | encja Sync now |
| `services.yaml` (nowy) | opis serwisu `sync_production` |
| `strings.json`, `translations/en.json` | teksty buttona i serwisu |
| `manifest.json` | bump wersji |
| `tests/` | wykluczenie bieżącej godziny, `reconcile_recent`, handler serwisu, button press |
| `README.md` | sekcja o force sync + ograniczenie recordera (niżej) |

## Edge case'y

- **Klik 00:00–00:30:** wczoraj wciąż w oknie backendu (24h + 14h luzu) — OK.
- **Recorder bez statystyk** (świeży restart, LTS jeszcze nie skompilowane):
  istniejący skip `no_stats`, widoczny w sensorze diagnostycznym; kolejny klik
  za parę minut zadziała.
- **Godzina, w której HA było całkiem wyłączone**, może nie mieć wiersza
  statystyk — energia z niej ląduje w delcie następnej godziny. Żaden sync tego
  nie rozdzieli (ograniczenie recordera); suma dzienna się zgadza. Do
  udokumentowania w README.
- **Wiele config entries:** button per entry; serwis bez daty iteruje wszystkie.

## Testowanie i release

- Pytest (istniejący harness w `tests/`), TDD.
- Test manualny z HA wycelowanym w `staging.volcast.app`.
- Merge do `main` i release HACS wyłącznie na polecenie użytkownika.
