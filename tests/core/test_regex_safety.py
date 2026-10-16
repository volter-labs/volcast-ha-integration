"""Jedna reguła bezpieczeństwa wzorców `*_regex` dla walidatora profilu i ładowarki feedu.

Profil, który przechodzi `validate_profile` (bramy profili: test generyczny, CI repo Volcast), musi
przejść też ładowarkę feedu — inaczej jeden zły wzorzec odrzuca cały feed u każdego klienta.
"""
import ast
import inspect
import json
import re
import time
from pathlib import Path

import pytest

from custom_components.volcast.core import profile_feed as pf
from custom_components.volcast.core import profile_schema, regex_safety
from custom_components.volcast.core.profile import PROFILES_DIR
from custom_components.volcast.core.profile_schema import validate_profile

# Rodzaj → (wzorzec, kod odrzucenia).
UNSAFE = {
    "length": ("^" + "A" * 250, "length"),
    "group_repeat": ("^(a+)+$", "quantifier_after_group"),
    "noncapturing_group_repeat": ("^(?:[0-9a-z]+_)+soc$", "quantifier_after_group"),
    "open_repeat": ("^([a-z]+){2,}x", "repeat_too_large"),
    "uncompilable": ("^(unclosed", "compile"),
}

# Każde pole `*_regex` schematu → miejsca w profilu pakietu, gdzie występuje (profil bazowy, ścieżka).
# Nowe pole `*_regex` w schemacie bez wpisu tutaj = czerwony `test_every_schema_regex_field_is_covered`.
FIELDS = {
    "model_regex": [
        ("sofar-hyd", ("identify", "model_regex", 0)),
        ("sofar-hyd", ("ha", "integrations", 0, "model_regex", 0)),
    ],
    "unique_id_regex": [
        ("sofar-hyd", ("ha", "integrations", 0, "entities", "soc", "unique_id_regex")),
        ("goodwe-et", ("work_mode", "ha", "unique_id_regex")),
    ],
}
SITES = [(name, base, path) for name, sites in FIELDS.items() for base, path in sites]


def _raw(pid: str) -> dict:
    return json.loads((PROFILES_DIR / f"{pid}.json").read_text(encoding="utf-8"))


def _json_path(path: tuple) -> str:
    return "$" + "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in path)


def _set(raw: dict, path: tuple, value: str) -> None:
    node = raw
    for p in path[:-1]:
        node = node[p]
    node[path[-1]] = value


def _schema_regex_names() -> set[str]:
    """Nazwy pól `*_regex` wpisane w źródło schematu (napisy i f-stringi), nie ręczna lista."""
    tree = ast.parse(inspect.getsource(profile_schema))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            names.update(re.findall(r"([a-z][a-z0-9_]*_regex)\b", node.value))
    return names


def test_every_schema_regex_field_is_covered():
    assert _schema_regex_names() == set(FIELDS)


def test_every_regex_check_in_the_schema_is_a_regex_named_field():
    # Reguła łapie pola po nazwie `*_regex` na każdej głębokości — każde wywołanie `v.regex(...)`
    # musi więc dotyczyć pola o takiej nazwie (albo elementu jego listy).
    tree = ast.parse(inspect.getsource(profile_schema))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "regex"]
    assert calls
    for call in calls:
        path = ast.unparse(call.args[1])
        assert re.search(r"_regex(\[\{\w+\}\])?['\"]$", path), path


@pytest.mark.parametrize("name,base,path", SITES, ids=[_json_path(p) for _, _, p in SITES])
def test_site_exists_and_base_profile_is_accepted(name, base, path):
    raw = _raw(base)
    assert path[-1] == name or path[-2] == name
    node = raw
    for p in path:
        node = node[p]
    assert isinstance(node, str)
    assert validate_profile(raw) == []
    pf._one_profile(raw)


@pytest.mark.parametrize("kind", sorted(UNSAFE))
@pytest.mark.parametrize("name,base,path", SITES, ids=[_json_path(p) for _, _, p in SITES])
def test_unsafe_regex_fails_validation_and_the_feed_loader(name, base, path, kind):
    raw = _raw(base)
    _set(raw, path, UNSAFE[kind][0])
    errors = validate_profile(raw)
    assert any(e.startswith(_json_path(path) + ":") for e in errors), errors
    with pytest.raises(pf.FeedRejected):
        pf._one_profile(raw)


