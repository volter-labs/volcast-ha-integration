"""Bezpieczeństwo wzorców `*_regex` profilu — jedna reguła dla walidatora profilu i ładowarki feedu.

Wzorce profilu działają w pętli zdarzeń HA, także te z feedu: profil przyjęty przez bramy profili
nie może odrzucić feedu, a wzorzec przyjęty tutaj nie może zawiesić HA nawrotami.
"""
from __future__ import annotations

import re
import warnings
from typing import Any, Iterator, NamedTuple

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
#     na ścieżce dopasowania szerokości się mnożą (alternatywa = SUMA gałęzi, bo każda gałąź to osobne
#     wybory; lookaround jak grupa); iloczyn > MAX_PATH_WIDTH (= 4 × dwa nieograniczone)
#     → `too_many_quantifiers`;
#   - odwołania wsteczne (`\1`–`\9`, `(?P=`) → `backreference`;
#   - kwantyfikator bez atomu, kwantyfikowana kotwica, nawias bez pary → `compile`.
# Arytmetyka całkowita, żeby obie kopie liczyły identycznie.
UNBOUNDED_WIDTH = 256
MAX_PATH_WIDTH = 4 * UNBOUNDED_WIDTH * UNBOUNDED_WIDTH
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
    """Grupa: q = zawiera kwantyfikator zmienny, alt = zawiera `|`, cur/done = iloczyn szerokości
    bieżącej gałęzi / SUMA iloczynów gałęzi już zamkniętych."""
    __slots__ = ("q", "alt", "cur", "done")

    def __init__(self) -> None:
        self.q = False
        self.alt = False
        self.cur = 1
        self.done = 0


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
            width = min(frame.done + frame.cur, _CAP)
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
            top.done = min(top.done + top.cur, _CAP)
            top.cur = 1
            i += 1
        else:
            i = after_atom(i + 1, None)
    if problem is not None:
        return problem
    if len(stack) != 1:
        return "compile"  # grupa bez zamknięcia
    return "too_many_quantifiers" if stack[0].done + stack[0].cur > MAX_PATH_WIDTH else None


# --- struktura na prawdziwym drzewie parsera `re` ---------------------------------------------------
# Ręczny tokenizer wyżej jest kopią reguły Volcast (te same wyniki na tych samych wzorcach), ale może
# czytać składnię inaczej niż Python. O bezpieczeństwie rozstrzyga więc drzewo, które `re` naprawdę
# wykonuje: te same reguły i kody na węzłach parsera. Parser jest prywatnym API (`re._parser`, dawniej
# `sre_parse`): brak modułu, nieznany węzeł albo nieoczekiwany kształt = odrzucenie (fail closed).
# Odrzucane zachowawczo także konstrukcje, których wspólne przypadki nie definiują: grupy atomowe
# i kwantyfikatory zaborcze, flagi w grupie (`(?i:…)`), flagi globalne inne niż i/m/s/a/u (np. `(?x)`),
# komentarze `(?#…)`, `\N{…}` (oba niewidoczne w drzewie) i warunki `(?(1)…)` (= odwołanie wsteczne).
_ALLOWED_FLAGS = int(re.IGNORECASE | re.MULTILINE | re.DOTALL | re.UNICODE | re.ASCII)


class _Sre(NamedTuple):
    parse: Any
    maxrepeat: int
    atoms: frozenset         # szerokość 1, bez wyboru
    repeats: frozenset       # MAX_REPEAT, MIN_REPEAT
    subpattern: Any
    branch: Any
    asserts: frozenset
    backrefs: frozenset
    rejected: frozenset      # konstrukcje spoza reguły (POSSESSIVE_REPEAT, ATOMIC_GROUP)
    failure: Any


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
            atoms=frozenset((c.LITERAL, c.NOT_LITERAL, c.ANY, c.IN, c.AT)),
            repeats=frozenset((c.MAX_REPEAT, c.MIN_REPEAT)), subpattern=c.SUBPATTERN, branch=c.BRANCH,
            asserts=frozenset((c.ASSERT, c.ASSERT_NOT)), backrefs=frozenset((c.GROUPREF, c.GROUPREF_EXISTS)),
            rejected=frozenset(getattr(c, n) for n in ("POSSESSIVE_REPEAT", "ATOMIC_GROUP") if hasattr(c, n)),
            failure=getattr(c, "FAILURE", None))
    except AttributeError:
        return None


_SRE = _load_sre()


