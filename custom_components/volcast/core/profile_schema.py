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
# Rodzaje mocy intencji trybu mode_setpoint. Oba rodzaje "ze slotu" niosą moc slotu;
# `slot_live_export` dodatkowo mówi wykonawcy, że moc slotu to moc po stronie baterii
# i przeliczy ją na nastawę eksportu z bieżących odczytów.
SLOT_POWER_KINDS = ("slot", "slot_live_export")
CAPABILITY_KEYS = ("force_charge_from_grid", "sell_from_battery", "force_discharge", "standby",
                   "set_power_w", "limit_export", "set_soc_floor", "set_soc_ceiling")
REG_TYPES = ("u16", "i16", "u32", "i32", "f32", "ascii")
ENCODE_FOR = {"mode": "mode", "power_w": "watts", "soc_min": "percent", "soc_max": "percent",
              "export_limit_w": "watts", "export_limit_enabled": "bool"}
TOU_ENCODE = {"start": "hhmm", "power_w": "watts", "soc": "percent"}
TRANSPORTS = ("goodwe_udp", "solarman_v5", "modbus_tcp", "modbus_rtu")
MAX_READBACK_SETTLE_S = 10
_ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_HA_TOU_KEY = re.compile(r"^tou_[1-9]_(start|power_w|soc|grid_charge)$")
# Odczyt włącznika harmonogramu TOU — tylko dostęp bezpośredni, nie encja HA.
TOU_ENABLED_READ = "tou_enabled"
WRITE_FUNCTIONS = (6, 16)
# Kod funkcji odczytu: 3 = rejestry holding (domyślny), 4 = rejestry input (tylko odczyt).
READ_FUNCTIONS = (3, 4)
MAX_READ_REGISTERS = 125

_TOP_REQ = ("schema_version", "id", "label", "status", "control_model", "sources", "unit_id",
            "transports", "identify", "read", "write", "intents", "baseline", "write_policy",
            "limits", "ha", "capabilities", "modbus")
_TOP_OPT = ("modes", "neutral_mode", "tou", "status_note", "verification")
# Nadpisania parametrów drabiny weryfikacji urządzenia (próbne godziny, okno próbne): pole → zakres.
VERIFICATION_RANGES = {"trial_hours": (1, 72), "window_minutes": (5, 60), "window_power_w": (100, 3000)}


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
              ("type", "scale", "sign", "undef", "len", "word_order", "expect", "fc"))
    if r is None:
        return
    v.int_(r.get("addr"), f"{path}.addr", 0, 65535)
    if "fc" in r:
        _read_function(v, r["fc"], f"{path}.fc")
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
    if "expect" in r:
        # Wykrywanie po wartości numerycznej (np. typ urządzenia) zamiast po napisie ASCII —
        # dla marek bez rejestru modelu w ASCII (patrz walidacja `$.identify` niżej).
        exp = r["expect"]
        if not isinstance(exp, list) or not exp or not all(
                isinstance(x, int) and not isinstance(x, bool) for x in exp):
            v.err(f"{path}.expect", "oczekiwano niepustej listy liczb całkowitych")


def _read_function(v: _V, fc: Any, path: str) -> None:
    if isinstance(fc, bool) or not isinstance(fc, int) or fc not in READ_FUNCTIONS:
        v.err(path, "tylko 3 (holding) albo 4 (input)")


def _read(v: _V, raw: Any) -> None:
    if not isinstance(raw, dict):
        v.err("$.read", "oczekiwano obiektu")
        return
    edges: dict[str, list[str]] = {}
    for key, spec in raw.items():
        path = f"$.read.{key}"
        if key not in READ_KEYS and key != TOU_ENABLED_READ:
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
                    else:
                        edges.setdefault(key, []).append(ref)
                    if "sign" in t and t["sign"] not in (-1, 1):
                        v.err(f"{tp}.sign", "tylko -1 albo 1")
                else:
                    _reg(v, t, tp)
        else:
            _reg(v, spec, path)
    _ref_cycles(v, edges)


