"""Zgłoszenie naprawy „ceny wstrzymane przez stronę sprzedaży” w PRAWDZIWYM rejestrze zgłoszeń HA.

Atrapa rejestru w testach jednostkowych nie złapie zmiany sygnatury `ir.async_create_issue` — szeroki
`except` w nadawcy połknąłby błąd i zgłoszenie po cichu nigdy by się nie pojawiło.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.volcast.const import DOMAIN
from custom_components.volcast.control.telemetry import TelemetrySender

NOW = datetime(2026, 9, 23, 10, 0, 30, tzinfo=timezone.utc)
_OPTIONS = {"entity_price_buy": "sensor.buy", "entity_price_sell": "sensor.sell"}


class _Cloud:
    async def async_post_telemetry(self, reading):
        return SimpleNamespace(status=200, signals=None)


class _Exec:
    local_switch = False

    def exec_summary(self):
        return {}


def _series(currency: str) -> dict:
    start = NOW.replace(minute=0, second=0) - timedelta(hours=1)
    return {"currency": currency, "raw_today": [
        {"start": (start + timedelta(hours=i)).isoformat(), "end": (start + timedelta(hours=i + 1)).isoformat(),
         "value": 0.5} for i in range(24)]}


def _sender(hass: HomeAssistant) -> TelemetrySender:
    hass.states.async_set("sensor.buy", "0.5", _series("PLN"))
    entry = SimpleNamespace(entry_id="e1", options=dict(_OPTIONS))
    return TelemetrySender(hass, entry, _Cloud(), _Exec(), choice=None, profile_map={}, manual_map={},
                           grid_negate=False, limits=None, utcnow=lambda: NOW)


async def _flush(sender: TelemetrySender, times: int) -> None:
    for _ in range(times):
        await sender.async_flush()


def _issue(hass: HomeAssistant, key: str):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{key}_e1")


async def test_sell_without_data_raises_a_real_issue_and_recovery_deletes_it(hass: HomeAssistant):
    sender = _sender(hass)                               # encji sprzedaży nie ma
    await _flush(sender, 1)
    assert _issue(hass, "prices_sell_no_data") is None   # pierwszy cykl to karencja
    await _flush(sender, 1)
    issue = _issue(hass, "prices_sell_no_data")
    assert issue is not None and issue.translation_key == "prices_sell_no_data"
    assert issue.translation_placeholders == {"source": "sensor.sell"} and issue.is_fixable is False
    assert issue.severity == ir.IssueSeverity.WARNING
    hass.states.async_set("sensor.sell", "0.4", _series("PLN"))
    await _flush(sender, 1)
    assert _issue(hass, "prices_sell_no_data") is None


async def test_sell_without_pair_raises_a_real_issue_and_stop_deletes_it(hass: HomeAssistant):
    sender = _sender(hass)
    hass.states.async_set("sensor.sell", "0.4", _series("EUR"))     # inna waluta niż kupno
    await _flush(sender, 2)
    issue = _issue(hass, "prices_sell_no_pair")
    assert issue is not None and issue.translation_placeholders == {"source": "sensor.sell"}
    assert _issue(hass, "prices_sell_no_data") is None
    await sender.async_stop()
    assert _issue(hass, "prices_sell_no_pair") is None
