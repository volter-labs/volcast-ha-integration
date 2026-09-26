"""Walidator profilu marki (schemat 1) — ręczny, tylko stdlib.

Zewnętrzny walidator schematów nie jest dostępny w środowisku Home Assistant,
a błąd profilu ma wyjść przy ładowaniu (z dokładną ścieżką pola), nigdy jako
nieobsłużony wyjątek w torze zapisu do falownika. Dlatego każde porównanie,
które mogłoby dostać niespodziewany typ (np. listę zamiast tekstu), jest
najpierw sprawdzane co do typu.
"""
from __future__ import annotations

import math
import re
from typing import Any

from .params import TOU_FIELDS, WRITE_PARAMS

READ_KEYS = frozenset({
    "soc", "battery_temp_c", "battery_voltage_v", "battery_current_a", "battery_power_w",
    "pv_power_w", "grid_power_w", "load_power_w", "active_power_w", "pv_energy_total_kwh",
    "grid_import_total_kwh", "grid_export_total_kwh",
    # odczyt nastaw — do uzgadniania z tym, co falownik faktycznie ma
    "mode_value", "power_w", "soc_min", "soc_max", "export_limit_w", "export_limit_enabled",
})
INTENTS = ("charge_grid", "charge_pv", "discharge_forced", "sell", "self_consume", "standby")
# Tylko te intencje wychodzą z rozstrzygania slotu z gwarantowaną mocą (silnik tryb+nastawa).
POWERED_INTENTS = ("charge_grid", "sell", "discharge_forced")
CAPABILITY_KEYS = ("force_charge_from_grid", "sell_from_battery", "force_discharge", "standby",
                   "set_power_w", "limit_export", "set_soc_floor", "set_soc_ceiling")
REG_TYPES = ("u16", "i16", "u32", "i32", "f32", "ascii")
ENCODE_FOR = {"mode": "mode", "power_w": "watts", "soc_min": "percent", "soc_max": "percent",
              "export_limit_w": "watts", "export_limit_enabled": "bool"}
TOU_ENCODE = {"start": "hhmm", "power_w": "watts", "soc": "percent"}
TRANSPORTS = ("goodwe_udp", "solarman_v5", "modbus_tcp", "modbus_rtu")
_ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_HA_TOU_KEY = re.compile(r"^tou_[1-9]_(start|power_w|soc|grid_charge)$")

_TOP_REQ = ("schema_version", "id", "label", "status", "control_model", "sources", "unit_id",
            "transports", "identify", "read", "write", "intents", "baseline", "write_policy",
            "limits", "ha", "capabilities")
_TOP_OPT = ("modes", "neutral_mode", "tou", "status_note")


class _V:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def err(self, path: str, msg: str) -> None:
        self.errors.append(f"{path}: {msg}")

    def obj(self, v: Any, path: str, req: tuple[str, ...], opt: tuple[str, ...] = ()) -> dict | None:
        if not isinstance(v, dict):
            self.err(path, "oczekiwano obiektu")
            return None
        for k in req:
            if k not in v:
                self.err(f"{path}.{k}", "brak wymaganego pola")
        for k in v:
            if k not in req and k not in opt:
                self.err(f"{path}.{k}", "nieznane pole")
        return v

    def int_(self, v: Any, path: str, lo: int, hi: int) -> bool:
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            self.err(path, f"oczekiwano liczby całkowitej {lo}..{hi}")
            return False
        return True

    def num(self, v: Any, path: str) -> bool:
        # `json.loads` domyślnie akceptuje literały NaN/Infinity — profil ich nie może
        # przemycić, bo gasiłyby porównania (np. próg temperatury) bez żadnego błędu.
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            self.err(path, "oczekiwano liczby")
            return False
        try:
            finite = math.isfinite(v)
        except OverflowError:
            # `json.loads` przyjmuje też int dowolnej wielkości (np. z pliku profilu);
            # konwersja na float w `isfinite` na takiej wartości rzuca OverflowError.
            finite = False
        if not finite:
            self.err(path, "oczekiwano liczby")
            return False
        return True

    def str_(self, v: Any, path: str) -> bool:
        if not isinstance(v, str) or not v:
            self.err(path, "oczekiwano niepustego tekstu")
            return False
        return True

    def bool_(self, v: Any, path: str) -> None:
        if not isinstance(v, bool):
            self.err(path, "oczekiwano true/false")

    def enum(self, v: Any, path: str, choices: tuple[str, ...]) -> bool:
        if v not in choices:
            self.err(path, f"{v!r} spoza {list(choices)}")
            return False
        return True

    def regex(self, v: Any, path: str) -> None:
        if self.str_(v, path):
            try:
                re.compile(v)
            except re.error as e:
                self.err(path, f"zły regex: {e}")

    def str_list(self, v: Any, path: str) -> list | None:
        """Lista samych tekstów — warunek konieczny przed `sorted()`/porównaniem multisetów."""
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            self.err(path, "oczekiwano listy tekstów")
            return None
        return v