@pytest.mark.parametrize("kind", sorted(UNSAFE))
def test_shared_rule_flags_unsafe(kind):
    rx, code = UNSAFE[kind]
    assert profile_schema.regex_problem(rx) == code
    assert profile_schema.regex_problem(rx, parse_tree=False) == code


def test_shared_rule_accepts_every_packaged_regex():
    for path in sorted(PROFILES_DIR.glob("*.json")):
        for where, rx in profile_schema.iter_regex_fields(json.loads(path.read_text(encoding="utf-8"))):
            assert profile_schema.regex_problem(rx) is None, (path.name, where)


def test_feed_loader_uses_the_shared_rule(monkeypatch):
    raw = _raw("sofar-hyd")
    pf._one_profile(raw)
    monkeypatch.setattr(profile_schema, "regex_problem", lambda rx, **_kw: "flagged")
    assert validate_profile(raw)
    with pytest.raises(pf.FeedRejected):
        pf._one_profile(raw)


def test_regex_fields_found_at_any_depth():
    found = dict(profile_schema.iter_regex_fields(
        {"a": [{"x_regex": ["^a", 5]}], "b": {"c": {"y_regex": "^b"}}, "z_regex": None}))
    assert found == {"$.a[0].x_regex[0]": "^a", "$.b.c.y_regex": "^b"}


# --- Nawroty: wzorce z długim `re.search` na wrogim tekście — wszystkie poza podzbiorem albo limitami ---
HOSTILE = [
    "^(a|a)+$", "^(a|aa)+$", "^((a+))+$", "^(?:(?:a+))+$", "^((a)+)+$", "^(?:a|a)*$",
    "^(a?){28}a{28}$", ".*.*.*.*=", "a{1,99}a{1,99}a{1,99}a{1,99}=", r"\d*\d*\d*\d*\d*\d*x",
    r"\w?" * 18 + r"\w{50}!", ".*.*.*=",
]
# Wrogie teksty długości INPUT_CAP (dłuższe nigdy nie są dopasowywane — `safe_search`).
_N = regex_safety.INPUT_CAP
HOSTILE_INPUTS = ["a" * _N, "a" * (_N - 1) + "!", "1" * (_N - 1) + "!", ("a1" * _N)[:_N - 1] + "!",
                  ("aaaa_" * _N)[:_N - 1] + "!", "_" * (_N - 1) + "!", ("ab" * _N)[:_N - 1] + "!",
                  ("a-" * _N)[:_N - 1] + "!", ("aA1_- " * _N)[:_N - 1] + "!", ("GW1" * _N)[:_N - 1] + "!",
                  "x" * (_N - 1) + "\x00"]
SEARCH_BUDGET_S = 0.25   # pomiar Volcast: najgorszy przyjęty wzorzec ~0,04 s; zapas na obciążone CI


@pytest.mark.parametrize("rx", HOSTILE)
def test_hostile_patterns_are_rejected(rx):
    assert profile_schema.regex_problem(rx) is not None
    assert profile_schema.regex_problem(rx, parse_tree=False) is not None


def _accepted_patterns() -> list[str]:
    fixture = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "regex-safety.json")
                         .read_text(encoding="utf-8"))
    packaged = [rx for path in sorted(PROFILES_DIR.glob("*.json"))
                for _, rx in profile_schema.iter_regex_fields(json.loads(path.read_text(encoding="utf-8")))]
    return list(fixture["accepted"]) + packaged


def test_accepted_patterns_search_within_budget():
    # Każdy wzorzec, który reguła przyjmuje, kończy `re.search` na wrogich tekstach w budżecie czasu.
    slow = {}
    for rx in _accepted_patterns():
        assert profile_schema.regex_problem(rx) is None, rx
        for text in HOSTILE_INPUTS:
            t0 = time.perf_counter()
            regex_safety.safe_search(rx, text)
            if (dt := time.perf_counter() - t0) > SEARCH_BUDGET_S:
                slow[rx] = dt
    assert not slow, slow