def _ref_cycles(v: _V, edges: dict[str, list[str]]) -> None:
    """Cykl odwołań (A→B→A) przeszedłby walidację, a odczyt padałby przy każdym cyklu."""
    state: dict[str, int] = {}          # 1 = na stosie, 2 = przetworzony
    stack: list[str] = []

    def visit(node: str) -> None:
        state[node] = 1
        stack.append(node)
        for nxt in edges.get(node, ()):
            if state.get(nxt) == 1:
                cycle = stack[stack.index(nxt):] + [nxt]
                v.err(f"$.read.{nxt}.sum", "cykl odwołań: " + " → ".join(cycle))
            elif nxt not in state:
                visit(nxt)
        stack.pop()
        state[node] = 2

    for key in edges:
        if key not in state:
            visit(key)


def _write(v: _V, raw: Any, model: str, tou: dict | None) -> set[str]:
    w = v.obj(raw, "$.write", (), WRITE_PARAMS + ("tou_program", "tou_enable"))
    if w is None:
        return set()
    if "tou_enable" in w:
        _tou_enable(v, w)
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
    # Włącznik jest częścią sekwencji TOU (wyłączenie na początku, włączenie na końcu),
    # nie osobnym kluczem kolejności zapisu.
    return {("tou" if k == "tou_program" else k) for k in w if k != "tou_enable"}


def _tou_enable(v: _V, w: dict) -> None:
    path = "$.write.tou_enable"
    if "tou_program" not in w:
        v.err(path, "dozwolone tylko razem z tou_program")
    e = v.obj(w["tou_enable"], path, ("addr", "enable_bit", "day_mask"))
    if e is None:
        return
    v.int_(e.get("addr"), f"{path}.addr", 0, 65535)
    bit_ok = v.int_(e.get("enable_bit"), f"{path}.enable_bit", 0, 15)
    if v.int_(e.get("day_mask"), f"{path}.day_mask", 0, 65535) and bit_ok \
            and e["day_mask"] & (1 << e["enable_bit"]):
        v.err(f"{path}.day_mask", "maska dni nie może zawierać bitu włącznika")


def _write_addresses(wr: Any) -> set[int]:
    """Adresy rejestrów zapisu (z programami harmonogramu); pola złego typu pomijane (zgłasza je `_write`)."""
    out: set[int] = set()
    if not isinstance(wr, dict):
        return out
    for key, spec in wr.items():
        if not isinstance(spec, dict):
            continue
        if key == "tou_program":
            count = spec.get("count")
            for field in spec.values():
                if isinstance(field, dict) and isinstance(field.get("addr"), int) and isinstance(count, int):
                    out.update(range(field["addr"], field["addr"] + count))
        elif isinstance(spec.get("addr"), int):
            out.add(spec["addr"])
    return out


