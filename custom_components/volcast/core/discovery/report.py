"""Raport rozpoznania (schemat 1): budowa, podsumowanie, maskowanie seriali."""
from __future__ import annotations

import json
import re
from collections.abc import Iterable

from .known import INVERTER_DOMAINS
from .models import Classification, DeviceSnap, EntitySnap, StateSnap
from .network import NetworkProbeResult

REPORT_SCHEMA = 1
# Krótsze tokeny (<6 znaków) maskowalibyśmy jako "serial" nawet gdy trafiają na zwykłe
# słowa/skróty w nazwach encji (np. domeny, jednostki) — z tego powodu próg zostaje na 6,
# mimo że odcina to teoretycznie krótsze numery seryjne: fałszywe maskowanie zwykłych
# słów jest gorsze niż rzadki, krótki numer seryjny, który prześlizgnie się przez próg.
_MIN_SERIAL = 6

# 12 cyfr szesnastkowych z opcjonalnymi separatorami ':' lub '-' co dwa znaki — łapie
# adresy MAC nawet w nieparsowalnym surowym tekście odpowiedzi 48899. Granice liczone
# lookaroundami, nie \b — '_' i litery są "znakami słowa", więc \b by ich nie zatrzymał
# (np. unique_id "aabbccddeeff_rssi"). Sam ciąg 12 cyfr bez separatora i bez litery A-F
# nie jest maskowany jako MAC — zbyt niepewne.
_MAC_RE = re.compile(
    r"(?i)(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{2}[:\-]?){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f])"
)

_IPV4_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")

# E-mail w dowolnym polu tekstowym (np. identyfikator konta w integracji chmurowej,
# tytuł wpisu konfiguracji) — maskowany bezwarunkowo w ostatnim przebiegu. Kwantyfikatory
# są ograniczone (limity długości części adresu z RFC 5321/1035), a etykiety domeny nie
# zawierają kropki — koszt dopasowania jest liniowy względem długości tekstu, bez
# katastrofalnego nawracania (maskowanie biegnie synchronicznie w pętli zdarzeń).
_EMAIL_RE = re.compile(
    r"(?i)[A-Z0-9._%+-]{1,64}@[A-Z0-9-]{1,63}(?:\.[A-Z0-9-]{1,63}){0,8}\.[A-Z]{2,24}"
)

# Górna granica długości tekstu w raporcie. Dłuższe wartości (np. atrybut stanu z
# całym dokumentem) są przycinane PRZED maskowaniem — ogranicza to czas przebiegu,
# a diagnostyka nie potrzebuje pełnej treści.
_MAX_TEXT = 2048