# --- Druga zapora: drzewo prywatnego parsera `re` (feed i test generyczny) ---
# Konstrukcje spoza podzbioru, które Python kompiluje — odrzuca je reguła tekstowa.
UNMODELLED = [
    "(?>a+)b", "(?i:a)", "(?-i:a)", "(?x)a b", "(?#c)a", "(?#a)x+", "(a)(?(1)b|c)",
    r"\N{LATIN SMALL LETTER A}", r"x\N{DIGIT ONE}+",
]
# To samo drzewo bez reguły tekstowej (zapora na wypadek różnicy tokenizera i parsera): kod drzewa.
TREE_ONLY = {
    "(?=a)b": "forbidden_construct", "(a)\\1": "forbidden_construct", "(?P<n>a)(?P=n)": "forbidden_construct",
    "(a)(?(1)b)": "forbidden_construct", "(?>a)": "forbidden_construct", "(?i:a)": "forbidden_construct",
    "(?s).": "forbidden_construct", "(?x)a b": "forbidden_construct", "\\Aa": "forbidden_construct",
    "(?=ab)": "forbidden_construct", "a*?": "lazy_quantifier", "a++": "lazy_quantifier",
    "(a|bc)+": "quantifier_after_group", "(ab){2}": "quantifier_after_group", "a{17}": "repeat_too_large",
}


def test_parser_is_available_here():
    # Bez parsera feed odrzuca każdy profil (fail closed) — w CI musi być dostępny.
    assert regex_safety._SRE is not None


@pytest.mark.parametrize("rx", UNMODELLED)
def test_unmodelled_constructs_are_rejected(rx):
    re.compile(rx)                                   # Python je kompiluje — odrzuca dopiero reguła
    assert profile_schema.regex_problem(rx, parse_tree=False) == "forbidden_construct"


@pytest.mark.parametrize("rx", sorted(TREE_ONLY))
def test_parse_tree_guard_alone_rejects(rx):
    assert regex_safety.tree_problem(rx) == TREE_ONLY[rx]


def test_parse_tree_guard_accepts_shared_and_packaged():
    for rx in _accepted_patterns():
        assert regex_safety.tree_problem(rx) is None, rx


def test_missing_parser_fails_closed(monkeypatch):
    monkeypatch.setattr(regex_safety, "_SRE", None)
    assert profile_schema.regex_problem("^GW") == "compile"


def test_unknown_parse_node_fails_closed(monkeypatch):
    sre = regex_safety._SRE
    monkeypatch.setattr(regex_safety, "_SRE", sre._replace(atoms=frozenset()))
    assert profile_schema.regex_problem("^GW") == "forbidden_construct"
    assert profile_schema.regex_problem("^GW", parse_tree=False) is None


@pytest.mark.parametrize("rx", ["a{4294967296}", "a{1,4294967296}", "(?:a){99999999999999999999}"])
def test_huge_repeat_count_is_rejected_not_raised(rx):
    assert profile_schema.regex_problem(rx) is not None


@pytest.mark.parametrize("exc", [OverflowError, RecursionError, MemoryError, RuntimeError])
def test_any_compile_failure_rejects(monkeypatch, exc):
    def boom(*_a, **_k):
        raise exc("x")
    monkeypatch.setattr(regex_safety.re, "compile", boom)
    assert profile_schema.regex_problem("^GW") == "compile"


@pytest.mark.parametrize("exc", [OverflowError, RecursionError, MemoryError, RuntimeError, KeyError])
def test_any_parser_failure_rejects(monkeypatch, exc):
    def boom(*_a, **_k):
        raise exc("x")
    monkeypatch.setattr(regex_safety, "_SRE", regex_safety._SRE._replace(parse=boom))
    assert profile_schema.regex_problem("^GW") == "compile"


@pytest.mark.parametrize("rx", ["^(ab)?c", "^(?:ab|cd)?e", r"\bGW\d{1,3}", "(?i)^gw", "(?<=ab)c", "^x(?<!_m[1-3])y",
                                 "^GW$", r"^[A-Z]{2,4}\d{3,5}"])
def test_parse_tree_guard_accepts_subset_constructs(rx):
    # Konstrukcje dozwolone przez regułę tekstową (grupa opcjonalna, \b, (?i) na początku, lookbehind)
    # przechodzą też zaporę drzewa — inaczej feed odrzucałby profile przyjęte przez bramy Volcast.
    assert regex_safety.text_problem(rx) is None
    assert regex_safety.tree_problem(rx) is None