def _modbus(v: _V, raw: Any, transports: Any, written: set[str], write_addrs: set[int]) -> None:
    """Parametry dostępu bezpośredniego: status ścieżki rejestrów, funkcja zapisu, łącza."""
    m = v.obj(raw, "$.modbus", ("status", "write_function", "max_read_registers", "transport_options",
                                "identify_reads", "probe_keys"), ("status_note", "echo_only", "verify_blocks"))
    if m is None:
        return
    v.enum(m.get("status"), "$.modbus.status", ("draft", "verified"))
    if "status_note" in m:
        v.str_(m["status_note"], "$.modbus.status_note")
    wf = m.get("write_function")
    if isinstance(wf, bool) or wf not in WRITE_FUNCTIONS:
        v.err("$.modbus.write_function", f"{wf!r} spoza {list(WRITE_FUNCTIONS)}")
    v.int_(m.get("max_read_registers"), "$.modbus.max_read_registers", 1, MAX_READ_REGISTERS)
    known = set(transports) if isinstance(transports, list) else set()
    opts = m.get("transport_options")
    if not isinstance(opts, dict):
        v.err("$.modbus.transport_options", "oczekiwano obiektu")
    else:
        for name, o in opts.items():
            tp = f"$.modbus.transport_options.{name}"
            if name not in known:
                v.err(tp, "transport spoza $.transports")
                continue
            t = v.obj(o, tp, ("port", "timeout_ms", "gap_ms"))
            if t is not None:
                v.int_(t.get("port"), f"{tp}.port", 1, 65535)
                v.int_(t.get("timeout_ms"), f"{tp}.timeout_ms", 200, 10000)
                v.int_(t.get("gap_ms"), f"{tp}.gap_ms", 0, 2000)
    reads = m.get("identify_reads")
    if not isinstance(reads, list) or not reads:
        v.err("$.modbus.identify_reads", "oczekiwano niepustej listy")
    else:
        for i, r in enumerate(reads):
            rp = f"$.modbus.identify_reads[{i}]"
            o = v.obj(r, rp, ("addr", "count"), ("fc",))
            if o is None:
                continue
            if "fc" in o:
                _read_function(v, o["fc"], f"{rp}.fc")
            a_ok = v.int_(o.get("addr"), f"{rp}.addr", 0, 65535)
            c_ok = v.int_(o.get("count"), f"{rp}.count", 1, MAX_READ_REGISTERS)
            if a_ok and c_ok and o["addr"] + o["count"] > 65536:
                v.err(rp, "blok wychodzi poza przestrzeń adresów")
    keys = v.str_list(m.get("probe_keys"), "$.modbus.probe_keys")
    if keys is not None:
        if len(set(keys)) != len(keys):
            v.err("$.modbus.probe_keys", "klucze powtórzone")
        extra = sorted(set(keys) - written)
        if extra:
            v.err("$.modbus.probe_keys", f"klucze spoza zapisów profilu: {extra}")
    # Klucze potwierdzane samym echem (rejestr bez odczytu w tej rodzinie falowników) —
    # jawna zgoda profilu; każdy inny klucz jest pisany wyłącznie z odczytem zwrotnym.
    if "echo_only" in m:
        eo = v.str_list(m["echo_only"], "$.modbus.echo_only")
        if eo is not None:
            if len(set(eo)) != len(eo):
                v.err("$.modbus.echo_only", "klucze powtórzone")
            extra = sorted(set(eo) - written)
            if extra:
                v.err("$.modbus.echo_only", f"klucze spoza zapisów profilu: {extra}")
    if "verify_blocks" in m:
        _verify_blocks(v, m["verify_blocks"], m.get("max_read_registers"), write_addrs)


def _verify_blocks(v: _V, raw: Any, max_count: Any, write_addrs: set[int]) -> None:
    """Bloki odczytu znane jako dozwolone na urządzeniu (czyta je urządzenie referencyjne) — pisarz
    i sonda czytają przez nie rejestr zapisu, żeby kolejne żądania różniły się długością odpowiedzi.
    Każdy blok obejmuje co najmniej jeden rejestr zapisu."""
    path = "$.modbus.verify_blocks"
    if not isinstance(raw, list) or not raw:
        v.err(path, "oczekiwano niepustej listy")
        return
    limit = max_count if isinstance(max_count, int) and not isinstance(max_count, bool) else MAX_READ_REGISTERS
    seen: set[tuple[int, int]] = set()
    for i, b in enumerate(raw):
        o = v.obj(b, f"{path}[{i}]", ("addr", "count"))
        if o is None:
            continue
        a_ok = v.int_(o.get("addr"), f"{path}[{i}].addr", 0, 65535)
        c_ok = v.int_(o.get("count"), f"{path}[{i}].count", 1, limit)
        if not (a_ok and c_ok):
            continue
        block = (o["addr"], o["count"])
        if block[0] + block[1] > 65536:
            v.err(f"{path}[{i}]", "blok wychodzi poza przestrzeń adresów")
        elif block in seen:
            v.err(path, "bloki powtórzone")
        elif not any(block[0] <= a < block[0] + block[1] for a in write_addrs):
            v.err(f"{path}[{i}]", "blok nie obejmuje żadnego rejestru zapisu")
        seen.add(block)


