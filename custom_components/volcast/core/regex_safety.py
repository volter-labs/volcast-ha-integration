"""Bezpieczeństwo wzorców `*_regex` profilu — jedna reguła dla walidatora profilu i ładowarki feedu.

Wzorce profilu działają w pętli zdarzeń HA, także te z feedu: profil przyjęty przez bramy profili
nie może odrzucić feedu, a wzorzec przyjęty tutaj nie może zawiesić HA nawrotami.
"""
from __future__ import annotations

import re
from typing import Any, Iterator

REGEX_KEY_SUFFIX = "_regex"
MAX_REGEX_LEN = 200
# Grupa z kwantyfikatorem, sama pod kwantyfikatorem: `(a+)+`, `(.*)*`, `([a-z]+){2,}`.
NESTED_QUANTIFIER = re.compile(r"\((?:[^()\\]|\\.)*(?:[*+]|\{\d*,\d*\})(?:[^()\\]|\\.)*\)(?:[*+]|\{\d*,?\d*\})")

# --- struktura (nawroty): tokenizer według składni Pythona `re` -------------------------------------
# Sam `NESTED_QUANTIFIER` przepuszcza wykładnicze nawroty (`((a+))+`, `(a|a)+`, `(a?){28}a{28}`) i
# wielomiany wysokiego stopnia (`.*.*.*.*=`). Zachowawczo, 1:1 z bramami Volcast
# (`scripts/profiles/regex-safety.mjs` `structureProblem`, stałe w `profiles/schema-lite.json`
# `x-regex-safety`, przypadki wspólne w `profiles/fixtures/regex-safety.json`):
#   - grupa pod kwantyfikatorem POWTARZAJĄCYM (max > 1), która na dowolnej głębokości zawiera
#     kwantyfikator o zmiennej liczbie powtórzeń (`*`, `+`, `?`, `{m,n}` z m < n; nie `{n}`) albo `|`
#     → `nested_quantifier`;
#   - szerokość kwantyfikatora = max − min + 1 (nieograniczony albo > UNBOUNDED_WIDTH → UNBOUNDED_WIDTH);
#     na ścieżce dopasowania szerokości się mnożą (alternatywa = maksimum gałęzi, lookaround jak grupa);
#     iloczyn > MAX_PATH_WIDTH → `too_many_quantifiers`;
#   - odwołania wsteczne (`\1`–`\9`, `(?P=`) → `backreference`;
#   - kwantyfikator bez atomu, kwantyfikowana kotwica, nawias bez pary → `compile`.
# Arytmetyka całkowita, żeby obie kopie liczyły identycznie.
UNBOUNDED_WIDTH = 256
MAX_PATH_WIDTH = UNBOUNDED_WIDTH * UNBOUNDED_WIDTH
_CAP = MAX_PATH_WIDTH + 1  # iloczyny obcinamy tutaj
_INF = float("inf")
_BRACE = re.compile(r"\{([0-9]*)(,?)([0-9]*)\}")
# Przedrostek grupy po `(`: `?:`, `?=`, `?!`, `?<=`, `?<!`, `?P<n>`, `?<n>`, `?flagi:`/`?flagi)`.
_GROUP_PREFIX = re.compile(r"\?(?::|=|!|<=|<!|P?<[^>]*>|[a-zA-Z-]*:|[a-zA-Z-]*(?=\)))")


def _mul(a: int, b: int) -> int:
    return min(a * b, _CAP)


def _quantifier_at(rx: str, i: int) -> tuple[int, float, float, int] | None:
    """Kwantyfikator w `i`: (długość, min, max, szerokość) albo None."""
    c = rx[i:i + 1]
    if c in ("*", "+"):
        length, lo, hi = 1, (1 if c == "+" else 0), _INF
    elif c == "?":
        length, lo, hi = 1, 0, 1
    elif c == "{":
        m = _BRACE.match(rx, i)
        if not m or m.group(0) == "{}":  # `{}` = literał
            return None
        lo = int(m.group(1)) if m.group(1) else 0
        hi = (_INF if m.group(3) == "" else int(m.group(3))) if m.group(2) else lo
        length = len(m.group(0))
    else:
        return None
    if rx[i + length:i + length + 1] == "?":  # leniwy
        length += 1
    span = hi - lo + 1
    width = UNBOUNDED_WIDTH if span > UNBOUNDED_WIDTH else max(int(span), 1)
    return length, lo, hi, width


