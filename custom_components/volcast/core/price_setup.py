"""Wykrywanie źródeł cen w Home Assistancie i walidacja prognozy przy wyborze.

Warstwa CZYSTA — bez importów Home Assistanta. Formularz opcji i onboarding podają
tu dane (encje, wpisy integracji, atrybuty), a dostają kandydatów, gotowe opcje źródła
(`OPT_PRICE_*_ACTION`, kształt z `core/price_actions.py`) i werdykt „prognoza na N godzin".

Do planowania liczy się tylko źródło z prognozą: encja z samą bieżącą ceną nie jest
źródłem planu i nie zapisujemy jej jako takiego.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone, tzinfo
from typing import Any, Mapping, Sequence

from .price_actions import OPTION_KEYS, InvalidPriceSource, parse_source
from .price_integrations import OFFERED_PLATFORMS, SELL_WORDS, SIDE_TOKENS, FieldKind, FormField, Target, integration
from .prices import (currency_from_attributes, has_usable_prices_now, intervals_from_attributes,
                     starts_by_next_interval)

#: Ogólny słownik ról w nazwach encji; słowa konkretnych integracji dochodzą z rejestru.
_SELL_WORDS = ("export", "sell", "sprzed", "return", "teruglever", "inject", *SELL_WORDS)
#: Człony nazwy encji, które mówią tylko o stronie (kupno/sprzedaż) — odpadają przy szukaniu „pnia" pary.
_SIDE_TOKENS = frozenset({"export", "sell", "sprzedaz", "return", "buy", "import", "kupno", "teruglevering",
                          *SIDE_TOKENS})
_AREA = re.compile(r"[A-Za-z0-9_-]{1,16}")


@dataclass(frozen=True)
class EntityPair:
    platform: str
    buy: str
    sell: str | None = None
    #: Para zgadnięta z kilku możliwych — pokazujemy wybór, nie stosujemy jej po cichu.
    ambiguous: bool = False


def platform_currency(platform: str | None) -> str | None:
    """Waluta kraju integracji (rejestr), gdy seria jej nie podaje; None dla nieznanej albo wielokrajowej."""
    integ = integration(platform)
    return integ.currency if integ is not None else None


def is_excluded_entity(platform: str | None, entity_id: str) -> bool:
    """Encja integracji, która ma kształt cen prądu, ale ceną prądu domu nie jest (rejestr: `excluded_tokens`)."""
    integ = integration(platform)
    if integ is None or not integ.excluded_tokens:
        return False
    tokens = "_" + "_".join(re.split(r"[._]", entity_id.lower())) + "_"
    return any(f"_{token}_" in tokens for token in integ.excluded_tokens)


def enrich_attributes(platform: str | None, attrs: Mapping[str, Any] | None) -> dict[str, Any]:
    """Atrybuty encji z wiedzą o integracji: jednostka z symbolem jednoznacznym dla niej, bez `currency` → waluta.

    Ogólny adapter odmawia waluty dla symbolu wieloznacznego (np. `$`); integracja działająca w jednym
    kraju tej niejednoznaczności nie ma (rejestr: `symbol_currency`). Jawny atrybut `currency` zawsze
    wygrywa. Inne platformy zostają bez zmian.
    """
    out = dict(attrs) if isinstance(attrs, Mapping) else {}
    integ = integration(platform)
    unit = out.get("unit_of_measurement")
    if integ is None or (isinstance(out.get("currency"), str) and out["currency"].strip()) or not isinstance(unit, str):
        return out
    for symbol, currency in integ.symbol_currency:
        if unit.strip().startswith(symbol):
            out["currency"] = currency
            break
    return out


def effective_currency(platform: str | None, attrs: Mapping[str, Any] | None) -> str | None:
    """Waluta, którą seria niesie sama (atrybut, jednostka albo reguła integracji), bez fallbacku z opcji."""
    return currency_from_attributes(enrich_attributes(platform, attrs), None)


def is_sell_entity(entity_id: str) -> bool:
    low = entity_id.lower()
    return any(w in low for w in _SELL_WORDS)


def _stem(entity_id: str) -> str:
    tokens = re.split(r"[._]", entity_id.lower())
    return "_".join(t for t in tokens if t and t not in _SIDE_TOKENS)


def pair_entities(candidates: Sequence[Mapping[str, Any]]) -> list[EntityPair]:
    """Encje podpowiadanych integracji (JUŻ z użyteczną prognozą) → pary kupno/sprzedaż.

    Grupa = platforma + wpis konfiguracji. W grupie para powstaje, gdy nazwy różnią się tylko
    stroną (`…_buy_price` / `…_sell_price`, `…_import_…` / `…_export_…`), a gdy zostaje po jednym
    kupnie i sprzedaży — także wtedy (osobne liczniki importu i eksportu). Reszta jest
    niejednoznaczna: wszystkie kombinacje z `ambiguous=True`. Sprzedaż bez kupna nie jest
    podpowiadana; encje wykluczone w rejestrze (`excluded_tokens`) pomijamy.
    """
    groups: dict[tuple[str, Any], tuple[list[str], list[str]]] = {}
    for cand in candidates:
        if not isinstance(cand, Mapping):
            continue
        platform, eid = cand.get("platform"), cand.get("entity_id")
        if platform not in OFFERED_PLATFORMS or not isinstance(eid, str) or not eid \
                or is_excluded_entity(platform, eid):
            continue
        buys, sells = groups.setdefault((platform, cand.get("config_entry_id")), ([], []))
        (sells if is_sell_entity(eid) else buys).append(eid)
    out: list[EntityPair] = []
    for (platform, _entry), (buys, sells) in sorted(groups.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
        buys, sells = sorted(buys), sorted(sells)
        by_stem: dict[str, list[str]] = {}
        for sell in sells:
            by_stem.setdefault(_stem(sell), []).append(sell)
        rest_buys, used = [], set()
        for buy in buys:
            match = by_stem.get(_stem(buy), [])
            if len(match) == 1 and match[0] not in used:
                used.add(match[0])
                out.append(EntityPair(platform, buy, match[0]))
            else:
                rest_buys.append(buy)
        left = [x for x in sells if x not in used]
        if not left:
            out.extend(EntityPair(platform, b) for b in rest_buys)
        elif len(rest_buys) == 1 and len(left) == 1:
            out.append(EntityPair(platform, rest_buys[0], left[0]))
        else:
            out.extend(EntityPair(platform, b, s, True) for b in rest_buys for s in left)
    return out


@dataclass(frozen=True)
class ActionCandidate:
    """Źródło z akcji HA: strona kupna i (opcjonalnie) sprzedaży, bez danych od użytkownika."""
    domain: str
    entry_id: str
    title: str
    buy: dict
    sell: dict | None
    #: Pola, o które formularz musi zapytać (kolejność = kolejność w formularzu).
    form: tuple[FormField, ...]

    @property
    def fields(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.form)


def action_candidates(entries: Mapping[str, Sequence[tuple[str, str]]]) -> list[ActionCandidate]:
    """Wpisy integracji z akcją (domena → [(entry_id, tytuł)]) → kandydaci na źródło z akcji (szablony z rejestru)."""
    out: list[ActionCandidate] = []
    for domain in sorted(entries):
        integ = integration(domain)
        if integ is None or integ.buy is None:
            continue
        for entry_id, title in entries[domain]:
            def side(template) -> dict:
                data = {template.entry_key: entry_id} if template.entry_key is not None else {}
                data.update(template.data)
                return {"action": template.action, "data": data}
            out.append(ActionCandidate(domain, entry_id, title, side(integ.buy),
                                       side(integ.sell) if integ.sell is not None else None, integ.form))
    return out


def _form_value(form_field: FormField, values: Mapping[str, Any]) -> Any:
    """Odpowiedź z formularza znormalizowana według rodzaju pola; None = pola nie zapisujemy.

    Brak pola wymaganego → `InvalidPriceSource`. Wartości nie do użycia (zła waluta, skala) odrzuca
    dopiero `parse_source` — to samo sito co w czasie działania.
    """
    name, kind = form_field.name, form_field.kind
    if kind is FieldKind.FLAG:
        if name not in values:
            if form_field.required:
                raise InvalidPriceSource("missing data")
            return None
        return bool(values[name])
    value = values.get(name)
    if kind is FieldKind.AREA:
        if not isinstance(value, str) or not _AREA.fullmatch(value.strip()):
            raise InvalidPriceSource("one area required")       # kilka stref po przecinku to nie jedna strefa
        return value.strip()
    if not value:
        if form_field.required:
            raise InvalidPriceSource("missing data")
        return None
    if isinstance(value, str) and kind is FieldKind.CURRENCY:
        return value.strip().upper()
    if isinstance(value, str) and kind is FieldKind.UNIT:
        return value.strip()
    return value


def build_action_option(candidate: ActionCandidate, values: Mapping[str, Any]) -> tuple[dict, dict | None]:
    """Kandydat + odpowiedzi z formularza → (opcja kupna, opcja sprzedaży albo None).

    Każda strona bierze tylko pola, które przyjmuje (rejestr: `SourceSide.fields`). Każda opcja przechodzi
    `parse_source` (to samo sito co w czasie działania); `InvalidPriceSource`, gdy czegoś brakuje albo
    jest nie do użycia (brak skali, kilka stref…).
    """
    integ = integration(candidate.domain)
    if integ is None or integ.buy is None:
        raise InvalidPriceSource("unsupported action")
    form = {f.name: f for f in candidate.form}

    def side(template: dict, takes: tuple[str, ...]) -> dict:
        data = dict(template["data"])
        extra: dict[str, Any] = {}
        for name in takes:
            value = _form_value(form[name], values)
            if value is not None:
                (data if form[name].target is Target.DATA else extra)[form[name].key] = value
        # Klucze opcji w stałej kolejności formatu — zapis jest taki sam bez względu na kolejność pól formularza.
        option: dict[str, Any] = {"action": template["action"], "data": data,
                                  **{k: extra[k] for k in OPTION_KEYS if k in extra}}
        parsed = parse_source(option)           # kontrola kształtu; wyjątek idzie do wołającego
        option["data"] = dict(parsed.data)      # postać znormalizowana (np. strefa wielkimi literami)
        return option

    buy_option = side(candidate.buy, integ.buy.fields)
    sell_option = side(candidate.sell, integ.sell.fields) if candidate.sell is not None and integ.sell is not None \
        else None
    return buy_option, sell_option


def currency_conflict(source_currency: str | None, option_currency: str | None) -> bool:
    """Waluta źródła i `price_currency` są obie ustawione i się różnią.

    Źródło wygrywa w czasie działania, ale cicho wybrana waluta bywa pomyłką, więc formularz
    pokazuje niejednoznaczność zamiast ją rozstrzygać.
    """
    if not source_currency or not option_currency:
        return False
    return source_currency.strip().upper() != option_currency.strip().upper()


@dataclass(frozen=True)
class SeriesCheck:
    #: ok (pełna prognoza teraz), short (prognoza krótsza niż wymagane 6 h), current_only (sama
    #: bieżąca cena), unusable (brak waluty, zła seria albo encja niedostępna).
    status: str
    hours: int = 0
    #: Seria obejmuje CAŁĄ jutrzejszą dobę lokalną (np. ceny na jutro już opublikowane).
    tomorrow: bool = False


def check_series(attrs: Mapping[str, Any] | None, fallback_currency: str | None, tz: tzinfo,
                 now: datetime, *, state: str | None = None) -> SeriesCheck:
    """Werdykt o źródle ceny: na ile godzin w przód jest prognoza i czy nadaje się do planowania."""
    attrs = attrs if isinstance(attrs, Mapping) else {}
    currency = currency_from_attributes(attrs, fallback_currency)
    intervals = intervals_from_attributes(attrs, None, currency, tz, now) if currency else []
    if not intervals:
        # „Sama bieżąca cena": encja wygląda na cenę (jest waluta i jednostka), ma liczbę w stanie, a żadnej serii.
        if currency and state is not None and _numeric(state) and not _has_series_attribute(attrs):
            return SeriesCheck("current_only")
        return SeriesCheck("unusable")
    tomorrow = _covers_tomorrow(intervals, tz, now)
    if has_usable_prices_now(attrs, fallback_currency, tz, now):
        return SeriesCheck("ok", _hours_ahead(intervals, now), tomorrow)
    if not starts_by_next_interval(intervals, now):
        return SeriesCheck("unusable")      # seria zaczyna się dalej niż jeden przedział po teraz — nie obejmuje teraz
    hours = _hours_ahead(intervals, now)
    return SeriesCheck("short", hours, tomorrow) if hours > 0 else SeriesCheck("unusable")


def sell_lacks_tomorrow(buy: SeriesCheck, sell: SeriesCheck) -> bool:
    """Kupno ma już całe jutro, a sprzedaż (z serią) jeszcze nie — informacja przy wyborze, nie błąd.

    W czasie działania blok cen idzie tylko w zakresie pokrytym przez obie strony, więc jutro planer
    liczy wtedy z cen poprzedniej doby. Przed publikacją cen na jutro żadna strona go nie ma — wtedy nic.
    """
    return buy.tomorrow and sell.status in ("ok", "short") and not sell.tomorrow


def _end(intervals: Sequence[Mapping[str, Any]]) -> datetime:
    last = intervals[-1]
    return datetime.fromisoformat(last["startAt"].replace("Z", "+00:00")) + timedelta(minutes=last["minutes"])


def _hours_ahead(intervals: Sequence[Mapping[str, Any]], now: datetime) -> int:
    return int((_end(intervals) - now.astimezone(timezone.utc)).total_seconds() // 3600)


def _covers_tomorrow(intervals: Sequence[Mapping[str, Any]], tz: tzinfo, now: datetime) -> bool:
    """Seria (ciągła) sięga końca jutrzejszej doby lokalnej — północy pojutrze w strefie `tz`."""
    pojutrze = now.astimezone(tz).date() + timedelta(days=2)
    return _end(intervals) >= datetime.combine(pojutrze, time(), tzinfo=tz)


def _numeric(state: str) -> bool:
    try:
        float(str(state).replace(",", "."))
    except ValueError:
        return False
    return True


def _has_series_attribute(attrs: Mapping[str, Any]) -> bool:
    return any(isinstance(v, (list, tuple, dict)) for v in attrs.values())
