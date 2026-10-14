"""Bezpieczeństwo wzorców `*_regex` profilu — jedna reguła dla walidatora profilu i ładowarki feedu.

Wzorce profilu działają w pętli zdarzeń HA, także te z feedu: profil przyjęty przez bramy profili
nie może odrzucić feedu, a wzorzec przyjęty tutaj nie może zawiesić HA nawrotami.

Reguła = OGRANICZONY PODZBIÓR składni + limity dobrane pomiarem, tak by każdy przyjęty wzorzec kończył
`re.search` w ≤ 0,1 s na wrogim tekście długości INPUT_CAP (dłuższy tekst nie pasuje — `safe_search`).
Wierna kopia `analyze` z bram Volcast (`scripts/profiles/regex-safety.mjs`; stałe w
`profiles/schema-lite.json` `x-regex-safety`; wspólne przypadki z kodami w `profiles/fixtures/
regex-safety.json`, kopia w `tests/fixtures/`):
  1. Tylko drukowalne ASCII 0x20–0x7E, długość ≤ MAX_REGEX_LEN.
  2. Dozwolone: literały; `\\d \\D \\w \\W \\s \\S` i `\\` + znak interpunkcji ASCII; klasy `[...]` (bez `[`
     w środku, bez `]` na początku, bez `--`/`&&`/`||`/`~~`, zakres tylko między dwoma literałami,
     rosnąco); `.`; kotwice `^ $ \\b`; grupy `(...)`, `(?:...)`; alternatywa `|`.
  3. Kwantyfikatory `* + ? {n} {n,m}` (n ≤ m ≤ MAX_FIXED_REPEAT) tylko bezpośrednio po atomie; po
     grupie tylko `?`; leniwe, zaborcze, `{n,}`, `{,m}`, kwantyfikator po kotwicy/`(`/`|`/początku/
     innym kwantyfikatorze, literał `{ } ]` bez `\\` = odrzucenie.
  4. `(?i)` tylko na początku; lookbehind `(?<=…)`/`(?<!…)` z 1..MAX_LOOKBEHIND atomami szerokości 1,
     bez kwantyfikatora; reszta po `(?`, odwołania wsteczne, `\\N \\A \\Z \\x \\u` = odrzucenie.
  5. Limity: szeroki kwantyfikator (max − min + 1 > MAX_NARROW_WIDTH) ≤ MAX_WIDE_PER_PATH na ścieżce
     (sekwencja = suma, alternatywa = maksimum); wybory (gałęzie × 2 za grupę opcjonalną × szerokość
     wąskich kwantyfikatorów) ≤ MAX_CHOICES; praca stała = Σ(max − 1) wąskich + Σ szerokości lookbehind;
     koszt = INPUT_CAP^szerokie × wybory × (1 + praca stała) ≤ MAX_COST; zagnieżdżenie ≤ MAX_GROUP_DEPTH.
Na końcu wzorzec musi się skompilować (każdy wyjątek `re` = `compile`).
"""
from __future__ import annotations

import re
import warnings
from typing import Any, Iterator, NamedTuple

REGEX_KEY_SUFFIX = "_regex"
MAX_REGEX_LEN = 200
INPUT_CAP = 128
MAX_WIDE_PER_PATH = 2
MAX_NARROW_WIDTH = 4
MAX_CHOICES = 16
MAX_FIXED_REPEAT = 16
MAX_COST = INPUT_CAP * INPUT_CAP * 24
MAX_GROUP_DEPTH = 4
MAX_LOOKBEHIND = 16

