"""Rejestr integracji cenowych: każdy wpis jest poprawny, a szablony akcji przechodzą `parse_source`."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from custom_components.volcast.core import price_integrations as reg
from custom_components.volcast.core.price_actions import OPTION_KEYS, parse_source
from custom_components.volcast.core.price_integrations import (
    INTEGRATIONS, Dates, EntityUse, FieldKind, ResponseShape, Target)
from custom_components.volcast.core.price_setup import (
    action_candidates, build_action_option, check_series, effective_currency, enrich_attributes, pair_entities)

_CODE = re.compile(r"^[A-Z]{3}$")
_IDS = [i.domain for i in INTEGRATIONS]
#: Przykładowa poprawna wartość pola formularza danego rodzaju.
_SAMPLE = {FieldKind.TEXT: "Dom", FieldKind.AREA: "se3", FieldKind.CURRENCY: "eur", FieldKind.UNIT: "MWh",
           FieldKind.FLAG: True}


def test_domains_and_action_names_are_unique():
    assert len(_IDS) == len(set(_IDS))
    names = [a.name for i in INTEGRATIONS for a in i.actions]
    assert len(names) == len(set(names))


@pytest.mark.parametrize("integ", INTEGRATIONS, ids=_IDS)
def test_entry_is_well_formed(integ):
    assert re.fullmatch(r"[a-z][a-z0-9_]*", integ.domain)
    assert integ.entity_domains and all(re.fullmatch(r"[a-z_]+", d) for d in integ.entity_domains)
    assert integ.currency is None or _CODE.match(integ.currency)
    assert all(sym and _CODE.match(cur) for sym, cur in integ.symbol_currency)
    assert all(t and t == t.lower() for t in (*integ.excluded_tokens, *integ.sell_words, *integ.side_tokens))
    assert all(v.path and v.divisor > 0 for v in integ.scaled_values)
    for action in integ.actions:
        assert action.name.startswith(f"{integ.domain}.") and action.name.count(".") == 1
        assert action.currency is None or _CODE.match(action.currency)
        assert action.unit in (None, "kWh", "MWh")
        assert action.refresh_s is None or action.refresh_s > 0
        if action.response is ResponseShape.BY_AREA:
            assert action.area_key in action.required and action.response_key is None
        else:
            assert action.response_key and action.area_key is None
        assert action.currency_key is None or action.currency_key in action.required
        if action.dates is Dates.ROLLING:
            assert action.response is ResponseShape.KEY
    names = [f.name for f in integ.form]
    assert len(names) == len(set(names))
    sides = [s for s in (integ.buy, integ.sell) if s is not None]
    assert integ.sell is None or integ.buy is not None             # sprzedaż bez kupna nie jest źródłem
    assert {s.action for s in sides} <= {a.name for a in integ.actions}
    used = {n for s in sides for n in s.fields}
    assert used <= set(names) and used == set(names)               # każde pole formularza ktoś przyjmuje
    for f in integ.form:
        assert f.target is not Target.OPTION or f.key in OPTION_KEYS
        assert f.kind is not FieldKind.FLAG or f.target is Target.DATA


@pytest.mark.parametrize("integ", [i for i in INTEGRATIONS if i.buy is not None], ids=lambda i: i.domain)
def test_action_templates_validate(integ):
    """Szablon + przykładowe odpowiedzi formularza → opcje, które przyjmuje runtime."""
    (cand,) = action_candidates({integ.domain: [("e1", "X")]})
    buy, sell = build_action_option(cand, {f.name: _SAMPLE[f.kind] for f in integ.form})
    assert (sell is None) == (integ.sell is None)
    for option, side in ((buy, integ.buy), (sell, integ.sell)):
        if side is None:
            continue
        src = parse_source(option)
        assert src.action == side.action
        if side.entry_key is not None:
            assert src.data[side.entry_key] == "e1"
    # Tylko pola wymagane też wystarczą (pola opcjonalne są naprawdę opcjonalne).
    build_action_option(cand, {f.name: _SAMPLE[f.kind] for f in integ.form if f.required})


def test_action_needing_unit_asks_for_unit_and_currency():
    for integ in INTEGRATIONS:
        for action in integ.actions:
            if action.needs_unit:
                kinds = {f.kind for f in integ.form if f.required and f.target is Target.OPTION}
                assert {FieldKind.UNIT, FieldKind.CURRENCY} <= kinds


def test_derived_sets():
    assert reg.OFFERED_PLATFORMS <= reg.REPORTED_PLATFORMS
    assert set(reg.ACTION_DOMAINS) <= reg.OFFERED_PLATFORMS
    assert set(reg.ACTIONS) == {a.name for i in INTEGRATIONS for a in i.actions}
    assert reg.ENTITY_DOMAINS == ("sensor", "event")
    assert all(reg.integration(d).entities is not EntityUse.NONE for d in reg.REPORTED_PLATFORMS)
    assert reg.integration("nope") is None and reg.integration(None) is None


# ── Granica: wiedza o integracjach tylko w rejestrze ─────────────────────────

_PACKAGE = Path(__file__).resolve().parents[2] / "custom_components" / "volcast"
_REGISTRY = _PACKAGE / "core" / "price_integrations.py"
#: Nazwy integracji, które w tekście nie wyglądają jak ich domena.
_NAMES = (r"\bamber", r"nord\s*pool", r"entso", r"octopus", r"zonneplan", r"\bfrank\b", r"\bepex\b", r"pstryk",
          r"tibber", r"energy\s*zero", r"easy\s*energy", r"controlled_load")


def test_integration_names_live_only_in_the_registry():
    patterns = [re.compile(rf"(?<![a-z0-9_]){re.escape(i.domain)}(?![a-z0-9_])", re.I) for i in INTEGRATIONS]
    patterns += [re.compile(n, re.I) for n in _NAMES]
    hits = []
    for path in sorted(_PACKAGE.rglob("*.py")):
        if path == _REGISTRY:
            continue
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if any(p.search(line) for p in patterns):
                hits.append(f"{path.relative_to(_PACKAGE)}:{no}: {line.strip()}")
    assert hits == []


def test_boundary_check_sees_a_planted_name(tmp_path):
    """Kontrola wyżej naprawdę łapie domenę (żeby pusty wynik nie był przypadkiem złego wzorca)."""
    pattern = re.compile(rf"(?<![a-z0-9_]){re.escape(INTEGRATIONS[0].domain)}(?![a-z0-9_])", re.I)
    assert pattern.search(f'if platform == "{INTEGRATIONS[0].domain}":')
    assert not pattern.search(f"x_{INTEGRATIONS[0].domain}_y")


# ── Platformy podpowiadane w konfiguracji = te, których kształt potwierdziliśmy ──

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "prices"
_WAW = ZoneInfo("Europe/Warsaw")


def _frank_series(now: datetime, hours: int) -> dict:
    """Fixture integracji (`prices` z `from` / `till` / `price`) przesunięty na „teraz”."""
    attrs = json.loads((_FIXTURES / "frank_energie_prices.json").read_text())["attributes"]
    start = now.replace(minute=0, second=0, microsecond=0)
    rows = [{"from": (start + timedelta(hours=i)).isoformat(), "till": (start + timedelta(hours=i + 1)).isoformat(),
             "price": 0.2} for i in range(hours)]
    return {**attrs, "prices": rows}


def test_frank_energie_shape_parses_but_is_not_offered():
    # Kształt jest potwierdzony u źródła (`prices` z `from` / `till` / `price`, €/kWh), więc wybrana ręcznie encja
    # działa. Podpowiedzi nie ma: integracja wystawia ten sam kształt i jednostkę także dla serii, które nie są ceną
    # prądu domu — sama cena rynkowa, sam VAT, a przy umowie belgijskiej gaz w €/kWh.
    now = datetime(2026, 10, 31, 9, 20, tzinfo=timezone.utc)
    assert "frank_energie" in reg.REPORTED_PLATFORMS and "frank_energie" not in reg.OFFERED_PLATFORMS
    attrs = _frank_series(now, 24)
    assert effective_currency("frank_energie", attrs) == "EUR"           # z jednostki €/kWh
    for series in (attrs, {**attrs, "prices": [{**row, "price": 0.04} for row in attrs["prices"]]}):
        check = check_series(enrich_attributes("frank_energie", series), None, _WAW, now)
        assert (check.status, check.hours) == ("ok", 23)                 # VAT / gaz: nie do odróżnienia kształtem
    out = pair_entities([{"entity_id": eid, "platform": "frank_energie", "config_entry_id": "f1"} for eid in (
        "sensor.frank_energie_prices_current_electricity_price_all_in",
        "sensor.frank_energie_prices_current_electricity_market_price",
        "sensor.frank_energie_prices_current_electricity_vat_price",
        "sensor.frank_energie_gasprices_huidige_gasprijs_all_in")])
    assert out == []


@pytest.mark.parametrize("platform", ["epex_spot", "pstryk_aio"])
def test_unverified_shapes_are_reported_but_not_offered(platform):
    assert platform in reg.REPORTED_PLATFORMS and platform not in reg.OFFERED_PLATFORMS
    assert pair_entities([{"entity_id": f"sensor.{platform}_price", "platform": platform}]) == []


def test_discovery_and_setup_read_the_same_table():
    from custom_components.volcast.core.discovery.known import PRICE_PLATFORMS
    assert PRICE_PLATFORMS is reg.REPORTED_PLATFORMS
