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
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Mapping, Sequence

from .price_actions import InvalidPriceSource, parse_source
from .prices import (currency_from_attributes, has_usable_prices_now, intervals_from_attributes,
                     starts_by_next_interval)

#: Integracje cenowe, które podpowiadamy (platformy encji / domeny akcji).
KNOWN_PLATFORMS = frozenset({"amberelectric", "octopus_energy", "pstryk", "entsoe", "nordpool", "tibber",
                             "energyzero", "easyenergy"})
_SELL_WORDS = ("export", "feed_in", "feedin", "sell", "sprzed", "return", "teruglever", "inject")
#: Człony nazwy encji, które mówią tylko o stronie (kupno/sprzedaż) — odpadają przy szukaniu „pnia" pary.
_SIDE_TOKENS = frozenset({"export", "feed", "in", "feedin", "sell", "sprzedaz", "return", "buy", "import", "general",
                          "kupno", "teruglevering"})
_AREA = re.compile(r"[A-Za-z0-9_-]{1,16}")
#: Waluta kraju integracji — gdy seria jej nie podaje. Każda z tych integracji działa w jednym kraju
#: (Amber: Australia, Octopus: Wielka Brytania, Pstryk: Polska). Amber ma jeszcze regułę
#: `enrich_attributes` dla jednostki `$`, której ogólny adapter nie rozstrzyga.
PLATFORM_CURRENCY = {"amberelectric": "AUD", "octopus_energy": "GBP", "pstryk": "PLN"}
#: Domeny wpisów integracji, z których akcji umiemy czytać ceny.
ACTION_DOMAINS = ("amberelectric", "tibber", "nordpool", "entsoe", "energyzero", "easyenergy")


@dataclass(frozen=True)
class EntityPair:
    platform: str
    buy: str
    sell: str | None = None
    #: Para zgadnięta z kilku możliwych — pokazujemy wybór, nie stosujemy jej po cichu.
    ambiguous: bool = False


def is_gas_entity(platform: str | None, entity_id: str) -> bool:
    """Encja gazowa Octopusa (`…_gas_…`) ma kształt cen prądu, ale prądem nie jest."""
    return platform == "octopus_energy" and "gas" in re.split(r"[._]", entity_id.lower())


def enrich_attributes(platform: str | None, attrs: Mapping[str, Any] | None) -> dict[str, Any]:
    """Atrybuty encji z wiedzą o integracji: Amber (tylko Australia) z jednostką `$` i bez `currency` → AUD.

    Ogólny adapter odmawia waluty dla niejednoznacznego `$`; dla Ambera nie ma tu niejednoznaczności.
    Jawny atrybut `currency` zawsze wygrywa. Inne platformy zostają bez zmian.
    """
    out = dict(attrs) if isinstance(attrs, Mapping) else {}
    unit = out.get("unit_of_measurement")
    if (platform == "amberelectric" and not (isinstance(out.get("currency"), str) and out["currency"].strip())
            and isinstance(unit, str) and unit.strip().startswith("$")):
        out["currency"] = PLATFORM_CURRENCY["amberelectric"]
    return out


def effective_currency(platform: str | None, attrs: Mapping[str, Any] | None) -> str | None:
    """Waluta, którą seria niesie sama (atrybut, jednostka albo reguła integracji), bez fallbacku z opcji."""
    return derived_currency(enrich_attributes(platform, attrs))


def is_sell_entity(entity_id: str) -> bool:
    low = entity_id.lower()
    return any(w in low for w in _SELL_WORDS)


def _stem(entity_id: str) -> str:
    tokens = re.split(r"[._]", entity_id.lower())
    return "_".join(t for t in tokens if t and t not in _SIDE_TOKENS)


def pair_entities(candidates: Sequence[Mapping[str, Any]]) -> list[EntityPair]:
    """Encje znanych integracji (JUŻ z użyteczną prognozą) → pary kupno/sprzedaż.

    Grupa = platforma + wpis konfiguracji. W grupie para powstaje, gdy nazwy różnią się tylko
    stroną (`…_buy_price` / `…_sell_price`, `…general_forecast` / `…feed_in_forecast`), a gdy
    zostaje po jednym kupnie i sprzedaży — także wtedy (Octopus ma osobne liczniki importu
    i eksportu). Reszta jest niejednoznaczna: wszystkie kombinacje z `ambiguous=True`.
    Sprzedaż bez kupna nie jest podpowiadana; kanał `controlled_load` (Amber) pomijamy.
    """
    groups: dict[tuple[str, Any], tuple[list[str], list[str]]] = {}
    for cand in candidates:
        if not isinstance(cand, Mapping):
            continue
        platform, eid = cand.get("platform"), cand.get("entity_id")
        if platform not in KNOWN_PLATFORMS or not isinstance(eid, str) or not eid or "controlled_load" in eid \
                or is_gas_entity(platform, eid):
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
    fields: tuple[str, ...]