def _reg(v: _V, raw: Any, path: str, *, need_type: bool = True) -> None:
    r = v.obj(raw, path, ("addr",) + (("type",) if need_type else ()),
              ("type", "scale", "sign", "undef", "len", "word_order"))
    if r is None:
        return
    v.int_(r.get("addr"), f"{path}.addr", 0, 65535)
    if "type" in r and v.enum(r["type"], f"{path}.type", REG_TYPES):
        if r["type"] == "ascii" and "len" not in r:
            v.err(f"{path}.len", "ascii wymaga len (liczba rejestrów)")
    if "len" in r:
        v.int_(r["len"], f"{path}.len", 1, 64)
    if "scale" in r:
        v.num(r["scale"], f"{path}.scale")
    if "sign" in r and r["sign"] not in (-1, 1):
        v.err(f"{path}.sign", "tylko -1 albo 1")
    if "undef" in r:
        v.int_(r["undef"], f"{path}.undef", 0, 2**32 - 1)
    if "word_order" in r:
        v.enum(r["word_order"], f"{path}.word_order", ("hi_lo", "lo_hi"))


def _read(v: _V, raw: Any) -> None:
    if not isinstance(raw, dict):
        v.err("$.read", "oczekiwano obiektu")
        return
    for key, spec in raw.items():
        path = f"$.read.{key}"
        if key not in READ_KEYS:
            v.err(path, "nieznany klucz odczytu")
            continue
        if isinstance(spec, dict) and "sum" in spec:
            v.obj(spec, path, ("sum",))
            terms = spec["sum"]
            if not isinstance(terms, list) or not terms:
                v.err(f"{path}.sum", "oczekiwano niepustej listy")
                continue
            for i, t in enumerate(terms):
                tp = f"{path}.sum[{i}]"
                if isinstance(t, dict) and "ref" in t:
                    v.obj(t, tp, ("ref",), ("sign",))
                    ref = t.get("ref")
                    if not isinstance(ref, str):
                        v.err(f"{tp}.ref", "oczekiwano tekstu")
                    elif ref not in raw or ref == key:
                        v.err(f"{tp}.ref", f"odwołanie do nieistniejącego klucza {ref!r}")
                    if "sign" in t and t["sign"] not in (-1, 1):
                        v.err(f"{tp}.sign", "tylko -1 albo 1")
                else:
                    _reg(v, t, tp)
        else:
            _reg(v, spec, path)


def _write(v: _V, raw: Any, model: str, tou: dict | None) -> set[str]:
    w = v.obj(raw, "$.write", (), WRITE_PARAMS + ("tou_program",))
    if w is None:
        return set()
    for key in WRITE_PARAMS:
        if key in w:
            s = v.obj(w[key], f"$.write.{key}", ("addr", "type", "encode"))
            if s is not None:
                v.int_(s.get("addr"), f"$.write.{key}.addr", 0, 65535)
                v.enum(s.get("type"), f"$.write.{key}.type", ("u16",))
                if s.get("encode") != ENCODE_FOR[key]:
                    v.err(f"$.write.{key}.encode", f"dla {key} wymagane {ENCODE_FOR[key]!r}")
    if model == "mode_setpoint" and "mode" not in w:
        v.err("$.write.mode", "mode_setpoint wymaga zapisu trybu")
    if model == "time_window":
        tp = v.obj(w.get("tou_program"), "$.write.tou_program", ("count",) + TOU_FIELDS)
        if tp is not None:
            if v.int_(tp.get("count"), "$.write.tou_program.count", 1, 12) and tou is not None \
                    and tp["count"] != tou.get("programs"):
                v.err("$.write.tou_program.count", "musi równać się tou.programs")
            for f in ("start", "power_w", "soc"):
                s = v.obj(tp.get(f), f"$.write.tou_program.{f}", ("addr", "encode"))
                if s is not None:
                    v.int_(s.get("addr"), f"$.write.tou_program.{f}.addr", 0, 65535)
                    if s.get("encode") != TOU_ENCODE[f]:
                        v.err(f"$.write.tou_program.{f}.encode", f"wymagane {TOU_ENCODE[f]!r}")
            g = v.obj(tp.get("grid_charge"), "$.write.tou_program.grid_charge", ("addr", "bit"))
            if g is not None:
                v.int_(g.get("addr"), "$.write.tou_program.grid_charge.addr", 0, 65535)
                v.int_(g.get("bit"), "$.write.tou_program.grid_charge.bit", 0, 15)
    return {("tou" if k == "tou_program" else k) for k in w}


