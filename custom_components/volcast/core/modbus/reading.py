"""Obraz rejestrów → stan urządzenia w konwencji rdzenia sterowania.

`values` — klucze mapy `read` profilu (jak telemetria z encji), bez kluczy `serial`.
`device` — klucze zapisu w konwencji `Params.flatten`: tryb jako nazwa z profilu albo
`"?<wartość>"` dla wartości spoza profilu (obcy tryb), przełącznik jako 1.0/0.0, dla modelu
okien czasowych pola programów `tou.<i>.<pole>` i `tou_enabled`. Klucz bez odczytu nie
trafia do `device` (brak odczytu, nigdy zero).

Nastawa tylko do zapisu (bez klucza w mapie `read`) jest czytana z własnego rejestru zapisu według
jego typu, skali, kolejności słów i adresu urządzenia — ta sama wartość, którą zapis by ustawił
(migawka stanu wyjściowego przywraca ją potem zapisem). Przełącznik zdalnego sterowania (`enable`):
z odczytu `enable_state` albo z rejestru zapisu; wartość inna niż `on`/`off` profilu to brak odczytu.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..params import TouProgram
from ..registers import RegisterError, RegisterImage, decode, read_values

_FLAT_KEYS = ("soc_min", "soc_max", "power_w", "export_limit_w", "charge_power_w", "discharge_power_w")


@dataclass(frozen=True)
class DirectReading:
    values: dict[str, float | str | None]
    device: dict[str, float | str]
    programs: tuple[TouProgram, ...] | None
    tou_enabled: bool | None
    image: RegisterImage
    at_mono: float
    at_utc: datetime


def _word(image: RegisterImage, addr: int) -> int | None:
    try:
        return image.words(addr, 1)[0]
    except RegisterError:
        return None


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _write_register_value(spec, image: RegisterImage) -> float | None:
    """Wartość rejestru zapisu jak przy odczycie: typ (u16/u32), skala, kolejność słów, adres urządzenia."""
    read_spec = {"addr": spec["addr"], "type": spec.get("type", "u16")}
    for field in ("scale", "word_order", "unit_id"):
        if field in spec:
            read_spec[field] = spec[field]
    try:
        return _num(decode(read_spec, image))
    except RegisterError:
        return None


def _enable_state(write, read_map, values, image: RegisterImage) -> float | None:
    spec = write["enable"]
    raw = _num(values.get("enable_state")) if "enable_state" in read_map else _write_register_value(spec, image)
    if raw is None:
        return None
    if raw == spec["on"]:
        return 1.0
    if raw == spec["off"]:
        return 0.0
    return None                                    # wartość spoza profilu — nie zgadujemy stanu


def current_programs(profile, image: RegisterImage) -> tuple[TouProgram, ...] | None:
    """Programy TOU z rejestrów; brak rejestru albo niepoprawne HHMM → None (fail-closed)."""
    tp = profile.raw["write"].get("tou_program")
    if tp is None:
        return None
    bit = 1 << tp["grid_charge"]["bit"]
    out: list[TouProgram] = []
    for i in range(tp["count"]):
        words = [_word(image, tp[f]["addr"] + i) for f in ("start", "power_w", "soc", "grid_charge")]
        if any(w is None for w in words):
            return None
        hhmm, power, soc, charge = words
        h, m = divmod(hhmm, 100)
        if h > 23 or m > 59:
            return None
        out.append(TouProgram(start_min=h * 60 + m, power_w=float(power), soc=float(soc),
                              grid_charge=bool(charge & bit)))
    return tuple(out)


def _tou_enabled(profile, image: RegisterImage) -> bool | None:
    spec = profile.raw["write"].get("tou_enable")
    if spec is None:
        return None
    word = _word(image, spec["addr"])
    return None if word is None else bool(word & (1 << spec["enable_bit"]))


def build_reading(profile, image: RegisterImage, *, at_mono: float, at_utc: datetime) -> DirectReading:
    read_map = {k: v for k, v in profile.raw["read"].items() if k != "serial"}
    values = read_values(read_map, image)
    write = profile.raw["write"]
    device: dict[str, float | str] = {}
    if "mode" in write:
        raw = _num(values.get("mode_value"))
        if raw is not None:
            mode = profile.mode_by_value(int(raw))
            device["mode"] = mode.name if mode is not None else f"?{int(raw)}"
    for key in _FLAT_KEYS:
        v = _num(values.get(key))
        if v is None and key in write and key not in read_map:
            # Klucz tylko do zapisu (np. górny próg SoC): odczyt z jego własnego rejestru.
            v = _write_register_value(write[key], image)
        if key in write and v is not None:
            device[key] = v
    if "enable" in write:
        v = _enable_state(write, read_map, values, image)
        if v is not None:
            device["enable"] = v
    if "export_limit_enabled" in write:
        v = _num(values.get("export_limit_enabled"))
        if v is not None:
            device["export_limit_enabled"] = 1.0 if v else 0.0
    programs = current_programs(profile, image)
    for i, p in enumerate(programs or (), start=1):
        device[f"tou.{i}.start"] = float(p.start_min)
        device[f"tou.{i}.power_w"] = float(p.power_w)
        device[f"tou.{i}.soc"] = float(p.soc)
        device[f"tou.{i}.grid_charge"] = 1.0 if p.grid_charge else 0.0
    enabled = _tou_enabled(profile, image)
    if enabled is not None:
        device["tou_enabled"] = 1.0 if enabled else 0.0
    return DirectReading(values=values, device=device, programs=programs, tou_enabled=enabled,
                         image=image, at_mono=at_mono, at_utc=at_utc)
