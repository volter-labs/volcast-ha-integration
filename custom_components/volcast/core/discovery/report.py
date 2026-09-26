"""Raport rozpoznania (schemat 1): budowa, podsumowanie, maskowanie seriali."""
from __future__ import annotations

import json

from .known import INVERTER_DOMAINS
from .models import Classification, EntitySnap, StateSnap
from .network import NetworkProbeResult

REPORT_SCHEMA = 1
_MIN_SERIAL = 6


def mask_serials(text, serials: set[str]):
    if not isinstance(text, str):
        return text
    for s in sorted((s for s in serials if len(s) >= _MIN_SERIAL), key=len, reverse=True):
        text = text.replace(s, "<SN>")
    return text


def _tail(v: str | None) -> str | None:
    return None if not v else "…" + v[-4:]


def _mac(v: str | None) -> str | None:
    return None if not v else v[:6] + "*" * max(0, len(v) - 6)


def _serials(c: Classification, net: NetworkProbeResult | None) -> set[str]:
    out: set[str] = set()
    for inv in c.inverters:
        for d in inv.devices:
            if d.serial_number:
                out.add(d.serial_number)
            out.update(v for _, v in d.identifiers if len(v) >= _MIN_SERIAL)
    for r in (net.replies if net else []):
        if r.name and len(r.name) >= _MIN_SERIAL and any(ch.isdigit() for ch in r.name):
            out.add(r.name)
    return out


def _entity(e: EntitySnap, states: dict[str, StateSnap], sn: set[str]) -> dict:
    st = states.get(e.entity_id)
    a = st.attributes if st else {}
    return {
        "entity_id": e.entity_id, "entity_domain": e.entity_id.split(".", 1)[0],
        "platform": e.platform, "unique_id": mask_serials(e.unique_id, sn),
        "device_class": e.device_class or a.get("device_class"),
        "unit": e.unit or a.get("unit_of_measurement"),
        "translation_key": e.translation_key,
        "original_name": mask_serials(e.original_name, sn), "disabled": e.disabled,
        "state": mask_serials(st.state, sn) if st else None,
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
            "config_entry_title": mask_serials(inv.config_entry_title, sn), "host": inv.host,
            "devices": [{
                "manufacturer": d.manufacturer, "model": d.model,
                "name": mask_serials(d.name, sn), "sw_version": d.sw_version,
                "hw_version": d.hw_version, "serial": _tail(d.serial_number),
                "identifiers": [[dom, mask_serials(v, sn)] for dom, v in d.identifiers],
            } for d in inv.devices],
            "entities": [_entity(e, states, sn) for e in inv.entities],
        })
    def _st(eid):
        s = states.get(eid)
        return s.attributes if s else {}
    return {
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
                         "raw": mask_serials(r.raw, sn)} for r in network.replies]}},
        "errors": list(errors),
    }


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
