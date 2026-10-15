"""Bezpieczeństwo wzorców `*_regex` profilu — jedna reguła dla walidatora profilu i ładowarki feedu.

Wzorce profilu działają w pętli zdarzeń HA, także te z feedu: profil przyjęty przez bramy profili
nie może odrzucić feedu, a wzorzec przyjęty tutaj nie może zawiesić HA nawrotami.

Reguła = OGRANICZONY PODZBIÓR składni + MODEL KOSZTU nawrotów `sre` na tekście długości INPUT_CAP
(dłuższy tekst nie pasuje — `safe_search`). Wierna kopia bram Volcast (`scripts/profiles/
regex-safety.mjs`: `parse`, `firstOf`, `groupOwn`, `costOf`, `anchored`, `identifyIssues`; stałe w
`profiles/schema-lite.json` `x-regex-safety`; wspólne przypadki z kodami w `profiles/fixtures/
regex-safety.json`, kopia w `tests/fixtures/`).

SKŁADNIA: drukowalne ASCII, ≤ MAX_REGEX_LEN; atomy: literał, `\\d \\D \\w \\W \\s \\S`, `\\` + interpunkcja
ASCII, klasa `[...]` (bez `[` w środku, bez `]` na początku, bez `--`/`&&`/`||`/`~~`, zakres tylko między
dwoma literałami, rosnąco; `-` po zakresie = literał), `.`; kotwice `^ $ \\b`; grupy `(...)`, `(?:...)`;
`|`. Kwantyfikatory `* + ? {n} {n,m}` (m ≤ MAX_FIXED_REPEAT) tylko po atomie, po grupie tylko `?`.
`(?i)` tylko na początku; lookbehind z 1..MAX_LOOKBEHIND atomów szerokości 1. Zagnieżdżenie ≤ MAX_GROUP_DEPTH.

MODEL KOSZTU: zbiór znaków atomu = maska 128 znaków ASCII + „inne” (spoza ASCII: `.`, `\\d \\w \\s` i
negacje, klasy zanegowane, pod `(?i)` każda litera) + „koniec” (`$` — także przed `\\n`; koniec wzorca =
koniec tekstu). Atom o zmiennej liczbie powtórzeń: szerokość w = min(max, CAP) − min + 1; rozłączny
z pierwszym zbiorem dalszej części → praca += w, inaczej liście × w. Grupa: gałęzie o rozłącznych
pierwszych zbiorach → max liści, inaczej suma (+1 za pominięcie grupy opcjonalnej). Praca na liść =
Σ długości elementów. Starty = 1, gdy każda gałąź najwyższego poziomu zaczyna się od `^`, inaczej
CAP + 1. Koszt = starty × liście × praca ≤ MAX_COST (`too_costly_unanchored`, gdy `^` by wystarczył).

IDENTYFIKACJA (`identify_issues`, używa `profile_feed._identify_narrow`): `identify.model_regex` = lista
wzorców zaczynających się od `^` (nie `(?i)^`), niepasujących do pustego napisu ani IDENTIFY_PROBES;
`identify.registers.device_type.expect` ≤ MAX_DEVICE_TYPE_EXPECT wartości, bez 0/65535/false.
"""
from __future__ import annotations

import re
import warnings
from typing import Any, Iterator, NamedTuple

REGEX_KEY_SUFFIX = "_regex"
MAX_REGEX_LEN = 200
INPUT_CAP = 128
MAX_FIXED_REPEAT = 16
MAX_GROUP_DEPTH = 4
MAX_LOOKBEHIND = 16
MAX_ASSERTIONS = 8
MAX_COST = 500_000
IDENTIFY_PROBES = ("A", "Q", "z", "0", "7", "-", "QX", "ZZZZZZZZ", "Q0-Q0", "00000000", "xyz123", "NOT-A-MODEL")
MAX_DEVICE_TYPE_EXPECT = 64
BLANK_REGISTER_VALUES = (0, 65535)