def _nvm_budget(v: _V, raw: Any) -> None:
    path = "$.write_policy.nvm_budget"
    b = v.obj(raw, path, ("window_h", "per_key", "total"))
    if b is None:
        return
    v.int_(b.get("window_h"), f"{path}.window_h", 1, 168)
    if v.int_(b.get("per_key"), f"{path}.per_key", 1, 1_000_000):
        v.int_(b.get("total"), f"{path}.total", b["per_key"], 1_000_000)
    else:
        v.int_(b.get("total"), f"{path}.total", 1, 1_000_000)


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
                if v.enum(power, f"{path}.power", (*SLOT_POWER_KINDS, "zero", "none")):
                    if power in SLOT_POWER_KINDS and name not in POWERED_INTENTS:
                        v.err(f"{path}.power",
                              f"{power!r} dozwolone tylko dla {list(POWERED_INTENTS)}")
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
        it = v.obj(integ, p, ("domain", "ems", "status", "entities"), ("model_regex",))
        if it is None:
            continue
        v.str_(it.get("domain"), f"{p}.domain")
        if "model_regex" in it:
            # Rozstrzyga między profilami tej samej domeny po tekście „producent model” urządzenia HA.
            mr = v.str_list(it["model_regex"], f"{p}.model_regex")
            if mr is not None and not mr:
                v.err(f"{p}.model_regex", "oczekiwano niepustej listy")
            for j, r in enumerate(mr or ()):
                v.regex(r, f"{p}.model_regex[{j}]")
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


