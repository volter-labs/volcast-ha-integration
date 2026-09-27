"""Diagnostyka wpisu Volcast (pobierana przez użytkownika z karty integracji).

`entry.data` (klucz API, tokeny parowania, dane konta) NIGDY nie trafia tu w
całości — kopiujemy z niego wyłącznie pojedyncze, nieszkodliwe pola (czy wpis
jest sparowany, hostname backendu). `entry.options` trafia tu jedynie jako
lista NAZW kluczy (`options_keys`), nigdy wartości. Sekcja `control`, gdy
sterowanie jest złożone, przechodzi przez to samo maskowanie seriali/MAC-ów/
e-maili co raport wykrywania (`core/discovery/report.py`).

Tryb bezpośredni (`control.direct`): profil, transport, stan łącza, odmowa, tożsamość (tylko stan),
liczniki transportu, monitor kolizji, ostatnia sonda (`ProbeReport.to_dict()` — bez adresu i
odcisku), klucze bez odczytu zwrotnego, budżet NVM (ramki na klucz w oknie, trafienia, wyłączenia
harmonogramu w stronę bezpieczną) i ostatnia decyzja. Nigdy adres, port, numer loggera, numer
seryjny, odcisk urządzenia ani celu, ani sól. Ramki (`frames`) tylko w połączeniu próbnym —
rejestry seryjne wyzerowane z przeliczonymi sumami, numer loggera wyzerowany.
"""
from __future__ import annotations

import dataclasses

from typing import Any
from urllib.parse import urlparse

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import CONF_BACKEND, CONF_MODE, DOMAIN
from .core.discovery.report import _known_mac_pattern, _mac_hex, _mask_value, _serial_pattern, _valid_serial
from .core.control.tou_cycle import SAFETY_OFF_CAP
from .core.modbus.frame_redact import redact_frames
from .registry_compat import all_devices

# Wpis bez `mode` (sprzed trybu „tylko rozpoznanie") to klasyczny wpis prognozy
# z kluczem API.
_MODE_FORECAST = "forecast"


def _serials(hass: HomeAssistant) -> set[str]:
    """Numery seryjne znane rejestrowi urządzeń — do maskowania sekcji `control`.

    Identyfikatory rejestru filtrujemy `_valid_serial` tak samo jak raport
    wykrywania — bez tego słowny identyfikator integracji (np. `"supervisor"`)
    maskowałby zwykłe słowo w `entity_id` zamiast prawdziwego numeru seryjnego.
    """
    out: set[str] = set()
    for dev in all_devices(dr.async_get(hass)):
        if getattr(dev, "serial_number", None):
            out.add(str(dev.serial_number))
        for ident in getattr(dev, "identifiers", ()) or ():
            out.update(str(p) for p in list(ident)[1:] if len(str(p)) >= 8 and _valid_serial(str(p)))
    return out


def _macs(hass: HomeAssistant) -> set[str]:
    """MAC-i znane rejestrowi urządzeń (`device.connections`) — do maskowania `control`.

    Ten sam wzorzec, którego używa raport wykrywania dla ZNANYCH MAC-ów
    (`_known_mac_pattern`/`_mac_alt`) — w przeciwieństwie do ogólnego `_MAC_RE`
    (tylko separator `:`/`-`) łapie MAC-a wsunięty w `entity_id` z podkreśleniem
    (`select.shelly_aa_bb_cc_dd_ee_ff_mode`) albo bez separatora wcale.
    """
    out: set[str] = set()
    for dev in all_devices(dr.async_get(hass)):
        for conn_type, value in getattr(dev, "connections", ()) or ():
            if conn_type == "mac":
                hexed = _mac_hex(str(value))
                if hexed:
                    out.add(hexed)
    return out