class _Reject(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _walk(nodes: Any, sre: _Sre) -> tuple[int, bool]:
    """(iloczyn szerokości, zawiera kwantyfikator zmienny albo `|`) sekwencji węzłów."""
    width, var = 1, False
    for op, av in nodes:
        if op in sre.atoms or (sre.failure is not None and op is sre.failure):
            w, v = 1, False
        elif op in sre.repeats:
            lo, hi, item = av
            iw, iv = _walk(item, sre)
            if hi > 1 and iv:
                raise _Reject("nested_quantifier")
            span = (UNBOUNDED_WIDTH + 1 if hi >= sre.maxrepeat else hi - lo + 1)
            w = _mul(iw, UNBOUNDED_WIDTH if span > UNBOUNDED_WIDTH else max(span, 1))
            v = iv or hi != lo
        elif op is sre.subpattern:
            _group, add_flags, del_flags, p = av
            if add_flags or del_flags:
                raise _Reject("compile")
            w, v = _walk(p, sre)
        elif op is sre.branch:
            w, v = min(sum(_walk(b, sre)[0] for b in av[1]), _CAP), True
        elif op in sre.asserts:
            w, v = _walk(av[1], sre)
        elif op in sre.backrefs:
            raise _Reject("backreference")
        else:  # `rejected` i każdy węzeł nieznany tej wersji reguły
            raise _Reject("compile")
        width, var = _mul(width, w), var or v
    return width, var


def _invisible_in_tree(rx: str) -> bool:
    """`(?#…)` i `\\N{…}` znikają w drzewie parsera — odrzucamy je po tekście (zachowawczo)."""
    if "(?#" in rx:
        return True
    i = 0
    while i < len(rx):
        if rx[i] == "\\":
            if rx[i + 1:i + 2] == "N":
                return True
            i += 2
        else:
            i += 1
    return False


def tree_problem(rx: str) -> str | None:
    """Reguła struktury na drzewie parsera `re` (wzorzec już się kompiluje)."""
    sre = _SRE
    if sre is None or _invisible_in_tree(rx):
        return "compile"
    try:
        tree = sre.parse(rx, 0)
        state = getattr(tree, "state", None) or getattr(tree, "pattern", None)
        if int(state.flags) & ~_ALLOWED_FLAGS:
            return "compile"
        width, _ = _walk(tree, sre)
    except _Reject as rej:
        return rej.code
    except Exception:  # noqa: BLE001 — każdy błąd parsera (także OverflowError, MemoryError) = odrzucenie
        return "compile"
    return "too_many_quantifiers" if width > MAX_PATH_WIDTH else None


def regex_problem(rx: Any, *, parse_tree: bool = True) -> str | None:
    """Kod powodu odrzucenia wzorca (`not_string`, `length`, `nested_quantifier`, `compile`,
    `too_many_quantifiers`, `backreference`) albo None, gdy wzorzec jest bezpieczny. Wzorzec musi
    przejść regułę tekstową (kopia Volcast) i — przy `parse_tree` — tę samą regułę na drzewie
    prywatnego parsera `re` (fail closed). Profile z pakietu sprawdza się bez drzewa (`parse_tree=False`):
    ich ładowanie nie może zależeć od prywatnego API; feed i bramy CI sprawdzają z drzewem."""
    if not isinstance(rx, str):
        return "not_string"
    if len(rx) > MAX_REGEX_LEN:
        return "length"
    if NESTED_QUANTIFIER.search(rx):
        return "nested_quantifier"
    try:
        re.compile(rx)
    except Exception:  # noqa: BLE001 — nie tylko re.error: np. `a{4294967296}` → OverflowError
        return "compile"
    return structure_problem(rx) or (tree_problem(rx) if parse_tree else None)


# --- dopasowanie w czasie działania ---------------------------------------------------------------
# Każdy wzorzec z danych profilu (pakiet albo feed) trafia do `re` WYŁĄCZNIE tędy (pilnuje test AST).
# Tekst dłuższy niż INPUT_CAP znaków = BRAK dopasowania (fail closed), nigdy dopasowanie do jego
# początku: czas nawrotów wzorca przyjętego przez regułę rośnie z długością tekstu, a przycięcie
# zmieniłoby znaczenie dopasowania (urządzenie uznane za inne). Modele i unique_id encji HA są
# wielokrotnie krótsze (test na danych wzorcowych). `re` trzyma skompilowane wzorce w swojej pamięci.
INPUT_CAP = 128


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