# Kod → wskazówka naprawy dla autora profilu (te same teksty co w Volcast).
REGEX_HINTS = {
    "not_string": "a *_regex value must be a string (or a list of strings)",
    "length": f"keep the pattern at most {MAX_REGEX_LEN} characters; split alternatives into separate entries of "
              "the list",
    "non_ascii": "use printable ASCII only (0x20-0x7E); match other characters with . or a class of ASCII characters",
    "forbidden_construct":
        "only literals, \\d \\w \\s (and upper-case), escaped punctuation like \\. \\-, [classes], ., ^ $ \\b, "
        "(groups), (?:groups) and | are allowed; no lookahead, named groups, backreferences, comments or inline "
        "flags; (?i) only at the very start; (?<=x)/(?<!x) only with plain single characters inside; escape "
        "{ } ] as \\{ \\} \\]",
    "quantifier_after_group":
        "only ? may follow a group; write [ab]+ instead of (a|b)+, repeat the group text instead of (ab){2}, "
        "or split the pattern into several entries of the list",
    "quantifier_position":
        "a quantifier must directly follow one literal, escape, class or . (not an anchor, ( or | or another "
        "quantifier)",
    "lazy_quantifier": "lazy (*? +? ??) and possessive (*+ ++) quantifiers are not allowed; use the plain greedy "
                       "quantifier",
    "repeat_too_large":
        f"use {{n}} or {{n,m}} with n <= m <= {MAX_FIXED_REPEAT}; {{n,}} and {{,m}} are not allowed (use + or *)",
    "too_deep": f"nest groups at most {MAX_GROUP_DEPTH} deep",
    "empty_branch":
        "write an optional group as (ab)? instead of an empty alternation branch like (ab|) or (|); every branch "
        "and group must match at least one character",
    "too_many_assertions": f"use at most {MAX_ASSERTIONS} \\b and lookbehind assertions in one pattern",
    "compile": "the pattern does not compile: check unbalanced ( ) or [ ] and ranges like [z-a]",
    "too_costly":
        "too much backtracking: use \\d{1,3} instead of \\d+ where the length is known, follow each + or * with a "
        "character it cannot match (e.g. [0-9]+K, not .+K), avoid two quantifiers over the same characters in a "
        "row, and split | alternatives into separate entries of the list",
    "too_costly_unanchored":
        "anchor the pattern with ^ at the start (unanchored patterns are tried at every position and cost 129x "
        "more)",
    "identify_not_anchored":
        "identify.model_regex must be a list of patterns that each start with ^ (not (?i)^; use [Gg][Ww] for case)",
    "identify_too_broad":
        'identify.model_regex must not match an empty or generic model like "A", "0", "QX" or "NOT-A-MODEL"; '
        "add the brand prefix",
    "identify_expect":
        f"identify.registers.device_type.expect: at most {MAX_DEVICE_TYPE_EXPECT} values and never 0 or 65535 "
        "(blank register)",
}

_META = frozenset("\\^$.|?*+()[]{}")
_CLASS_ESCAPES = frozenset("dDwWsS")
_BRACE = re.compile(r"\{([0-9]*)(,?)([0-9]*)\}")
_INF = float("inf")


