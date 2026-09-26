"""Raport rozpoznania (schemat 1): budowa, podsumowanie, maskowanie seriali."""
from __future__ import annotations

import json
import re

from .known import INVERTER_DOMAINS
from .models import Classification, EntitySnap, StateSnap
from .network import NetworkProbeResult

REPORT_SCHEMA = 1
# Krótsze tokeny (<6 znaków) maskowalibyśmy jako "serial" nawet gdy trafiają na zwykłe
# słowa/skróty w nazwach encji (np. domeny, jednostki) — z tego powodu próg zostaje na 6,
# mimo że odcina to teoretycznie krótsze numery seryjne (ruling: fałszywe maskowanie
# zwykłych słów jest gorsze niż rzadki, krótki numer seryjny, który prześlizgnie się przez próg).
_MIN_SERIAL = 6
# 12 cyfr szesnastkowych z opcjonalnymi separatorami ':' lub '-' co dwa znaki — łapie
# adresy MAC nawet w nieparsowalnym surowym tekście odpowiedzi 48899 (Task 5 fix round 1).
_MAC_RE = re.compile(
    r"(?i)\b([0-9a-f]{2})[:\-]?([0-9a-f]{2})[:\-]?([0-9a-f]{2})"
    r"[:\-]?([0-9a-f]{2})[:\-]?([0-9a-f]{2})[:\-]?([0-9a-f]{2})\b"
)


def _clean_serial(v: str | None) -> str | None:
    """Usuwa białe znaki i NUL z brzegów — odczyty Modbus bywają dopełniane spacjami/NUL."""
    if not v:
        return None
    v = v.strip(" \t\r\n\x00")
    return v or None


def _serial_pattern(serials: set[str]) -> re.Pattern[str] | None:
    escaped = sorted(
        (re.escape(s) for s in serials if s and len(s) >= _MIN_SERIAL),
        key=len, reverse=True,
    )
    return re.compile("|".join(escaped), re.IGNORECASE) if escaped else None


def mask_serials(text, serials: set[str]):
    if not isinstance(text, str):
        return text
    pattern = _serial_pattern(serials)
    return pattern.sub("<SN>", text) if pattern else text


def _mask_mac_in_text(text: str) -> str:
    def _repl(m: re.Match[str]) -> str:
        return (m.group(1) + m.group(2) + m.group(3)).upper() + "******"
    return _MAC_RE.sub(_repl, text)


def _mask_value(value, pattern: re.Pattern[str] | None):
    """Ostateczny, rekurencyjny przebieg maskujący po zbudowaniu całego raportu —
    łapie serial/MAC w KAŻDYM polu tekstowym (w tym entity_id, unique_id, errors,
    model, wersje, options, raw), niezależnie od tego, czy konkretne pole zostało
    już zamaskowane punktowo przy budowie."""
    if isinstance(value, str):
        masked = pattern.sub("<SN>", value) if pattern else value
        return _mask_mac_in_text(masked)
    if isinstance(value, list):
        return [_mask_value(v, pattern) for v in value]
    if isinstance(value, dict):
        return {k: _mask_value(v, pattern) for k, v in value.items()}
    return value


def _tail(v: str | None) -> str | None:
    return None if not v else "…" + v[-4:]


def _mac(v: str | None) -> str | None:
    return None if not v else v[:6] + "*" * max(0, len(v) - 6)


def _serials(c: Classification, net: NetworkProbeResult | None) -> set[str]:
    out: set[str] = set()
    for inv in c.inverters:
        for d in inv.devices:
            s = _clean_serial(d.serial_number)
            if s and len(s) >= _MIN_SERIAL:
                out.add(s)
            out.update(
                cv for _, v in d.identifiers
                if (cv := _clean_serial(v)) and len(cv) >= _MIN_SERIAL
            )
    for r in (net.replies if net else []):
        name = _clean_serial(r.name)
        if name and len(name) >= _MIN_SERIAL and any(ch.isdigit() for ch in name):
            out.add(name)
    return out


def _entity(e: EntitySnap, states: dict[str, StateSnap]) -> dict:
    st = states.get(e.entity_id)
    a = st.attributes if st else {}
    return {
        "entity_id": e.entity_id, "entity_domain": e.entity_id.split(".", 1)[0],
        "platform": e.platform, "unique_id": e.unique_id,
        "device_class": e.device_class or a.get("device_class"),
        "unit": e.unit or a.get("unit_of_measurement"),
        "translation_key": e.translation_key,
        "original_name": e.original_name, "disabled": e.disabled,
        "state": st.state if st else None,
        "options": a.get("options"), "min": a.get("min"), "max": a.get("max"),
        "step": a.get("step"), "state_class": a.get("state_class"),
    }