def _control(hass: HomeAssistant, rt: Any) -> dict | None:
    """Sekcja sterowania: profil, mapowanie encji (zamaskowane), stan wykonawcy.

    `foreign_changes` trzyma `entity_id` — to jest pobrana lokalnie diagnostyka,
    nie telemetria (patrz `control/executor.py` — logi i telemetria widzą tylko
    nazwę parametru), ale numer seryjny albo MAC w środku `entity_id` i tak
    przechodzi przez maskowanie niżej.
    """
    if rt is None:
        return None
    ex = rt.executor
    raw = {"profile": getattr(getattr(rt.choice, "profile", None), "id", None),
           "integration_domain": getattr(rt.choice, "integration_domain", None),
           "mapped": dict(rt.mapped or {}), "exec": ex.exec_summary(),
           "tou_preview": ex.tou_preview, "foreign_changes": list(ex.foreign_changes)}
    conn = getattr(rt, "direct", None)
    if conn is not None:
        raw["direct"] = _direct(rt, conn)
    return _mask_value(raw, _serial_pattern(_serials(hass)), _known_mac_pattern(_macs(hass)))


def _direct(rt: Any, conn: Any) -> dict:
    ex = rt.executor
    memory = getattr(ex, "_memory", None)
    budget = getattr(memory, "budget", None)
    now_wall = ex._utcnow().timestamp() if hasattr(ex, "_utcnow") else None
    keys = budget.counts(now_wall) if budget is not None and now_wall is not None else {}
    if conn.trial:
        status = "trial"
    elif conn.conflict:
        status = "conflict"
    else:
        status = "ok" if conn.reading is not None else "link_down"
    probe = next((r for r in getattr(rt, "last_probe", None) or [] if r.identity is not None), None)
    decision = getattr(ex, "last_decision", None)
    out = {
        "profile": conn.profile.id, "transport": conn.target.get("transport"), "status": status,
        "refused": conn.refused(), "identity": conn.identity,
        "stats": dataclasses.asdict(conn.stats),
        "monitor": {"state": conn.monitor.state, "reason": conn.monitor.reason},
        "static_conflicts": list(conn.static_conflicts),
        "probe": probe.to_dict() if probe is not None else None,
        "echo_only": sorted(conn.unreadable),
        "nvm": {"keys": keys, "total": sum(keys.values()), "hit": bool(getattr(budget, "hit", False)),
                "safety_offs": len(getattr(memory, "tou_safety_offs", ()) or ()),
                "safety_off_capped": len(getattr(memory, "tou_safety_offs", ()) or ()) >= SAFETY_OFF_CAP,
                "restore_ineffective": bool(getattr(memory, "budget_restore_ineffective", False))},
        "last_decision": decision.summary() if decision is not None else None,
    }
    client = conn.client
    if conn.trial and client is not None and getattr(client, "record", False):
        out["frames"] = redact_frames(str(conn.target.get("transport")), client.recorded(), conn.profile)
    return out


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    entry_data = (getattr(hass, "data", None) or {}).get(DOMAIN, {}).get(entry.entry_id) or {}
    runner = entry_data.get("discovery")
    mode = entry.data.get(CONF_MODE) if hasattr(entry.data, "get") else None
    backend = entry.data.get(CONF_BACKEND) if hasattr(entry.data, "get") else None
    try:
        backend_host = urlparse(backend.get("base_url", "")).hostname if isinstance(backend, dict) else None
    except Exception:  # noqa: BLE001 — malformed backend must not fail the whole download
        backend_host = None
    try:
        control = _control(hass, entry_data.get("control"))
    except Exception as err:  # noqa: BLE001 — one broken section must not fail the whole download
        control = {"error": type(err).__name__}
    return {
        "entry": {
            "mode": mode if isinstance(mode, str) else _MODE_FORECAST,
            "version": getattr(runner, "integration_version", None) or "unknown",
            "paired": isinstance(backend, dict),
            "backend_host": backend_host,
            "options_keys": sorted(entry.options),
        },
        "discovery": (runner.report if runner is not None and runner.report
                      else {"status": "pending"}),
        "control": control,
    }