# Kod → wskazówka dla autora profilu (te same teksty co w Volcast).
REGEX_HINTS = {
    "not_string": "a *_regex value must be a string (or a list of strings)",
    "length": f"keep the pattern at most {MAX_REGEX_LEN} characters",
    "non_ascii": "use printable ASCII only (0x20-0x7E); write other characters as a class of ASCII or drop them",
    "forbidden_construct":
        "only literals, \\d \\w \\s (and upper-case), escaped punctuation, [classes], ., ^ $ \\b, (groups), "
        "(?:groups) and | are allowed; no lookahead, named groups, backreferences, comments or inline flags "
        "((?i) only at the very start; (?<=x)/(?<!x) only with plain single characters inside)",
    "quantifier_after_group":
        "a quantifier after a group is not allowed (only ? for an optional group); use a character class "
        "like [ab]+ instead of (a|b)+",
    "quantifier_position":
        "a quantifier must follow a single literal, escape, class or . (not an anchor or another quantifier)",
    "lazy_quantifier": "lazy (*? +? ??) and possessive (*+ ++) quantifiers are not allowed; use the plain quantifier",
    "repeat_too_large":
        f"use {{n}} or {{n,m}} with n <= m <= {MAX_FIXED_REPEAT}; {{n,}} and {{,m}} are not allowed (use * or +)",
    "too_many_quantifiers":
        f"at most {MAX_WIDE_PER_PATH} open-ended quantifiers (* + or {{n,m}} with m-n >= {MAX_NARROW_WIDTH}) on "
        "one match path; prefer a fixed prefix like ^goodwe-battery_soc-",
    "too_many_choices":
        "choices multiply: | branches, optional groups (x)?, x? and small {n,m} ranges together at most "
        f"{MAX_CHOICES} combinations",
    "too_costly":
        "too much backtracking work: with two open-ended quantifiers keep alternatives, x? and {n} repeats / "
        "lookbehinds short; anchor with ^ and use a fixed literal prefix",
    "too_deep": f"nest groups at most {MAX_GROUP_DEPTH} deep",
    "compile": "the pattern does not compile (unbalanced (), [] or an invalid range like [z-a])",
}

_META = frozenset("\\^$.|?*+()[]{}")
_CLASS_ESCAPES = frozenset("dDwWsS")
_BRACE = re.compile(r"\{([0-9]*)(,?)([0-9]*)\}")


