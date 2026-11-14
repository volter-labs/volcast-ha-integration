"""Decyzja przepięcia celu bezpośredniego na nowy adres — czysta funkcja, bez I/O, fail-closed.

Wejście: zapisany cel (`OPT_DIRECT_TARGET`), raporty sondy (`ProbeReport`) z wyszukiwania oraz
kolizje adresów (`host → domeny`, jak `static_conflicts` / `async_clash`). Nowy adres jest przyjmowany
tylko dla TEGO SAMEGO egzemplarza:

* kandydat ma identyczne `device_fp`, `profile_id`, `transport`, `port` i `unit_id` co cel;
* dokładnie jeden adres z tym odciskiem (więcej = `ambiguous`; obecny adres też się liczy);
* adres zajęty (albo niesprawdzony — brak klucza w `clashes`) = `clash`;
* cel bez odcisku (albo bez profilu / transportu) nigdy nie jest przepinany (`not_found`);
* tylko odcisk z serialem (`fp_kind == "serial"`) może dać przepięcie. Odcisk słaby (`model|moc`,
  profil bez rejestru seriala) oraz brak / nieznany `fp_kind` dają zawsze `not_found`: dwa
  identyczne modele o tej samej mocy mają ten sam odcisk, a wynik wyszukiwania nie widzi
  wyłączonego oryginału ani nie jest pełny, więc bliźniak pod nowym adresem jest nie do odróżnienia.
  Ręczne wyszukiwanie w opcjach (decyzja użytkownika) zostaje dostępne.

Adres i numer loggera nie trafiają do `repr` wyniku (logi, diagnostyka).
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Sequence

from .probe import ProbeReport

FP_SERIAL = "serial"            # odcisk z `profil|serial` — jednoznaczny egzemplarz
FP_MODEL = "model"              # odcisk z `profil|model|moc` — dwa identyczne modele są nie do odróżnienia

Outcome = Literal["relocated", "same_host", "not_found", "ambiguous", "foreign_only", "clash"]

_V5 = "solarman_v5"


@dataclass(frozen=True)
class Relocation:
    outcome: Outcome
    host: str | None = field(default=None, repr=False)            # tylko dla `relocated`
    logger_serial: int | None = field(default=None, repr=False)   # tylko `solarman_v5`; None = bez zmiany


_NOT_FOUND = Relocation("not_found")
_AMBIGUOUS = Relocation("ambiguous")
_FOREIGN = Relocation("foreign_only")
_SAME = Relocation("same_host")
_CLASH = Relocation("clash")


def fingerprint_kind(profile: Any) -> str:
    """Rodzaj odcisku z profilu — ta sama reguła co `device_fingerprint` (rejestr `serial` w `identify`)."""
    raw = getattr(profile, "raw", None)
    ident = raw.get("identify") if isinstance(raw, Mapping) else None
    regs = ident.get("registers") if isinstance(ident, Mapping) else None
    return FP_SERIAL if isinstance(regs, Mapping) and "serial" in regs else FP_MODEL


def _canon(host: Any) -> str | None:
    """Postać kanoniczna literału (z odpakowaniem `::ffff:a.b.c.d` jak `check_target`); inny tekst bez zmian."""
    if not isinstance(host, str) or not host:
        return None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host
    mapped = getattr(ip, "ipv4_mapped", None)
    return (mapped or ip).compressed


def _same_device(target: Mapping, report: ProbeReport) -> bool:
    i = report.identity
    return (i is not None and report.candidate is not None
            and i.device_fp == target["device_fp"]
            and i.profile_id == target["profile_id"]
            and i.transport == target["transport"]
            and i.port == target.get("port")
            and i.unit_id == target.get("unit_id"))


def _logger_serial(target: Mapping, matches: Sequence[ProbeReport]) -> tuple[bool, int | None]:
    """(spójne, numer) — numer loggera z kandydata dla `solarman_v5`; dwa różne numery = niespójne."""
    if target["transport"] != _V5:
        return True, None
    serials = {s for s in (r.candidate.logger_serial for r in matches)
               if isinstance(s, int) and not isinstance(s, bool)}
    if len(serials) > 1:
        return False, None
    return True, next(iter(serials), None)


def decide_relocation(target: Mapping, reports: Sequence[ProbeReport], *,
                      clashes: Mapping[str, Sequence[str]]) -> Relocation:
    """Czy i dokąd przepiąć cel; każdy wątpliwy przypadek kończy się wynikiem bez adresu (brak zmian)."""
    fp = target.get("device_fp")
    if not isinstance(fp, str) or not fp or not target.get("profile_id") or not target.get("transport"):
        return _NOT_FOUND
    if target.get("fp_kind") != FP_SERIAL:      # tożsamości nie da się udowodnić (bliźniaki), patrz wyżej
        return _NOT_FOUND
    identified = [r for r in reports
                  if r.identity is not None and r.candidate is not None and r.identity.device_fp]
    if not identified:
        return _NOT_FOUND
    matches = [r for r in identified if _same_device(target, r)]
    if not matches:
        return _FOREIGN
    hosts = {_canon(r.candidate.host) for r in matches}
    if len(hosts) != 1 or None in hosts:
        return _AMBIGUOUS
    host = matches[0].candidate.host
    if _canon(host) == _canon(target.get("host")):
        return _SAME
    consistent, serial = _logger_serial(target, matches)
    if not consistent:
        return _AMBIGUOUS
    clash = clashes.get(host)
    if clash is None or len(clash) > 0:
        return _CLASH
    return Relocation("relocated", host, serial)