def _class_end(rx: str, i: int) -> int:
    """Indeks `]` zamykającego klasę z `[` w `i` (składnia Pythona) albo -1."""
    j = i + 2 if rx[i + 1:i + 2] == "^" else i + 1
    if rx[j:j + 1] == "]":  # pierwszy `]` = literał
        j += 1
    while j < len(rx) and rx[j] != "]":
        j += 2 if rx[j] == "\\" else 1
    return j if j < len(rx) else -1


class _Frame:
    """Grupa: q = zawiera kwantyfikator zmienny, alt = zawiera `|`, cur/best = iloczyn szerokości
    bieżącej gałęzi / największy iloczyn gałęzi już zamkniętych."""
    __slots__ = ("q", "alt", "cur", "best")

    def __init__(self) -> None:
        self.q = False
        self.alt = False
        self.cur = 1
        self.best = 1


def structure_problem(rx: str) -> str | None:
    stack = [_Frame()]
    problem: str | None = None

    def after_atom(i: int, frame: _Frame | None) -> int:
        # Po atomie (albo zamknięciu grupy `frame`): kwantyfikator i propagacja do grupy nadrzędnej.
        nonlocal problem
        top = stack[-1]
        width = 1
        if frame is not None:
            top.alt = top.alt or frame.alt
            top.q = top.q or frame.q
            width = max(frame.best, frame.cur)
        q = _quantifier_at(rx, i)
        if q:
            length, lo, hi, qwidth = q
            if frame is not None and hi > 1 and (frame.q or frame.alt) and problem is None:
                problem = "nested_quantifier"
            width = _mul(width, qwidth)
            if hi != lo:  # `{n}` = stała liczba powtórzeń, bez wyboru → bez nawrotów
                top.q = True
        top.cur = _mul(top.cur, width)
        return i + q[0] if q else i

    i = 0
    while i < len(rx) and problem is None:
        c = rx[i]
        nxt = rx[i + 1:i + 2]
        # Kwantyfikator tam, gdzie oczekiwany jest atom (początek, po `(`, `|`, po kwantyfikatorze).
        if _quantifier_at(rx, i):
            return "compile"
        if c in ("^", "$") or (c == "\\" and nxt != "" and nxt in "AZbB"):
            i += 2 if c == "\\" else 1
            if _quantifier_at(rx, i):  # kotwica pod kwantyfikatorem
                return "compile"
        elif c == "\\":
            if "1" <= nxt <= "9":
                return "backreference"
            i = after_atom(i + 2, None)
        elif c == "[":
            j = _class_end(rx, i)
            if j < 0:
                return None  # niezamknięta klasa: odrzuca kompilacja
            i = after_atom(j + 1, None)
        elif c == "(":
            if rx.startswith("(?P=", i):
                return "backreference"
            stack.append(_Frame())
            m = _GROUP_PREFIX.match(rx, i + 1)
            i += 1 + (len(m.group(0)) if m else (1 if nxt == "?" else 0))
        elif c == ")":
            if len(stack) == 1:
                return "compile"  # nawias bez pary
            i = after_atom(i + 1, stack.pop())
        elif c == "|":
            top = stack[-1]
            top.alt = True
            top.best = max(top.best, top.cur)
            top.cur = 1
            i += 1
        else:
            i = after_atom(i + 1, None)
    if problem is not None:
        return problem
    if len(stack) != 1:
        return "compile"  # grupa bez zamknięcia
    return "too_many_quantifiers" if max(stack[0].best, stack[0].cur) > MAX_PATH_WIDTH else None


def regex_problem(rx: Any) -> str | None:
    """Kod powodu odrzucenia wzorca (`not_string`, `length`, `nested_quantifier`, `compile`,
    `too_many_quantifiers`, `backreference`) albo None, gdy wzorzec jest bezpieczny."""
    if not isinstance(rx, str):
        return "not_string"
    if len(rx) > MAX_REGEX_LEN:
        return "length"
    if NESTED_QUANTIFIER.search(rx):
        return "nested_quantifier"
    try:
        re.compile(rx)
    except re.error:
        return "compile"
    return structure_problem(rx)


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
