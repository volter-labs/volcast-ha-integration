"""Wzorce z profili w czasie działania: jeden pomocnik z limitem długości tekstu i niezależność
profili z pakietu od prywatnego parsera `re`."""
import ast
import json
import re
import time
from pathlib import Path

import pytest

from custom_components.volcast.core import profile_feed as pf
from custom_components.volcast.core import regex_safety
from custom_components.volcast.core.profile import PROFILES_DIR, ProfileError, load_profile
from custom_components.volcast.core.profile_schema import validate_profile
from custom_components.volcast.core.regex_safety import INPUT_CAP, safe_match, safe_search

PACKAGE = Path(regex_safety.__file__).resolve().parents[1]
_RE_FUNCS = {"search", "match", "fullmatch", "finditer", "findall", "compile", "sub", "subn", "split"}
# Wzorce budowane z tekstu przepuszczonego przez `re.escape` (seriale, MAC-i z raportu) — nie z profilu.
_ALLOWED = {("core/discovery/report.py", "_serial_pattern"), ("core/discovery/report.py", "_known_mac_pattern")}


def _static(node: ast.AST) -> bool:
    """Wzorzec znany w kodzie: literał albo f-string, którego wstawki to tylko `re.escape(...)`."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return True
    if isinstance(node, ast.JoinedStr):
        return all(isinstance(v, ast.Constant) or (
            isinstance(v, ast.FormattedValue) and isinstance(v.value, ast.Call)
            and isinstance(v.value.func, ast.Attribute) and v.value.func.attr == "escape"
            and isinstance(v.value.func.value, ast.Name) and v.value.func.value.id == "re") for v in node.values)
    return False


def _dynamic_regex_calls() -> list[str]:
    out = []
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE).as_posix()
        if rel == "core/regex_safety.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        owner: dict[ast.AST, str] = {}
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # BFS: funkcja zagnieżdżona nadpisuje właściciela swoich węzłów
                owner.update((n, fn.name) for n in ast.walk(fn) if n is not fn)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "re"
                    and node.func.attr in _RE_FUNCS and node.args and not _static(node.args[0])
                    and (rel, owner.get(node, "<module>")) not in _ALLOWED):
                out.append(f"{rel}:{node.lineno} re.{node.func.attr} in {owner.get(node, '<module>')}")
    return sorted(out)


def test_profile_patterns_reach_re_only_through_the_helper():
    # Wzorzec spoza kodu (profil, feed) trafia do `re` wyłącznie przez `safe_search`/`safe_match`.
    assert _dynamic_regex_calls() == []


def test_helper_truncates_the_subject():
    assert INPUT_CAP == 128
    assert safe_search("^a{128}$", "a" * 128)
    assert safe_search("^a{129}", "a" * 500) is None
    assert safe_search("x", "a" * INPUT_CAP + "x") is None
    assert safe_match("^GW", "GW10K-ET")
    assert safe_match("ET", "GW10K-ET") is None


@pytest.mark.parametrize("rx", [".*.*=", "[a-z]*[a-z]*[a-z]{0,3}x", "^(?:.*a|.*b)(?:.*a|.*b)="])
def test_hostile_pattern_on_long_subject_is_fast(rx):
    assert regex_safety.regex_problem(rx) is None          # przyjęty przez regułę — limit chroni czas
    t0 = time.perf_counter()
    for text in ("a" * 10_000, "b" * 10_000 + "!"):
        safe_search(rx, text)
        safe_match(rx, text)
    assert time.perf_counter() - t0 < 0.2


# --- profile z pakietu nie zależą od prywatnego parsera; feed tak (fail closed) ---

def _no_parser(monkeypatch):
    monkeypatch.setattr(regex_safety, "_SRE", None)


def _broken_parser(monkeypatch):
    def boom(*_a, **_k):
        raise AttributeError("shape changed")
    monkeypatch.setattr(regex_safety, "_SRE", regex_safety._SRE._replace(parse=boom))


@pytest.mark.parametrize("break_parser", [_no_parser, _broken_parser])
def test_packaged_profiles_load_without_the_private_parser(monkeypatch, break_parser):
    break_parser(monkeypatch)
    for path in sorted(PROFILES_DIR.glob("*.json")):
        assert load_profile(path).id == path.stem
        assert validate_profile(json.loads(path.read_text(encoding="utf-8"))) == []


@pytest.mark.parametrize("break_parser", [_no_parser, _broken_parser])
def test_feed_profiles_fail_closed_without_the_private_parser(monkeypatch, break_parser):
    raw = json.loads((PROFILES_DIR / "sofar-hyd.json").read_text(encoding="utf-8"))
    pf._one_profile(raw)
    break_parser(monkeypatch)
    with pytest.raises(pf.FeedRejected):
        pf._one_profile(raw)
    assert validate_profile(raw, parse_tree=True)


def test_parse_tree_only_constructs_reject_feed_but_not_packaged_validation():
    raw = json.loads((PROFILES_DIR / "sofar-hyd.json").read_text(encoding="utf-8"))
    raw["ha"]["integrations"][0]["entities"]["soc"]["unique_id_regex"] = "^(?>sofar)_soc"
    assert validate_profile(raw) == []
    assert validate_profile(raw, parse_tree=True)
    with pytest.raises(pf.FeedRejected):
        pf._one_profile(raw)


def test_load_profile_still_rejects_text_rule_violations():
    raw = json.loads((PROFILES_DIR / "sofar-hyd.json").read_text(encoding="utf-8"))
    raw["ha"]["integrations"][0]["entities"]["soc"]["unique_id_regex"] = "^(a+)+$"
    assert validate_profile(raw)
    with pytest.raises(ProfileError):
        from custom_components.volcast.core.profile import profile_from_dict
        profile_from_dict(raw)


# --- kształt drzewa prywatnego parsera: przypięty, ten sam na każdej wspieranej wersji Pythona ---

def _shape(nodes) -> list:
    out = []
    for op, av in nodes:
        name = str(op)
        if name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"):
            out.append([name, av[0], "MAX" if av[1] == regex_safety._SRE.maxrepeat else av[1], _shape(av[2])])
        elif name == "SUBPATTERN":
            out.append([name, av[0], av[1], av[2], _shape(av[3])])
        elif name == "BRANCH":
            out.append([name, [_shape(b) for b in av[1]]])
        elif name in ("ASSERT", "ASSERT_NOT"):
            out.append([name, av[0], _shape(av[1])])
        elif name == "ATOMIC_GROUP":
            out.append([name, _shape(av)])
        else:
            out.append(name)
    return out


PINNED = {
    "^GW": ["AT", "LITERAL", "LITERAL"],
    "(a|bc)+": [["MAX_REPEAT", 1, "MAX", [["SUBPATTERN", 1, 0, 0, [["BRANCH", [["LITERAL"], ["LITERAL", "LITERAL"]]]]]]]],
    "(a|b)+": [["MAX_REPEAT", 1, "MAX", [["SUBPATTERN", 1, 0, 0, ["IN"]]]]],
    "(?:a+)+": [["MAX_REPEAT", 1, "MAX", [["MAX_REPEAT", 1, "MAX", ["LITERAL"]]]]],
    "a{2,5}?": [["MIN_REPEAT", 2, 5, ["LITERAL"]]],
    "a{,}": [["MAX_REPEAT", 0, "MAX", ["LITERAL"]]],
    "(?=a)(?<!b)c": [["ASSERT", 1, ["LITERAL"]], ["ASSERT_NOT", -1, ["LITERAL"]], "LITERAL"],
    "(?i:a)": [["SUBPATTERN", None, 2, 0, ["LITERAL"]]],
    "(?P<n>a)(?P=n)": [["SUBPATTERN", 1, 0, 0, ["LITERAL"]], "GROUPREF"],
    "(a)(?(1)b|c)": [["SUBPATTERN", 1, 0, 0, ["LITERAL"]], "GROUPREF_EXISTS"],
    "a++": [["POSSESSIVE_REPEAT", 1, "MAX", ["LITERAL"]]],
    "(?>a)": [["ATOMIC_GROUP", ["LITERAL"]]],
    r"[^a]\d.": ["NOT_LITERAL", "IN", "ANY"],
}


@pytest.mark.parametrize("rx", sorted(PINNED))
def test_private_parser_shape_is_pinned(rx):
    assert regex_safety._SRE is not None
    assert _shape(regex_safety._SRE.parse(rx, 0)) == PINNED[rx]


def test_private_parser_flags_are_pinned():
    for rx, flags in (("a", re.UNICODE), ("(?i)a", re.UNICODE | re.IGNORECASE), ("(?a)a", re.ASCII),
                      ("(?x)a b", re.UNICODE | re.VERBOSE)):
        assert regex_safety._SRE.parse(rx, 0).state.flags == int(flags), rx