# Walidator sam nie może się zawiesić: prefiksy modelu kosztu są zapamiętywane (wersja bez pamięci rosła
# wykładniczo na łańcuchach opcjonalnych atomów).
_ALNUM = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
VALIDATOR_STRESS = {
    "optional_atom_chain": "^" + "a?" * 98 + "b",
    # różne znaki: każdy opcjonalny atom rozłączny z dalszą częścią → prefiks za prefiksem (bez pamięci 2^n)
    "distinct_optional_chain": "^" + "".join(c + "?" for c in _ALNUM) + "!",
    "nested_optional_groups": "^" + "(a(b(c(d)?)?)?)?" * 10 + "x",
    "alternating_optional_chain": "^" + "x?y?" * 49 + "z",
    "alternation_of_optionals": "^(?:" + "|".join("a?b?c" for _ in range(15)) + ")" + "d?" * 20 + "e",
}


@pytest.mark.parametrize("shape", sorted(VALIDATOR_STRESS))
def test_validator_decides_long_optional_chains_quickly(shape):
    rx = VALIDATOR_STRESS[shape]
    t0 = time.perf_counter()
    regex_safety.text_problem(rx)
    regex_safety.regex_cost(rx)
    assert len(rx) <= regex_safety.MAX_REGEX_LEN            # ocenia model kosztu, nie limit długości
    assert time.perf_counter() - t0 < 0.3, shape


@pytest.mark.parametrize("rx,code", [("()", "empty_branch"), ("(ab|)", "empty_branch"), ("(|)", "empty_branch"),
                                     ("(\\b)", "empty_branch"), ("a|", "empty_branch"),
                                     ("\\ba" * 9, "too_many_assertions")])
def test_empty_branches_and_assertions(rx, code):
    assert regex_safety.regex_problem(rx) == code


def test_parse_tree_guard_accepts_every_text_accepted_pattern_of_a_corpus():
    # Ziarnisty korpus z gramatyki podzbioru (grupy opcjonalne, alternatywy, \b, lookbehind, (?i), $):
    # każdy wzorzec przyjęty przez regułę tekstową przechodzi zaporę drzewa (feed nie odrzuci go dodatkowo).
    import random
    rnd = random.Random(7)
    atoms = ["a", "b", "1", "_", "-", ".", "[a-z]", "[0-9]", "[^_]", r"\d", r"\w", r"\s", r"\W", r"\."]
    quants = ["", "", "?", "*", "+", "{2}", "{1,3}", "{0,16}"]
    zero = [r"\b", "(?<=a)", "(?<![a-z])", "^", "$"]

    def seq(depth=0):
        out = []
        for _ in range(rnd.randint(1, 5)):
            r = rnd.random()
            if r < 0.2 and depth < 4:
                out.append(rnd.choice(["(", "(?:"]) + "|".join(seq(depth + 1) for _ in range(rnd.randint(1, 3)))
                           + ")" + rnd.choice(["", "?"]))
            elif r < 0.35:
                out.append(rnd.choice(zero))
            else:
                out.append(rnd.choice(atoms) + rnd.choice(quants))
        return "".join(out)

    accepted = 0
    for _ in range(5000):
        rx = rnd.choice(["", "^", "(?i)", "(?i)^"]) + seq()
        if regex_safety.text_problem(rx) is None:
            accepted += 1
            assert regex_safety.tree_problem(rx) is None, rx
    assert accepted > 500



@pytest.mark.parametrize("n,code", [(5, None), (6, None), (7, "too_many_groups")])
def test_group_count_boundary(n, code):
    assert regex_safety.regex_problem("^" + "(?:a)" * n + "b") == code


def test_too_many_groups_wins_over_later_checks():
    # 7. grupa odrzuca od razu — przed pustą gałęzią, asercjami i modelem kosztu; długość i ASCII są wcześniej.
    assert regex_safety.regex_problem("^(\\s*)" + "(c?|d?)" * 10 + "(?<=a)" * 8) == "too_many_groups"
    assert regex_safety.regex_problem("(a)" * 6 + "(|)") == "too_many_groups"
    assert regex_safety.regex_problem("é" + "(a)" * 7) == "non_ascii"