# Klucze niosące słownik/kontrakt raportu (nasz własny kod, stałe słownictwo), nie dane
# właściciela — maskowanie ich wartości byłoby fałszywym trafieniem (domena równa nazwie
# producenta, wersja integracji). "host"/"ip" oraz "unit" celowo NIE są na liście:
# host bywa hostname'em niosącym serial/MAC (np. "SMA<serial>.local",
# "deye-<mac>.local"), a jednostka bywa dowolnym tekstem ze stanu encji — oba muszą
# przechodzić przez maskowanie.
_STRUCTURAL_KEYS = frozenset({
    "domain", "entity_domain", "platform", "brand_hint", "matched_by",
    "schema", "generated_at", "integration_version", "ha_version", "translation_key",
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
    IPv4 — inaczej host/adres dongla trafiałby do zbioru seriali."""
    return bool(
        s and len(s) >= _MIN_SERIAL
        and any(ch.isdigit() for ch in s)
        and not _looks_like_ip(s)
    )


def _serial_alt(s: str) -> str:
    """Wariant wzorca niewrażliwy na separatory: dzieli serial na kawałki alfanumeryczne
    i łączy je wzorcem dopuszczającym dowolne separatory — tak by np.
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


def _mac_hex(v: str | None) -> str | None:
    """12 czystych cyfr szesnastkowych (bez separatorów, wielkie litery) albo None."""
    if not v:
        return None
    hexonly = re.sub(r"[^0-9A-Fa-f]", "", v)
    return hexonly.upper() if len(hexonly) == 12 else None


def _mac_alt(hex12: str) -> str:
    """Wariant niewrażliwy na separatory dla znanego (sparsowanego) MAC-a — w
    przeciwieństwie do `_MAC_RE` nie wymaga braku sąsiedztwa szesnastkowego, bo znamy
    dokładną wartość (łapie też MAC sklejony z innymi znakami hex, np.
    "MACAABBCCDDEEFFSN…")."""
    pairs = [hex12[i:i + 2] for i in range(0, 12, 2)]
    return r"[\W_]*".join(re.escape(p) for p in pairs)


def _known_mac_pattern(macs: set[str]) -> re.Pattern[str] | None:
    """Jeden skompilowany wzorzec dla wszystkich znanych MAC-ów raportu."""
    if not macs:
        return None
    return re.compile("|".join(_mac_alt(h) for h in sorted(macs)), re.IGNORECASE)


def _known_mac_repl(m: re.Match[str]) -> str:
    return re.sub(r"[^0-9A-Fa-f]", "", m.group(0))[:6].upper() + "******"


def _mask_value(value, pattern: re.Pattern[str] | None,
                mac_pattern: re.Pattern[str] | None = None):
    """Ostateczny, rekurencyjny przebieg maskujący po zbudowaniu całego raportu — łapie
    serial/MAC/e-mail w każdym polu tekstowym (w tym entity_id, unique_id, errors, model,
    wersje, options, raw, host), a także wewnątrz list/krotek/zbiorów i kluczy słowników
    omijając pola strukturalne (_STRUCTURAL_KEYS), których wartości są słownikiem/
    kontraktem raportu, nie danymi właściciela ("host"/"ip"/"unit" nie są pomijane)."""
    if isinstance(value, str):
        if len(value) > _MAX_TEXT:
            value = value[:_MAX_TEXT] + "…"
        masked = pattern.sub("<SN>", value) if pattern else value
        masked = mac_pattern.sub(_known_mac_repl, masked) if mac_pattern else masked
        masked = _mask_mac_in_text(masked)
        return _EMAIL_RE.sub("<EMAIL>", masked)
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            new_k = _mask_value(k, pattern, mac_pattern) if isinstance(k, str) else k
            skip = isinstance(k, str) and k in _STRUCTURAL_KEYS
            out[new_k] = v if skip else _mask_value(v, pattern, mac_pattern)
        return out
    if isinstance(value, list):
        return [_mask_value(v, pattern, mac_pattern) for v in value]
    if isinstance(value, tuple):
        return tuple(_mask_value(v, pattern, mac_pattern) for v in value)
    if isinstance(value, (set, frozenset)):
        return type(value)(_mask_value(v, pattern, mac_pattern) for v in value)
    return value


def _tail(v: str | None) -> str | None:
    return None if not v else "…" + v[-4:]


def _mac(v: str | None) -> str | None:
    return None if not v else v[:6] + "*" * max(0, len(v) - 6)


# Identyfikator urządzenia jest emitowany wprost — bez progu na cyfrę: klucz kontowy w
# integracji chmurowej może być e-mailem albo nazwą bez cyfr ("abc:extra" ze starego
# formatu identyfikatorów). Maskujemy więc wartość identyfikatora POLOWO, niezależnie
# od ogólnego zbioru seriali.
_MIN_IDENTIFIER = 4


def _mask_identifier_value(v: str) -> str:
    cleaned = _clean_serial(v)
    return "<SN>" if cleaned and len(cleaned) >= _MIN_IDENTIFIER else v


def _device_serials(devices: Iterable[DeviceSnap], out: set[str]) -> None:
    for d in devices:
        s = _clean_serial(d.serial_number)
        if _valid_serial(s):
            out.add(s)
        for _, v in d.identifiers:
            cv = _clean_serial(v)
            if _valid_serial(cv):
                out.add(cv)


def _reported_device_ids(c: Classification) -> set[str]:
    """Urządzenia, których encje trafiają do raportu (falownik, ceny, czujniki energii)."""
    ents = [e for inv in c.inverters for e in inv.entities]
    ents += list(c.price_entities) + list(c.energy_candidates)
    return {e.device_id for e in ents if e.device_id}


def _serials(c: Classification, net: NetworkProbeResult | None,
             devices: Iterable[DeviceSnap] = ()) -> set[str]:
    """Kandydaci na serial: urządzenia znalezisk falownika, każde inne urządzenie, którego
    encja jest w raporcie (np. czujnik energii z bramki spoza listy falowników niesie
    serial w entity_id), oraz nazwy loggerów z odpowiedzi 48899."""
    out: set[str] = set()
    _device_serials((d for inv in c.inverters for d in inv.devices), out)
    reported = _reported_device_ids(c)
    _device_serials((d for d in devices if d.id in reported), out)
    for r in (net.replies if net else []):
        name = _clean_serial(r.name)
        if _valid_serial(name):
            out.add(name)
    return out


def _known_macs(net: NetworkProbeResult | None) -> set[str]:
    """Sparsowane MAC-i z odpowiedzi 48899 — maskowane dokładnie (exact-match), nawet gdy
    składają się z samych cyfr albo są sklejone z innym tekstem szesnastkowym, bo znamy
    ich dokładną wartość."""
    out: set[str] = set()
    for r in (net.replies if net else []):
        h = _mac_hex(r.mac)
        if h:
            out.add(h)
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
                 integration_version, ha_version, generated_at,
                 devices: Iterable[DeviceSnap] = ()) -> dict:
    """`devices` — migawki wszystkich aktywnych urządzeń; seriale bierzemy tylko z tych,
    których encje trafiają do raportu."""
    sn = _serials(classification, network, devices)
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
                "identifiers": [[dom, _mask_identifier_value(v)] for dom, v in d.identifiers],
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
    # w każdym polu tekstowym/liście/krotce/zbiorze/kluczu, W TYM "host"/"ip"/"unit"),
    # sparsowane MAC-i dokładnie (nawet same cyfry albo sklejone z hex tekstem), każdy
    # inny 12-cyfrowy szesnastkowy MAC heurystycznie, i e-mail — także tam, gdzie 48899
    # nie sparsowało odpowiedzi na ip/mac/name i cały tekst trafił tylko do "raw".
    pattern = _serial_pattern(sn)
    mac_pattern = _known_mac_pattern(_known_macs(network))
    return _mask_value(report, pattern, mac_pattern)


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
