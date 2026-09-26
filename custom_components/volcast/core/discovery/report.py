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
# adresy MAC nawet w nieparsowalnym surowym tekście odpowiedzi 48899. Granice liczone
# lookaroundami, nie \b — '_' i litery są "znakami słowa", więc \b by ich nie zatrzymał
# (np. unique_id "aabbccddeeff_rssi"). Sam ciąg 12 cyfr bez separatora i bez litery A-F
# nie jest maskowany jako MAC — zbyt niepewne (fix round 2, reviews.md Task 5
# re-review 1, R2).
_MAC_RE = re.compile(
    r"(?i)(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{2}[:\-]?){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f])"
)

_IPV4_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")

# Klucze niosące słownik/kontrakt raportu, nie dane właściciela — maskowanie ich wartości
# byłoby fałszywym trafieniem (np. host/ip wyglądający na numeryczny ciąg, domena równa
# nazwie producenta). Fix round 2, R4.
_STRUCTURAL_KEYS = frozenset({
    "domain", "entity_domain", "platform", "brand_hint", "matched_by", "host", "ip",
    "schema", "generated_at", "integration_version", "ha_version", "device_class",
    "unit", "state_class", "translation_key",
})


def _clean_serial(v: str | None) -> str | None:
    """Usuwa białe znaki i NUL z brzegów — odczyty Modbus bywają dopełniane spacjami/NUL."""
    if not v:
        return None
    v = v.strip(" \t\r\n\x00")
    return v or None


def _looks_like_ip(s: str) -> bool:
    m = _IPV4_RE.match(s)
    return bool(m) and all(0 <= int(g) <= 255 for g in m.groups())


def _valid_serial(s: str | None) -> bool:
    """Kandydat na serial: min. długość, przynajmniej jedna cyfra, nie wygląda jak adres
    IPv4 (fix round 2, R4) — inaczej host/adres dongla trafiałby do zbioru seriali."""
    return bool(
        s and len(s) >= _MIN_SERIAL
        and any(ch.isdigit() for ch in s)
        and not _looks_like_ip(s)
    )


def _serial_alt(s: str) -> str:
    """Wariant wzorca niewrażliwy na separatory: dzieli serial na kawałki alfanumeryczne
    i łączy je wzorcem dopuszczającym dowolne separatory (fix round 2, R1) — tak by np.
    serial "7F123456-78" złapał też slugifikowane "7f123456_78" w entity_id."""
    chunks = [c for c in re.split(r"[\W_]+", s) if c]
    if not chunks:
        return re.escape(s)
    return r"[\W_]*".join(re.escape(c) for c in chunks)


def _serial_pattern(serials: set[str]) -> re.Pattern[str] | None:
    valid = [s for s in serials if s and len(s) >= _MIN_SERIAL]
    if not valid:
        return None
    alts = sorted((_serial_alt(s) for s in valid), key=len, reverse=True)
    return re.compile("|".join(alts), re.IGNORECASE)


def mask_serials(text, serials: set[str]):
    if not isinstance(text, str):
        return text
    pattern = _serial_pattern(serials)
    return pattern.sub("<SN>", text) if pattern else text


def _mac_repl(m: re.Match[str]) -> str:
    raw = m.group(0)
    has_letter = any(ch in "abcdefABCDEF" for ch in raw)
    has_sep = any(ch in ":-" for ch in raw)
    if not (has_letter or has_sep):
        return raw  # sam ciąg 12 cyfr — zbyt niepewne, żeby traktować jak MAC
    hexdigits = raw.replace(":", "").replace("-", "")
    return hexdigits[:6].upper() + "******"


def _mask_mac_in_text(text: str) -> str:
    return _MAC_RE.sub(_mac_repl, text)


def _mask_value(value, pattern: re.Pattern[str] | None):
    """Ostateczny, rekurencyjny przebieg maskujący po zbudowaniu całego raportu — łapie
    serial/MAC w każdym polu tekstowym (w tym entity_id, unique_id, errors, model,
    wersje, options, raw), a także wewnątrz list/krotek/zbiorów i kluczy słowników
    (fix round 2, R3), omijając pola strukturalne (_STRUCTURAL_KEYS), których wartości
    są słownikiem/kontraktem raportu, nie danymi właściciela (fix round 2, R4)."""
    if isinstance(value, str):
        masked = pattern.sub("<SN>", value) if pattern else value
        return _mask_mac_in_text(masked)
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            new_k = _mask_value(k, pattern) if isinstance(k, str) else k
            skip = isinstance(k, str) and k in _STRUCTURAL_KEYS
            out[new_k] = v if skip else _mask_value(v, pattern)
        return out
    if isinstance(value, list):
        return [_mask_value(v, pattern) for v in value]
    if isinstance(value, tuple):
        return tuple(_mask_value(v, pattern) for v in value)
    if isinstance(value, (set, frozenset)):
        return type(value)(_mask_value(v, pattern) for v in value)
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
            if _valid_serial(s):
                out.add(s)
            for _, v in d.identifiers:
                cv = _clean_serial(v)
                if _valid_serial(cv):
                    out.add(cv)
    for r in (net.replies if net else []):
        name = _clean_serial(r.name)
        if _valid_serial(name):
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
    # Ostatni przebieg: maskuje serial (case-insensitive, niewrażliwie na separatory,
    # w każdym polu tekstowym/liście/krotce/zbiorze/kluczu) i każdy 12-cyfrowy szesnastkowy
    # MAC w dowolnym miejscu — także tam, gdzie 48899 nie sparsowało odpowiedzi na
    # ip/mac/name i cały tekst trafił tylko do "raw" — z pominięciem pól strukturalnych.
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