class _Reject(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _is_punct(c: str) -> bool:
    return len(c) == 1 and " " <= c <= "~" and not c.isalnum()


def _at(rx: str, i: int) -> str:
    """Znak w `i` albo "" (jak `undefined` w JS)."""
    return rx[i] if 0 <= i < len(rx) else ""


# --- zbiory znaków: (maska 128 bitów ASCII, „inne” spoza ASCII, „koniec” tekstu) ---------------------

class _Set(NamedTuple):
    a: int
    other: bool
    end: bool


_ALL_ASCII = (1 << 128) - 1
_EMPTY = _Set(0, False, False)
_END_ONLY = _Set(0, False, True)               # koniec wzorca = koniec tekstu (jak `re.fullmatch`)
_END_SET = _Set(1 << 10, False, True)          # `$`: koniec albo przed `\n`


def _union(x: _Set, y: _Set) -> _Set:
    return _Set(x.a | y.a, x.other or y.other, x.end or y.end)


def _disjoint(x: _Set, y: _Set) -> bool:
    return not (x.a & y.a) and not (x.other and y.other) and not (x.end and y.end)


def _complement(s: _Set) -> _Set:
    return _Set(_ALL_ASCII ^ s.a, True, False)


def _range_set(lo: int, hi: int) -> _Set:
    return _Set(sum(1 << c for c in range(lo, hi + 1)), False, False)


_DIGIT = _range_set(48, 57)
_WORD = _Set(_range_set(48, 57).a | _range_set(65, 90).a | _range_set(97, 122).a | (1 << 95), False, False)
_SPACE = _Set(_range_set(9, 13).a | _range_set(28, 32).a, False, False)
# `\d \w \s` w Pythonie (str) obejmują też znaki spoza ASCII; negacje — tym bardziej.
_CATEGORY = {"d": _DIGIT._replace(other=True), "w": _WORD._replace(other=True), "s": _SPACE._replace(other=True)}
_CATEGORY_NEG = {"D": "d", "W": "w", "S": "s"}


def _category_set(n: str) -> _Set:
    return _CATEGORY[n] if n in _CATEGORY else _complement(_CATEGORY[_CATEGORY_NEG[n]])


def _literal_set(ch: str, fold: bool) -> _Set:
    a = 1 << ord(ch)
    if fold and ch.isascii() and ch.isalpha():
        return _Set(a | 1 << ord(ch.lower()) | 1 << ord(ch.upper()), True, False)   # składanie Unicode
    return _Set(a, False, False)


def _fold_set(s: _Set, fold: bool) -> _Set:
    if not fold:
        return s
    a, other = s.a, s.other
    for c in range(65, 91):
        if s.a >> c & 1 or s.a >> (c + 32) & 1:
            a |= 1 << c | 1 << (c + 32)
            other = True
    return _Set(a, other, s.end)


# --- składnia → drzewo ------------------------------------------------------------------------------
# Węzły: _Atom(set, min, max) | _Anchor(k) | _Look(width) | _Group(branches, optional)

class _Atom:
    __slots__ = ("set", "min", "max")

    def __init__(self, s: _Set) -> None:
        self.set, self.min, self.max = s, 1, 1


class _Anchor(NamedTuple):
    k: str


class _Look(NamedTuple):
    width: int


class _Group:
    __slots__ = ("branches", "optional")

    def __init__(self) -> None:
        self.branches: list[list[Any]] = [[]]
        self.optional = False


def _parse_class(rx: str, i: int, fold: bool) -> tuple[int, _Set]:
    """Klasa od `[` w `i`: (indeks za `]`, zbiór)."""
    j = i + 1
    negated = False
    if _at(rx, j) == "^":
        negated = True
        j += 1
    if _at(rx, j) == "]":
        raise _Reject("forbidden_construct")   # `[]…]`: Python = literał, JS = pusta klasa
    items: list[tuple[str | None, _Set, str | None]] = []   # (literał | None = kategoria, zbiór, surowy znak)
    while j < len(rx) and rx[j] != "]":
        c = rx[j]
        if c == "[":
            raise _Reject("forbidden_construct")
        if c == "\\":
            n = _at(rx, j + 1)
            if n == "":
                raise _Reject("compile")
            if n in _CLASS_ESCAPES:
                items.append((None, _category_set(n), None))
            elif _is_punct(n):
                items.append((n, _literal_set(n, False), None))
            else:
                raise _Reject("forbidden_construct")
            j += 2
        else:
            if c in "-&|~" and _at(rx, j + 1) == c:
                raise _Reject("forbidden_construct")   # możliwe operacje na zbiorach
            items.append((c, _literal_set(c, False), c))
            j += 1
    if j >= len(rx):
        raise _Reject("compile")
    s = _EMPTY
    k = 0
    while k < len(items):
        lit, iset, _raw = items[k]
        # `-` między dwoma pozycjami łączy literały w zakres; `-` zaraz po zakresie jest literałem.
        if k + 2 < len(items) and items[k + 1][2] == "-":
            hi = items[k + 2][0]
            if lit is None or hi is None:
                raise _Reject("forbidden_construct")
            if lit > hi:
                raise _Reject("compile")
            s = _union(s, _range_set(ord(lit), ord(hi)))
            k += 3
            continue
        s = _union(s, iset)
        k += 1
    s = _fold_set(s, fold)
    return j + 1, (_complement(s) if negated else s)


def _quantifier_at(rx: str, i: int) -> tuple[int, float, float] | None:
    """Kwantyfikator w `i`: (długość, min, max) albo None. `{` bez poprawnej postaci = odrzucenie.
    Liczby jak `Number()` w JS (float), żeby ogromne powtórzenia dawały ten sam kod."""
    c = _at(rx, i)
    if c == "*":
        return 1, 0, _INF
    if c == "+":
        return 1, 1, _INF
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


def _consumes_nothing(branch: list[Any]) -> bool:
    """Gałąź bez żadnego atomu (pusta albo same asercje/kotwice) — dopasowuje tylko pusty napis."""
    return not any(isinstance(n, (_Atom, _Group)) for n in branch)


def _parse(rx: Any) -> _Group:
    if not isinstance(rx, str):
        raise _Reject("not_string")
    # Długość jak `length` w JS (jednostki UTF-16), żeby kod był ten sam także dla znaków spoza BMP.
    if len(rx.encode("utf-16-le", "surrogatepass")) // 2 > MAX_REGEX_LEN:
        raise _Reject("length")
    if any(not " " <= ch <= "~" for ch in rx):
        raise _Reject("non_ascii")
    fold = rx.startswith("(?i)")
    i = 4 if fold else 0
    root = _Group()
    stack = [root]

    def seq() -> list[Any]:
        return stack[-1].branches[-1]

    prev = "start"   # atom | group | anchor | quant | start
    assertions = 0   # \b i lookbehind w całym wzorcu
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
                seq()[-1].optional = True
            elif prev == "atom":
                seq()[-1].min, seq()[-1].max = lo, hi
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
                seq().append(_Anchor("b"))
                assertions += 1
                prev = "anchor"
            elif n in _CLASS_ESCAPES:
                seq().append(_Atom(_category_set(n)))
                prev = "atom"
            elif _is_punct(n):
                seq().append(_Atom(_literal_set(n, fold)))
                prev = "atom"
            else:
                raise _Reject("forbidden_construct")   # \1 \N \A \Z \x \u \n …
            i += 2
        elif c == "[":
            end, s = _parse_class(rx, i, fold)
            seq().append(_Atom(s))
            i = end
            prev = "atom"
        elif c == ".":
            seq().append(_Atom(_complement(_literal_set("\n", False))))
            i += 1
            prev = "atom"
        elif c in "^$":
            seq().append(_Anchor(c))
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
                        j = _parse_class(rx, j, fold)[0]
                    elif rx[j] in _META and rx[j] != ".":
                        raise _Reject("forbidden_construct")   # grupa, alternatywa, kwantyfikator, kotwica
                    else:
                        j += 1
                    width += 1
                if j >= len(rx):
                    raise _Reject("compile")
                if width == 0 or width > MAX_LOOKBEHIND:
                    raise _Reject("forbidden_construct")
                seq().append(_Look(width))
                assertions += 1
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
            g = _Group()
            seq().append(g)
            stack.append(g)
            i += length
            prev = "start"
        elif c == ")":
            if len(stack) == 1:
                raise _Reject("compile")
            g = stack.pop()
            if any(_consumes_nothing(b) for b in g.branches):
                raise _Reject("empty_branch")
            i += 1
            prev = "group"
        elif c == "|":
            stack[-1].branches.append([])
            i += 1
            prev = "start"
        elif c in "]}":
            raise _Reject("forbidden_construct")   # literał trzeba poprzedzić `\`
        else:
            seq().append(_Atom(_literal_set(c, fold)))
            i += 1
            prev = "atom"
    if len(stack) != 1:
        raise _Reject("compile")
    if len(root.branches) > 1 and any(_consumes_nothing(b) for b in root.branches):
        raise _Reject("empty_branch")
    if assertions > MAX_ASSERTIONS:
        raise _Reject("too_many_assertions")
    # Ostatnia linia obrony: wzorzec musi się skompilować (każdy wyjątek `re`, np. OverflowError).
    try:
        re.compile(rx)
    except Exception:  # noqa: BLE001
        raise _Reject("compile") from None
    return root


# --- model kosztu ------------------------------------------------------------------------------------
_CAP = INPUT_CAP
_LIMIT = MAX_COST * (_CAP + 1) * 4   # obcięcie iloczynów (nadal > każdego progu)


def _mul(a: int, b: int) -> int:
    return min(a * b, _LIMIT)


def _add(a: int, b: int) -> int:
    return min(a + b, _LIMIT)


def _first_of(items: list[Any], start: int, after: _Set, to: int | None = None) -> tuple[_Set, bool]:
    """(pierwszy zbiór elementów [start, to), czy mogą nic nie dopasować); `after` = to, co po nich.
    `^`, `\\b` i lookbehind są przezroczyste (tylko zawężają)."""
    s = _EMPTY
    for n in items[start:len(items) if to is None else to]:
        if isinstance(n, _Atom):
            s = _union(s, n.set)
            if n.min >= 1:
                return s, False
        elif isinstance(n, _Anchor) and n.k == "$":
            return _union(s, _END_SET), False
        elif isinstance(n, _Group):
            gs, nullable = _group_own(n)
            s = _union(s, gs)
            if not nullable:
                return s, False
    return _union(s, after), True


def _group_own(g: _Group) -> tuple[_Set, bool]:
    s, nullable = _EMPTY, g.optional
    for b in g.branches:
        fs, fn = _first_of(b, 0, _EMPTY)
        s = _union(s, fs)
        nullable = nullable or fn
    return s, nullable


def _required_end(items: list[Any], start: int, to: int) -> int:
    """Indeks pierwszego elementu obowiązkowego od `start` (atom z min ≥ 1, `$`, grupa bez pustej drogi)
    albo ostatni indeks zakresu."""
    for k in range(start, to):
        n = items[k]
        if ((isinstance(n, _Atom) and n.min >= 1) or (isinstance(n, _Anchor) and n.k == "$")
                or (isinstance(n, _Group) and not _group_own(n)[1])):
            return k
    return to - 1


_Memo = dict[tuple[int, int, int], tuple[int, int]]


def _prefix_cost(items: list[Any], start: int, to: int, memo: _Memo) -> tuple[int, int]:
    """Koszt prefiksu dalszej części z `after` = nic — zapamiętany (bez pamięci łańcuch opcjonalnych
    atomów dawał wykładniczo wiele wywołań walidatora). Klucz: tożsamość listy w obrębie jednej analizy."""
    key = (id(items), start, to)
    if key not in memo:
        memo[key] = _cost_of(items, _EMPTY, memo, start, to)
    return memo[key]


def _cost_of(items: list[Any], after: _Set, memo: _Memo, start: int = 0,
             to: int | None = None) -> tuple[int, int]:
    """(liście, praca na liść) elementów [start, to); `after` = pierwszy zbiór tego, co po nich."""
    end = len(items) if to is None else to
    leaves, work = 1, 0
    for k in range(start, end):
        n = items[k]
        if isinstance(n, _Atom):
            top = int(min(n.max, _CAP))
            work = _add(work, top)
            if n.min != n.max:
                w = max(top - int(n.min) + 1, 1)
                if _disjoint(n.set, _first_of(items, k + 1, after, end)[0]):
                    # Dalej przejdzie najwyżej jedna pozycja, ale przy każdym z w cofnięć `sre` wykonuje od
                    # nowa prefiks dalszej części aż do pierwszego elementu obowiązkowego (tam pada).
                    pl, pw = _prefix_cost(items, k + 1, min(_required_end(items, k + 1, end) + 1, end), memo)
                    work = _add(work, _mul(w, _mul(pl, max(pw, 1))))
                else:
                    leaves = _mul(leaves, w)
        elif isinstance(n, _Look):
            work = _add(work, n.width)
        elif isinstance(n, _Anchor):
            work = _add(work, 1)
        else:
            after2 = _first_of(items, k + 1, after, end)[0]
            parts = [(_cost_of(b, after2, memo), _first_of(b, 0, after2)) for b in n.branches]
            exclusive = all(_disjoint(parts[x][1][0], parts[y][1][0])
                            for x in range(len(parts)) for y in range(x + 1, len(parts)))
            if exclusive and n.optional:
                # pominięcie grupy opcjonalnej musi się wykluczać z wejściem do niej
                exclusive = all(not fn and _disjoint(fs, after2) for _, (fs, fn) in parts)
            group_leaves = 0 if exclusive else (1 if n.optional else 0)
            group_work = 0
            for (pl, pw), _f in parts:
                group_leaves = max(group_leaves, pl) if exclusive else _add(group_leaves, pl)
                group_work = max(group_work, pw)
            leaves = _mul(leaves, max(group_leaves, 1))
            work = _add(work, _add(group_work, len(n.branches)))
    return leaves, work


def _anchored(root: _Group) -> bool:
    return all(b and isinstance(b[0], _Anchor) and b[0].k == "^" for b in root.branches)


def regex_cost(rx: Any) -> int | None:
    """Koszt wzorca w modelu albo None (wzorzec spoza podzbioru) — do testów i kalibracji."""
    try:
        return _cost_report(_parse(rx))[0]
    except _Reject:
        return None


def _cost_report(root: _Group) -> tuple[int, int, int]:
    leaves, work = _cost_of([root], _END_ONLY, {})
    per_start = _mul(leaves, max(work, 1))
    starts = 1 if _anchored(root) else _CAP + 1
    return _mul(starts, per_start), per_start, starts


def text_problem(rx: Any) -> str | None:
    """Kod odrzucenia wzorca przez regułę (składnia, kompilacja, koszt) albo None. Port `analyze`."""
    try:
        cost, per_start, starts = _cost_report(_parse(rx))
    except _Reject as rej:
        return rej.code
    if cost > MAX_COST:
        return "too_costly_unanchored" if starts > 1 and per_start <= MAX_COST else "too_costly"
    return None


def identify_issues(doc: Any) -> list[tuple[str, str]]:
    """(ścieżka, kod) problemów reguły identyfikacji — port `identifyIssues`; reguła ładowarki feedu."""
    out: list[tuple[str, str]] = []
    ident = doc.get("identify") if isinstance(doc, dict) else None
    if not isinstance(ident, dict):
        return out
    mr = ident.get("model_regex")
    if mr is not None and not isinstance(mr, list):
        out.append(("$.identify.model_regex", "identify_not_anchored"))
    elif isinstance(mr, list):
        for i, rx in enumerate(mr):
            at = f"$.identify.model_regex[{i}]"
            if not isinstance(rx, str) or not rx.startswith("^"):
                out.append((at, "identify_not_anchored"))
                continue
            if text_problem(rx) is not None:
                continue   # zgłasza to reguła wzorców
            if safe_search(rx, "") is not None or any(safe_search(rx, p) for p in IDENTIFY_PROBES):
                out.append((at, "identify_too_broad"))
    regs = ident.get("registers")
    dt = regs.get("device_type") if isinstance(regs, dict) else None
    expect = dt.get("expect") if isinstance(dt, dict) else None
    if expect is not None:
        # `False` == 0 w zbiorze Pythona, więc też jest „pustym rejestrem”.
        ok = (isinstance(expect, list) and len(expect) <= MAX_DEVICE_TYPE_EXPECT
              and not any(v is False or (isinstance(v, (int, float)) and not isinstance(v, bool)
                                         and v in BLANK_REGISTER_VALUES) for v in expect))
        if not ok:
            out.append(("$.identify.registers.device_type.expect", "identify_expect"))
    return out


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
    reguła tekstowa z kompilacją w Pythonie (kopia Volcast; każdy wyjątek = `compile`) i — przy
    `parse_tree` — druga zapora na drzewie prywatnego parsera `re` (fail closed). Profile z pakietu
    sprawdza się bez drzewa (`parse_tree=False`); feed i test generyczny — z drzewem."""
    code = text_problem(rx)
    if code:
        return code
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