def _intents(v: _V, raw: Any, model: str, modes: dict) -> None:
    it = v.obj(raw, "$.intents", INTENTS)
    if it is None:
        return
    for name in INTENTS:
        if name not in it:
            continue
        spec, path = it[name], f"$.intents.{name}"
        if model == "mode_setpoint":
            s = v.obj(spec, path, ("mode", "power"))
            if s is not None:
                mode = s.get("mode")
                if not isinstance(mode, str) or mode not in modes:
                    v.err(f"{path}.mode", f"tryb {mode!r} nie istnieje w modes")
                power = s.get("power")
                if v.enum(power, f"{path}.power", ("slot", "zero", "none")):
                    if power == "slot" and name not in POWERED_INTENTS:
                        v.err(f"{path}.power",
                              f"'slot' dozwolone tylko dla {list(POWERED_INTENTS)}")
        else:
            if spec is None:
                if name == "self_consume":
                    v.err(path, "samokonsumpcja musi być obsługiwana (to tryb bazowy)")
                continue
            s = v.obj(spec, path, ("grid_charge", "soc", "power"))
            if s is not None:
                v.bool_(s.get("grid_charge"), f"{path}.grid_charge")
                v.enum(s.get("soc"), f"{path}.soc", ("target_or_max", "reserve", "hold"))
                v.enum(s.get("power"), f"{path}.power", ("slot", "max"))


_HA_TOU_DOMAIN = {"start": "time", "grid_charge": "switch", "power_w": "number", "soc": "number"}


def _ha_entity_domain(key: str) -> str:
    """Jedyna dopuszczalna domena encji HA dla klucza profilu.

    Warstwa zapisu wybiera usługę po domenie (select_option / set_value / turn_on…),
    więc klucz w złej domenie oznaczałby zapis wartości złego typu do falownika.
    """
    tou = _HA_TOU_KEY.match(key)
    if tou:
        return _HA_TOU_DOMAIN[tou.group(1)]
    if key == "mode":
        return "select"
    if key == "export_limit_enabled":
        return "switch"
    if key in WRITE_PARAMS:
        return "number"
    return "sensor"


def _ha(v: _V, raw: Any) -> None:
    h = v.obj(raw, "$.ha", ("integrations",))
    if h is None or not isinstance(h.get("integrations"), list):
        if h is not None:
            v.err("$.ha.integrations", "oczekiwano listy")
        return
    for i, integ in enumerate(h["integrations"]):
        p = f"$.ha.integrations[{i}]"
        it = v.obj(integ, p, ("domain", "ems", "status", "entities"))
        if it is None:
            continue
        v.str_(it.get("domain"), f"{p}.domain")
        v.bool_(it.get("ems"), f"{p}.ems")
        v.enum(it.get("status"), f"{p}.status", ("draft", "verified"))
        ents = it.get("entities")
        if not isinstance(ents, dict):
            v.err(f"{p}.entities", "oczekiwano obiektu")
            continue
        for key, e in ents.items():
            ep = f"{p}.entities.{key}"
            if key not in READ_KEYS and key not in WRITE_PARAMS and not _HA_TOU_KEY.match(key):
                v.err(ep, "nieznany klucz encji")
                continue
            s = v.obj(e, ep, ("domain", "unique_id_regex"), ("transform",))
            if s is None:
                continue
            if v.enum(s.get("domain"), f"{ep}.domain", ("sensor", "number", "select", "switch", "time")):
                want = _ha_entity_domain(key)
                if s["domain"] != want:
                    v.err(f"{ep}.domain", f"klucz {key!r} wymaga domeny {want!r}")
            v.regex(s.get("unique_id_regex"), f"{ep}.unique_id_regex")
            if "transform" in s:
                v.enum(s["transform"], f"{ep}.transform", ("invert_percent", "negate"))