def _verification(v: _V, raw: Any) -> None:
    """Opcjonalne nadpisania stałych drabiny; każde pole osobno, pusty blok to błąd pliku."""
    path = "$.verification"
    b = v.obj(raw, path, (), tuple(VERIFICATION_RANGES))
    if b is None:
        return
    if not b:
        v.err(path, "oczekiwano co najmniej jednego pola")
    for key, (lo, hi) in VERIFICATION_RANGES.items():
        if key in b:
            v.int_(b[key], f"{path}.{key}", lo, hi)


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

    # Model po tekście ASCII (`model_register`+`model_regex`) i model po wartości liczbowej
    # (`registers.<klucz>.expect`) to dwa wykluczające się sposoby identyfikacji — część marek
    # (np. Deye) nie ma w ogóle rejestru modelu w ASCII, tylko kod liczbowy typu urządzenia,
    # więc wymuszanie ASCII zmuszałoby do podstawienia pod "model" czegoś innego (np. numeru
    # seryjnego), co nie jest modelem i nie powinno być tak czytane.
    ident = v.obj(top.get("identify"), "$.identify", (), ("model_register", "model_regex", "registers"))
    ident_regs: dict = {}
    if ident is not None:
        regs = ident.get("registers", {})
        if not isinstance(regs, dict):
            v.err("$.identify.registers", "oczekiwano obiektu")
        else:
            ident_regs = regs
            for name, spec in ident_regs.items():
                _reg(v, spec, f"$.identify.registers.{name}")
        has_expect = any(isinstance(s, dict) and "expect" in s for s in ident_regs.values())
        has_model = "model_register" in ident or "model_regex" in ident
        if has_model:
            _reg(v, ident.get("model_register"), "$.identify.model_register")
            if isinstance(ident.get("model_register"), dict) and ident["model_register"].get("type") != "ascii":
                v.err("$.identify.model_register.type", "wymagane ascii")
            mr = ident.get("model_regex")
            if not isinstance(mr, list) or not mr:
                v.err("$.identify.model_regex", "oczekiwano niepustej listy")
            else:
                for i, r in enumerate(mr):
                    v.regex(r, f"$.identify.model_regex[{i}]")
        elif not has_expect:
            v.err("$.identify", "wymagane model_register+model_regex albo registers.<klucz>.expect")

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
            elif isinstance(modes[nm], dict) and modes[nm].get("direction") not in ("neutral", "idle"):
                # I-1 podmienia rozładowanie na tryb neutralny — tryb wymuszający ruch
                # baterii zamieniłby blokadę w rozładowanie na starej nastawie.
                v.err("$.neutral_mode", "tryb neutralny musi mieć kierunek neutral/idle")
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
    rd, wr = top.get("read"), top.get("write")
    if isinstance(rd, dict) and TOU_ENABLED_READ in rd:
        enable = wr.get("tou_enable") if isinstance(wr, dict) else None
        spec = rd[TOU_ENABLED_READ]
        if not isinstance(enable, dict):
            v.err(f"$.read.{TOU_ENABLED_READ}", "dozwolone tylko przy write.tou_enable")
        elif not isinstance(spec, dict) or spec.get("addr") != enable.get("addr"):
            v.err(f"$.read.{TOU_ENABLED_READ}.addr", "musi równać się write.tou_enable.addr")
    if "modbus" in top:                      # brak sekcji zgłasza już `v.obj` na `$`
        _modbus(v, top["modbus"], tr, written, _write_addresses(wr))
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
               ("order", "min_interval_s", "nvm", "max_direction_changes_per_hour", "max_state_age_s"),
               ("nvm_budget", "readback_settle_s"))
    if wp is not None:
        if "nvm_budget" in wp:
            _nvm_budget(v, wp["nvm_budget"])
        # Odczekanie przed ponownym odczytem zwrotnym, gdy pierwszy pokazał wartość sprzed zapisu.
        if "readback_settle_s" in wp and v.num(wp["readback_settle_s"], "$.write_policy.readback_settle_s") \
                and not 0 <= wp["readback_settle_s"] <= MAX_READBACK_SETTLE_S:
            v.err("$.write_policy.readback_settle_s", f"oczekiwano liczby 0..{MAX_READBACK_SETTLE_S}")
        order = v.str_list(wp.get("order"), "$.write_policy.order")
        if order is not None:
            if sorted(order) != sorted(written) or len(set(order)) != len(order):
                v.err("$.write_policy.order", f"musi zawierać dokładnie raz każdy zapis: {sorted(written)}")
            elif "mode" in order and order[-1] != "mode":
                v.err("$.write_policy.order", "tryb musi być zapisywany OSTATNI")
        if v.num(wp.get("min_interval_s"), "$.write_policy.min_interval_s") and wp["min_interval_s"] < 0:
            v.err("$.write_policy.min_interval_s", "oczekiwano liczby >= 0")
        v.bool_(wp.get("nvm"), "$.write_policy.nvm")
        # Dolna granica 1 obowiązuje tylko mode_setpoint: DirectionLimiter blokuje
        # KAŻDĄ zmianę kierunku przy wartości 0, więc dla time_window (który go
        # nie używa) pole jest wyłącznie zapisywane, nie egzekwowane.
        min_changes = 1 if model == "mode_setpoint" else 0
        v.int_(wp.get("max_direction_changes_per_hour"),
               "$.write_policy.max_direction_changes_per_hour", min_changes, 60)
        # 0 albo mniej = każdy odczyt nieświeży (albo wyłączony I-9 w starszym kodzie).
        if v.num(wp.get("max_state_age_s"), "$.write_policy.max_state_age_s") and wp["max_state_age_s"] <= 0:
            v.err("$.write_policy.max_state_age_s", "oczekiwano liczby > 0")

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
    if "verification" in top:
        _verification(v, top["verification"])

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