class _Reject(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _is_punct(c: str) -> bool:
    return len(c) == 1 and " " <= c <= "~" and not c.isalnum()


def _at(rx: str, i: int) -> str:
    """Znak w `i` albo "" (jak `undefined` w JS)."""
    return rx[i] if 0 <= i < len(rx) else ""


def _parse_class(rx: str, i: int) -> int:
    """Klasa od `[` w `i`; indeks za `]`. Python i JS czytają ją tak samo dzięki zakazom z pkt 2."""
    j = i + 1
    if _at(rx, j) == "^":
        j += 1
    if _at(rx, j) == "]":
        raise _Reject("forbidden_construct")
    items: list[tuple[str | None, str | None]] = []   # (literał | None = kategoria, surowy znak | None)
    while j < len(rx) and rx[j] != "]":
        c = rx[j]
        if c == "[":
            raise _Reject("forbidden_construct")
        if c == "\\":
            n = _at(rx, j + 1)
            if n == "":
                raise _Reject("compile")
            if n in _CLASS_ESCAPES:
                items.append((None, None))
            elif _is_punct(n):
                items.append((n, None))
            else:
                raise _Reject("forbidden_construct")
            j += 2
        else:
            if c in "-&|~" and _at(rx, j + 1) == c:
                raise _Reject("forbidden_construct")   # możliwe operacje na zbiorach
            items.append((c, c))
            j += 1
    if j >= len(rx):
        raise _Reject("compile")
    k = 1
    while k < len(items) - 1:   # `-` między dwoma pozycjami łączy literały rosnąco
        if items[k][1] == "-":
            lo, hi = items[k - 1][0], items[k + 1][0]
            if lo is None or hi is None:
                raise _Reject("forbidden_construct")
            if lo > hi:
                raise _Reject("compile")
            k += 1
        k += 1
    return j + 1


def _quantifier_at(rx: str, i: int) -> tuple[int, float, float] | None:
    """Kwantyfikator w `i`: (długość, min, max) albo None. `{` bez poprawnej postaci = odrzucenie.
    Liczby jak `Number()` w JS (float), żeby ogromne powtórzenia dawały ten sam kod."""
    c = _at(rx, i)
    if c == "*":
        return 1, 0, float("inf")
    if c == "+":
        return 1, 1, float("inf")
    if c == "?":
        return 1, 0, 1
    if c != "{":
        return None
    m = _BRACE.match(rx, i)
    if not m:
        raise _Reject("forbidden_construct")   # literał `{` trzeba poprzedzić `\`
    if m.group(1) == "" or (m.group(2) and m.group(3) == ""):
        raise _Reject("repeat_too_large")       # `{,m}`, `{n,}`, `{}`
    lo = float(m.group(1))
    hi = float(m.group(3)) if m.group(2) else lo
    if lo > hi:
        raise _Reject("compile")
    if hi > MAX_FIXED_REPEAT:
        raise _Reject("repeat_too_large")
    return len(m.group(0)), lo, hi


class _Frame:
    """Grupa (dno = cały wzorzec): gałęzie alternatywy; cur/best = szerokie kwantyfikatory w bieżącej
    gałęzi / najwięcej w gałęziach zamkniętych."""
    __slots__ = ("branches", "cur", "best")

    def __init__(self) -> None:
        self.branches = 1
        self.cur = 0
        self.best = 0


def _analyze(rx: Any) -> None:
    """Port `analyze` z Volcast; odrzucenie = `_Reject(kod)`. Bez kroku kompilacji (ten robi
    `regex_problem` w Pythonie)."""
    if not isinstance(rx, str):
        raise _Reject("not_string")
    # Długość jak `length` w JS (jednostki UTF-16), żeby kod był ten sam także dla znaków spoza BMP.
    if len(rx.encode("utf-16-le", "surrogatepass")) // 2 > MAX_REGEX_LEN:
        raise _Reject("length")
    if any(not " " <= ch <= "~" for ch in rx):
        raise _Reject("non_ascii")
    i = 4 if rx.startswith("(?i)") else 0
    stack = [_Frame()]
    choices = 1
    fixed = 0
    prev = "start"   # atom | group | anchor | quant | start

    def choose(k: int) -> None:
        nonlocal choices
        choices *= k
        if choices > MAX_CHOICES:
            raise _Reject("too_many_choices")

    while i < len(rx):
        c = rx[i]
        q = _quantifier_at(rx, i)
        if q:
            qlen, lo, hi = q
            if prev == "quant":
                raise _Reject("lazy_quantifier" if c in "?+" else "quantifier_position")
            if prev == "group":
                if c != "?":
                    raise _Reject("quantifier_after_group")
                choose(2)
            elif prev == "atom":
                width = hi - lo + 1
                if width > MAX_NARROW_WIDTH:
                    stack[-1].cur += 1
                else:
                    if width > 1:
                        choose(int(width))
                    fixed += int(hi) - 1
            else:
                raise _Reject("quantifier_position")
            i += qlen
            prev = "quant"
            continue
        if c == "\\":
            n = _at(rx, i + 1)
            if n == "":
                raise _Reject("compile")
            if n == "b":
                prev = "anchor"
            elif n in _CLASS_ESCAPES or _is_punct(n):
                prev = "atom"
            else:
                raise _Reject("forbidden_construct")   # \1 \N \A \Z \x \u …
            i += 2
        elif c == "[":
            i = _parse_class(rx, i)
            prev = "atom"
        elif c == ".":
            i += 1
            prev = "atom"
        elif c in "^$":
            i += 1
            prev = "anchor"
        elif c == "(":
            if rx.startswith("(?<=", i) or rx.startswith("(?<!", i):
                j = i + 4
                width = 0
                while j < len(rx) and rx[j] != ")":
                    if rx[j] == "\\":
                        n = _at(rx, j + 1)
                        if n == "" or not (n in _CLASS_ESCAPES or _is_punct(n)):
                            raise _Reject("forbidden_construct")
                        j += 2
                    elif rx[j] == "[":
                        j = _parse_class(rx, j)
                    elif rx[j] in _META and rx[j] != ".":
                        raise _Reject("forbidden_construct")
                    else:
                        j += 1
                    width += 1
                if j >= len(rx):
                    raise _Reject("compile")
                if width == 0 or width > MAX_LOOKBEHIND:
                    raise _Reject("forbidden_construct")
                fixed += width
                i = j + 1
                prev = "anchor"   # zerowej szerokości, nie wolno go kwantyfikować
                continue
            length = 1
            if _at(rx, i + 1) == "?":
                if _at(rx, i + 2) != ":":
                    raise _Reject("forbidden_construct")
                length = 3
            if len(stack) > MAX_GROUP_DEPTH:
                raise _Reject("too_deep")
            stack.append(_Frame())
            i += length
            prev = "start"
        elif c == ")":
            if len(stack) == 1:
                raise _Reject("compile")
            f = stack.pop()
            if prev == "start" and f.branches == 1:
                raise _Reject("forbidden_construct")   # pusta grupa
            stack[-1].cur += max(f.best, f.cur)
            i += 1
            prev = "group"
        elif c == "|":
            top = stack[-1]
            top.best = max(top.best, top.cur)
            top.cur = 0
            top.branches += 1
            choices = choices // (top.branches - 1) * top.branches
            if choices > MAX_CHOICES:
                raise _Reject("too_many_choices")
            i += 1
            prev = "start"
        elif c in "]}":
            raise _Reject("forbidden_construct")   # literał trzeba poprzedzić `\`
        else:
            i += 1
            prev = "atom"
    if len(stack) != 1:
        raise _Reject("compile")
    wide = max(stack[0].best, stack[0].cur)
    if wide > MAX_WIDE_PER_PATH:
        raise _Reject("too_many_quantifiers")
    if INPUT_CAP ** wide * choices * (1 + fixed) > MAX_COST:
        raise _Reject("too_costly")


def text_problem(rx: Any) -> str | None:
    """Kod odrzucenia wzorca przez regułę tekstową (bez kompilacji) albo None."""
    try:
        _analyze(rx)
    except _Reject as rej:
        return rej.code
    return None


# --- druga zapora: drzewo prywatnego parsera `re` (tylko feed i test generyczny) ----------------------
# Sprawdza, że to, co `re` NAPRAWDĘ wykona, mieści się w podzbiorze z reguły tekstowej (bez limitów
# kosztu — te liczy reguła tekstowa): tylko atomy, kotwice ^ $ \b, grupy bez flag, alternatywa,
# lookbehind z atomów, zachłanny kwantyfikator na atomie (max ≤ MAX_FIXED_REPEAT albo bez górnej
# granicy) albo `?` na grupie; flagi globalne tylko `i`. Parser to prywatne API (`re._parser`, dawniej
# `sre_parse`): brak modułu, nieznany węzeł albo nieoczekiwany kształt = odrzucenie (fail closed).
# Profili z pakietu nie sprawdza (ich ładowanie nie zależy od prywatnego API).
_ALLOWED_FLAGS = int(re.IGNORECASE | re.UNICODE)


class _Sre(NamedTuple):
    parse: Any
    maxrepeat: int
    atoms: frozenset        # LITERAL, NOT_LITERAL, ANY, IN
    at: Any
    anchors: frozenset      # AT_BEGINNING, AT_END, AT_BOUNDARY
    max_repeat: Any
    lazy: frozenset         # MIN_REPEAT, POSSESSIVE_REPEAT
    subpattern: Any
    branch: Any
    asserts: frozenset      # ASSERT, ASSERT_NOT


def _load_sre() -> _Sre | None:
    try:
        from re import _constants as c, _parser as parser
    except ImportError:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                import sre_constants as c
                import sre_parse as parser
        except ImportError:
            return None
    try:
        return _Sre(
            parse=parser.parse, maxrepeat=int(c.MAXREPEAT),
            atoms=frozenset((c.LITERAL, c.NOT_LITERAL, c.ANY, c.IN)), at=c.AT,
            anchors=frozenset((c.AT_BEGINNING, c.AT_END, c.AT_BOUNDARY)), max_repeat=c.MAX_REPEAT,
            lazy=frozenset(getattr(c, n) for n in ("MIN_REPEAT", "POSSESSIVE_REPEAT") if hasattr(c, n)),
            subpattern=c.SUBPATTERN, branch=c.BRANCH, asserts=frozenset((c.ASSERT, c.ASSERT_NOT)))
    except AttributeError:
        return None


_SRE = _load_sre()


def _walk(nodes: Any, sre: _Sre) -> None:
    for op, av in nodes:
        if op in sre.atoms:
            continue
        if op is sre.at:
            if av not in sre.anchors:
                raise _Reject("forbidden_construct")
        elif op is sre.max_repeat:
            lo, hi, item = av
            if hi != sre.maxrepeat and hi > MAX_FIXED_REPEAT:
                raise _Reject("repeat_too_large")
            single_atom = len(item) == 1 and item[0][0] in sre.atoms
            if not single_atom and not (lo == 0 and hi == 1):
                raise _Reject("quantifier_after_group")
            _walk(item, sre)
        elif op in sre.lazy:
            raise _Reject("lazy_quantifier")
        elif op is sre.subpattern:
            _group, add_flags, del_flags, p = av
            if add_flags or del_flags:
                raise _Reject("forbidden_construct")
            _walk(p, sre)
        elif op is sre.branch:
            for b in av[1]:
                _walk(b, sre)
        elif op in sre.asserts:
            direction, p = av
            if direction != -1 or not all(o in sre.atoms for o, _ in p):
                raise _Reject("forbidden_construct")
        else:   # odwołania, warunki, grupy atomowe i każdy węzeł nieznany tej wersji reguły
            raise _Reject("forbidden_construct")


def tree_problem(rx: str) -> str | None:
    """Druga zapora na drzewie parsera `re` (wzorzec już przeszedł regułę tekstową i się kompiluje)."""
    sre = _SRE
    if sre is None:
        return "compile"
    try:
        tree = sre.parse(rx, 0)
        if int(tree.state.flags) & ~_ALLOWED_FLAGS:
            return "forbidden_construct"
        _walk(tree, sre)
    except _Reject as rej:
        return rej.code
    except Exception:  # noqa: BLE001 — każdy błąd parsera (także OverflowError, MemoryError) = odrzucenie
        return "compile"
    return None


def regex_problem(rx: Any, *, parse_tree: bool = True) -> str | None:
    """Kod odrzucenia wzorca (kody i wskazówki: `REGEX_HINTS`) albo None, gdy wzorzec jest bezpieczny:
    reguła tekstowa (kopia Volcast), kompilacja w Pythonie (każdy wyjątek = `compile`) i — przy
    `parse_tree` — druga zapora na drzewie prywatnego parsera `re` (fail closed). Profile z pakietu
    sprawdza się bez drzewa (`parse_tree=False`); feed i test generyczny — z drzewem."""
    code = text_problem(rx)
    if code:
        return code
    try:
        re.compile(rx)
    except Exception:  # noqa: BLE001 — nie tylko re.error: np. ogromne powtórzenie → OverflowError
        return "compile"
    return tree_problem(rx) if parse_tree else None


# --- dopasowanie w czasie działania ---------------------------------------------------------------
# Każdy wzorzec z danych profilu (pakiet albo feed) trafia do `re` WYŁĄCZNIE tędy (pilnuje test AST).
# Tekst dłuższy niż INPUT_CAP znaków = BRAK dopasowania (fail closed), nigdy dopasowanie do jego
# początku: czas nawrotów wzorca przyjętego przez regułę rośnie z długością tekstu, a przycięcie
# zmieniłoby znaczenie dopasowania (urządzenie uznane za inne). Modele i unique_id encji HA są
# wielokrotnie krótsze (test na danych wzorcowych). `re` trzyma skompilowane wzorce w swojej pamięci.
def fits(text: str) -> bool:
    """Czy tekst urządzenia w ogóle może się dopasować (nie przekracza INPUT_CAP)."""
    return len(text) <= INPUT_CAP


def safe_search(pattern: str | re.Pattern[str], text: str) -> re.Match[str] | None:
    return re.compile(pattern).search(text) if fits(text) else None


def safe_match(pattern: str | re.Pattern[str], text: str) -> re.Match[str] | None:
    return re.compile(pattern).match(text) if fits(text) else None


def iter_regex_fields(node: Any, path: str = "$") -> Iterator[tuple[str, str]]:
    """(ścieżka, wzorzec) każdego pola `*_regex` — napis albo lista napisów — na każdej głębokości.
    Po nazwie, nie po liście pól: nowe pole wzorca w schemacie jest objęte regułą od razu.
    Wartości innego typu zgłasza samo pole schematu (`_V.regex`, `str_list`)."""
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{path}.{k}"
            if isinstance(k, str) and k.endswith(REGEX_KEY_SUFFIX):
                if isinstance(v, str):
                    yield p, v
                elif isinstance(v, list):
                    yield from ((f"{p}[{i}]", x) for i, x in enumerate(v) if isinstance(x, str))
            else:
                yield from iter_regex_fields(v, p)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from iter_regex_fields(v, f"{path}[{i}]")