def action_candidates(entries: Mapping[str, Sequence[tuple[str, str]]]) -> list[ActionCandidate]:
    """Wpisy znanych integracji (domena → [(entry_id, tytuł)]) → kandydaci na źródło z akcji."""
    out: list[ActionCandidate] = []
    for domain in sorted(entries):
        for entry_id, title in entries[domain]:
            if domain == "amberelectric":
                def amber(channel: str) -> dict:
                    return {"action": "amberelectric.get_forecasts",
                            "data": {"config_entry_id": entry_id, "channel_type": channel}}
                cand = ActionCandidate(domain, entry_id, title, amber("general"), amber("feed_in"), ())
            elif domain == "tibber":
                cand = ActionCandidate(domain, entry_id, title, {"action": "tibber.get_prices", "data": {}},
                                       None, ("home", "currency"))
            elif domain == "nordpool":
                cand = ActionCandidate(domain, entry_id, title,
                                       {"action": "nordpool.get_prices_for_date", "data": {"config_entry": entry_id}},
                                       None, ("area", "currency"))
            elif domain == "entsoe":
                cand = ActionCandidate(domain, entry_id, title,
                                       {"action": "entsoe.get_energy_prices", "data": {"config_entry": entry_id}},
                                       None, ("unit", "currency"))
            elif domain == "energyzero":
                cand = ActionCandidate(domain, entry_id, title,
                                       {"action": "energyzero.get_energy_prices", "data": {"config_entry": entry_id}},
                                       None, ("incl_vat",))
            elif domain == "easyenergy":
                cand = ActionCandidate(
                    domain, entry_id, title,
                    {"action": "easyenergy.get_energy_usage_prices", "data": {"config_entry": entry_id}},
                    {"action": "easyenergy.get_energy_return_prices", "data": {"config_entry": entry_id}},
                    ("incl_vat",))
            else:
                continue
            out.append(cand)
    return out


def build_action_option(candidate: ActionCandidate, values: Mapping[str, Any]) -> tuple[dict, dict | None]:
    """Kandydat + odpowiedzi z formularza → (opcja kupna, opcja sprzedaży albo None).

    Każda opcja przechodzi `parse_source` (to samo sito co w czasie działania); `InvalidPriceSource`,
    gdy czegoś brakuje albo jest nie do użycia (ENTSO-e bez jednostki, Nord Pool z kilkoma strefami…).
    """
    def side(template: dict, *, buy: bool) -> dict:
        data = dict(template["data"])
        option: dict[str, Any] = {"action": template["action"], "data": data}
        domain = candidate.domain
        if domain == "nordpool":
            area = values.get("area")
            if not isinstance(area, str) or not _AREA.fullmatch(area.strip()):
                raise InvalidPriceSource("one area required")       # kilka stref po przecinku to nie jedna strefa
            data["areas"] = area.strip()
            data["currency"] = values.get("currency")
        elif domain == "entsoe":
            option["unit"] = values.get("unit")
            option["currency"] = values.get("currency")
        elif domain in ("energyzero", "easyenergy") and buy:
            if "incl_vat" not in values:
                raise InvalidPriceSource("missing data")
            data["incl_vat"] = bool(values["incl_vat"])
        elif domain == "tibber":
            if values.get("currency"):
                option["currency"] = values["currency"]
            if values.get("home"):
                option["home"] = values["home"]
        for key in ("currency",):
            if isinstance(option.get(key), str):
                option[key] = option[key].strip().upper()
        if isinstance(option.get("unit"), str):
            option["unit"] = option["unit"].strip()
        parsed = parse_source(option)           # kontrola kształtu; wyjątek idzie do wołającego
        option["data"] = dict(parsed.data)      # postać znormalizowana (Nord Pool: strefa wielkimi, lista)
        return option

    buy_option = side(candidate.buy, buy=True)
    sell_option = side(candidate.sell, buy=False) if candidate.sell is not None else None
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
    if has_usable_prices_now(attrs, fallback_currency, tz, now):
        return SeriesCheck("ok", _hours_ahead(intervals, now))
    if not starts_by_next_interval(intervals, now):
        return SeriesCheck("unusable")      # seria zaczyna się dalej w przyszłości (np. „jutro") — nie obejmuje teraz
    hours = _hours_ahead(intervals, now)
    return SeriesCheck("short" if hours > 0 else "unusable", max(hours, 0))


def _hours_ahead(intervals: Sequence[Mapping[str, Any]], now: datetime) -> int:
    last = intervals[-1]
    end = datetime.fromisoformat(last["startAt"].replace("Z", "+00:00")) + timedelta(minutes=last["minutes"])
    return int((end - now.astimezone(timezone.utc)).total_seconds() // 3600)


def _numeric(state: str) -> bool:
    try:
        float(str(state).replace(",", "."))
    except ValueError:
        return False
    return True


def _has_series_attribute(attrs: Mapping[str, Any]) -> bool:
    return any(isinstance(v, (list, tuple, dict)) for v in attrs.values())


def derived_currency(attrs: Mapping[str, Any] | None) -> str | None:
    """Waluta, którą encja podaje SAMA (atrybut `currency` albo jednostka `SEK/kWh`), bez żadnego fallbacku."""
    return currency_from_attributes(attrs if isinstance(attrs, Mapping) else {}, None)