def build_report(*, classification, states, history_days, network, errors,
                 integration_version, ha_version, generated_at) -> dict:
    sn = _serials(classification, network)
    inverters = []
    for inv in classification.inverters:
        inverters.append({
            "domain": inv.domain, "brand_hint": INVERTER_DOMAINS.get(inv.domain),
            "matched_by": inv.matched_by,
            "config_entry_title": inv.config_entry_title, "host": inv.host,
            "devices": [{
                "manufacturer": d.manufacturer, "model": d.model,
                "name": d.name, "sw_version": d.sw_version,
                "hw_version": d.hw_version, "serial": _tail(d.serial_number),
                "identifiers": [[dom, v] for dom, v in d.identifiers],
            } for d in inv.devices],
            "entities": [_entity(e, states) for e in inv.entities],
        })
    def _st(eid):
        s = states.get(eid)
        return s.attributes if s else {}
    report = {
        "schema": REPORT_SCHEMA, "generated_at": generated_at,
        "integration_version": integration_version, "ha_version": ha_version,
        "inverters": inverters,
        "price_entities": [{"entity_id": e.entity_id, "platform": e.platform,
                            "unit": e.unit or _st(e.entity_id).get("unit_of_measurement"),
                            "state": states[e.entity_id].state if e.entity_id in states else None}
                           for e in classification.price_entities],
        "energy_sensors": [{"entity_id": e.entity_id, "platform": e.platform,
                            "unit": e.unit or _st(e.entity_id).get("unit_of_measurement"),
                            "state_class": _st(e.entity_id).get("state_class"),
                            "days_of_statistics": history_days.get(e.entity_id)}
                           for e in classification.energy_candidates],
        "network": {"udp_48899": None if network is None else {
            "sent": network.sent, "error": network.error,
            "replies": [{"ip": r.ip, "mac": _mac(r.mac), "name": _tail(r.name),
                         "raw": r.raw} for r in network.replies]}},
        "errors": list(errors),
    }
    # Ostatni przebieg: maskuje serial (case-insensitive, każde pole tekstowe) i każdy
    # 12-cyfrowy szesnastkowy MAC w dowolnym miejscu — także tam, gdzie 48899 nie
    # sparsowało odpowiedzi na ip/mac/name i cały tekst trafił tylko do "raw".
    pattern = _serial_pattern(sn)
    return _mask_value(report, pattern)


def summarize(report: dict) -> str:
    invs = report.get("inverters") or []
    if invs:
        first = invs[0]
        model = next((d.get("model") for d in first["devices"] if d.get("model")), None)
        inv = f"{first['domain']}{f' ({model})' if model else ''}: {len(first['entities'])} entities"
        if len(invs) > 1:
            inv += f" (+{len(invs) - 1} more)"
    else:
        inv = "No inverter integration found"
    prices = sorted({p["platform"] for p in report.get("price_entities") or []})
    price = f"prices: {', '.join(prices)}" if prices else "no price entities"
    days = [s["days_of_statistics"] for s in report.get("energy_sensors") or []
            if s.get("days_of_statistics")]
    hist = f" · history: {max(days)} d" if days else ""
    udp = (report.get("network") or {}).get("udp_48899")
    net = f" · network: {len(udp['replies'])} loggers" if udp and udp.get("sent") else " · network: n/a"
    return f"{inv} · {price}{hist}{net}"[:255]


def compact_attributes(report: dict) -> dict:
    out = {
        "schema": report["schema"], "generated_at": report["generated_at"],
        "inverters": [{"domain": i["domain"], "brand_hint": i["brand_hint"], "host": i["host"],
                       "models": sorted({d["model"] for d in i["devices"] if d.get("model")}),
                       "entity_count": len(i["entities"])} for i in report["inverters"]][:5],
        "price_platforms": sorted({p["platform"] for p in report["price_entities"]}),
        "max_history_days": max([s["days_of_statistics"] or 0 for s in report["energy_sensors"]] or [0]),
        "loggers": len(((report.get("network") or {}).get("udp_48899") or {}).get("replies") or []),
        "errors": report["errors"][:5],
    }
    while len(json.dumps(out)) > 4096 and out["errors"]:
        out["errors"].pop()
    return out