def validate_profile(raw: object) -> list[str]:
    v = _V()
    top = v.obj(raw, "$", _TOP_REQ, _TOP_OPT)
    if top is None:
        return v.errors
    sv = top.get("schema_version")
    if isinstance(sv, bool) or sv != 1:
        v.err("$.schema_version", "obsługiwana wyłącznie wersja 1")
    if not isinstance(top.get("id"), str) or not _ID_RE.match(top["id"]):
        v.err("$.id", "oczekiwano kebab-case, np. goodwe-et")
    v.str_(top.get("label"), "$.label")
    v.enum(top.get("status"), "$.status", ("draft", "verified"))
    model = top.get("control_model")
    v.enum(model, "$.control_model", ("mode_setpoint", "time_window"))
    src = top.get("sources")
    if not isinstance(src, list) or not src:
        v.err("$.sources", "oczekiwano niepustej listy")
    else:
        for i, s in enumerate(src):
            o = v.obj(s, f"$.sources[{i}]", ("what", "ref"))
            if o is not None:
                v.str_(o.get("what"), f"$.sources[{i}].what")
                v.str_(o.get("ref"), f"$.sources[{i}].ref")
    v.int_(top.get("unit_id"), "$.unit_id", 0, 255)
    tr = top.get("transports")
    if not isinstance(tr, list) or not tr:
        v.err("$.transports", "oczekiwano niepustej listy")
    else:
        for i, t in enumerate(tr):
            v.enum(t, f"$.transports[{i}]", TRANSPORTS)

    ident = v.obj(top.get("identify"), "$.identify", ("model_register", "model_regex"), ("registers",))
    ident_regs: dict = {}
    if ident is not None:
        _reg(v, ident.get("model_register"), "$.identify.model_register")
        if isinstance(ident.get("model_register"), dict) and ident["model_register"].get("type") != "ascii":
            v.err("$.identify.model_register.type", "wymagane ascii")
        mr = ident.get("model_regex")
        if not isinstance(mr, list) or not mr:
            v.err("$.identify.model_regex", "oczekiwano niepustej listy")
        else:
            for i, r in enumerate(mr):
                v.regex(r, f"$.identify.model_regex[{i}]")
        regs = ident.get("registers", {})
        if not isinstance(regs, dict):
            v.err("$.identify.registers", "oczekiwano obiektu")
        else:
            ident_regs = regs
            for name, spec in ident_regs.items():
                _reg(v, spec, f"$.identify.registers.{name}")

    _read(v, top.get("read"))

    modes: dict = {}
    tou = None
    if model == "mode_setpoint":
        for k in ("modes", "neutral_mode"):
            if k not in top:
                v.err(f"$.{k}", "wymagane dla mode_setpoint")
        modes = top.get("modes") if isinstance(top.get("modes"), dict) else {}
        values = set()
        for name, m in modes.items():
            mp = f"$.modes.{name}"
            o = v.obj(m, mp, ("value", "direction", "ha_option"))
            if o is None:
                continue
            if v.int_(o.get("value"), f"{mp}.value", 0, 65535):
                if o["value"] in values:
                    v.err(f"{mp}.value", "wartość trybu powtórzona")
                values.add(o["value"])
            v.enum(o.get("direction"), f"{mp}.direction", ("charge", "discharge", "idle", "neutral"))
            v.str_(o.get("ha_option"), f"{mp}.ha_option")
        if "neutral_mode" in top:
            nm = top["neutral_mode"]
            if not isinstance(nm, str) or nm not in modes:
                v.err("$.neutral_mode", "musi wskazywać tryb z modes")
        if "tou" in top:
            v.err("$.tou", "tylko dla time_window")
    elif model == "time_window":
        tou = v.obj(top.get("tou"), "$.tou",
                    ("programs", "time_step_min", "soc_tolerance_pp", "power_tolerance_w", "field_order"))
        if tou is not None:
            v.int_(tou.get("programs"), "$.tou.programs", 1, 12)
            if v.int_(tou.get("time_step_min"), "$.tou.time_step_min", 1, 60) and 60 % tou["time_step_min"]:
                v.err("$.tou.time_step_min", "musi dzielić 60")
            v.num(tou.get("soc_tolerance_pp"), "$.tou.soc_tolerance_pp")
            v.num(tou.get("power_tolerance_w"), "$.tou.power_tolerance_w")
            fo = v.str_list(tou.get("field_order"), "$.tou.field_order")
            if fo is not None and sorted(fo) != sorted(TOU_FIELDS):
                v.err("$.tou.field_order", f"permutacja {list(TOU_FIELDS)}")
        if "modes" in top:
            v.err("$.modes", "tylko dla mode_setpoint")

    written = _write(v, top.get("write"), model, tou)
    _intents(v, top.get("intents"), model, modes)

    base = top.get("baseline")
    if model == "mode_setpoint":
        # Flaga eksportu w stanie bazowym jest opcjonalna: jej brak znaczy „ogranicznik
        # eksportu zostaje, jak był" (np. limit narzucony przez operatora sieci).
        b = v.obj(base, "$.baseline", ("mode",), ("export_limit_enabled",))
        if b is not None:
            bm = b.get("mode")
            if not isinstance(bm, str) or bm not in modes:
                v.err("$.baseline.mode", "musi wskazywać tryb z modes")
            if "export_limit_enabled" in b:
                v.bool_(b["export_limit_enabled"], "$.baseline.export_limit_enabled")
    elif model == "time_window":
        b = v.obj(base, "$.baseline", ("intent",))
        if b is not None:
            v.enum(b.get("intent"), "$.baseline.intent", ("self_consume",))

    wp = v.obj(top.get("write_policy"), "$.write_policy",
               ("order", "min_interval_s", "nvm", "max_direction_changes_per_hour", "max_state_age_s"))
    if wp is not None:
        order = v.str_list(wp.get("order"), "$.write_policy.order")
        if order is not None:
            if sorted(order) != sorted(written) or len(set(order)) != len(order):
                v.err("$.write_policy.order", f"musi zawierać dokładnie raz każdy zapis: {sorted(written)}")
            elif "mode" in order and order[-1] != "mode":
                v.err("$.write_policy.order", "tryb musi być zapisywany OSTATNI")
        v.num(wp.get("min_interval_s"), "$.write_policy.min_interval_s")
        v.bool_(wp.get("nvm"), "$.write_policy.nvm")
        # Dolna granica 1 obowiązuje tylko mode_setpoint: DirectionLimiter blokuje
        # KAŻDĄ zmianę kierunku przy wartości 0, więc dla time_window (który go
        # nie używa) pole jest wyłącznie zapisywane, nie egzekwowane.
        min_changes = 1 if model == "mode_setpoint" else 0
        v.int_(wp.get("max_direction_changes_per_hour"),
               "$.write_policy.max_direction_changes_per_hour", min_changes, 60)
        v.num(wp.get("max_state_age_s"), "$.write_policy.max_state_age_s")

    lim = v.obj(top.get("limits"), "$.limits", ("battery_temp_c", "rated_power_register"))
    if lim is not None:
        t = v.obj(lim.get("battery_temp_c"), "$.limits.battery_temp_c", ("min", "max"))
        if t is not None and v.num(t.get("min"), "$.limits.battery_temp_c.min") \
                and v.num(t.get("max"), "$.limits.battery_temp_c.max") and t["min"] >= t["max"]:
            v.err("$.limits.battery_temp_c", "min musi być mniejsze od max")
        rpr = lim.get("rated_power_register")
        if not isinstance(rpr, str) or rpr not in ident_regs:
            v.err("$.limits.rated_power_register", "musi wskazywać identify.registers")

    _ha(v, top.get("ha"))

    caps = v.obj(top.get("capabilities"), "$.capabilities", CAPABILITY_KEYS + ("time_windows",))
    if caps is not None:
        for k in CAPABILITY_KEYS:
            if k in caps:
                v.bool_(caps[k], f"$.capabilities.{k}")
        tw = caps.get("time_windows")
        expected = tou.get("programs") if (model == "time_window" and tou) else 0
        if tw != expected:
            v.err("$.capabilities.time_windows", f"oczekiwano {expected}")
    return v.errors
