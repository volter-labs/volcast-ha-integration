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
from custom_components.volcast.core import profile_schema
from custom_components.volcast.core.profile import PROFILES_DIR
from custom_components.volcast.core.profile_schema import validate_profile

UNSAFE = {
    "too_long": "^" + "A" * 250,
    "nested_quantifier": "^(a+)+$",
    "nested_quantifier_noncapturing": "^(?:[0-9a-z]+_)+soc$",
    "nested_quantifier_counted": "^([a-z]+){2,}x",
    "uncompilable": "^(unclosed",
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
    _set(raw, path, UNSAFE[kind])
    errors = validate_profile(raw)
    assert any(e.startswith(_json_path(path) + ":") for e in errors), errors
    with pytest.raises(pf.FeedRejected):
        pf._one_profile(raw)


@pytest.mark.parametrize("kind", sorted(UNSAFE))
def test_shared_rule_flags_unsafe(kind):
    assert profile_schema.regex_problem(UNSAFE[kind])


def test_shared_rule_accepts_every_packaged_regex():
    for path in sorted(PROFILES_DIR.glob("*.json")):
        for where, rx in profile_schema.iter_regex_fields(json.loads(path.read_text(encoding="utf-8"))):
            assert profile_schema.regex_problem(rx) is None, (path.name, where)


def test_feed_loader_uses_the_shared_rule(monkeypatch):
    raw = _raw("sofar-hyd")
    pf._one_profile(raw)
    monkeypatch.setattr(profile_schema, "regex_problem", lambda rx: "flagged")
    assert validate_profile(raw)
    with pytest.raises(pf.FeedRejected):
        pf._one_profile(raw)


def test_regex_fields_found_at_any_depth():
    found = dict(profile_schema.iter_regex_fields(
        {"a": [{"x_regex": ["^a", 5]}], "b": {"c": {"y_regex": "^b"}}, "z_regex": None}))
    assert found == {"$.a[0].x_regex[0]": "^a", "$.b.c.y_regex": "^b"}


# --- Nawroty: wzorce, które przechodziły sam `NESTED_QUANTIFIER`, a w `re.search` trwają sekundy ---
HOSTILE = [
    "^(a|a)+$", "^(a|aa)+$", "^((a+))+$", "^(?:(?:a+))+$", "^((a)+)+$", "^(?:a|a)*$",
    "^(a?){28}a{28}$", ".*.*.*.*=", "a{1,99}a{1,99}a{1,99}a{1,99}=", r"\d*\d*\d*\d*\d*\d*x",
]
# Wrogie wejścia do 200 znaków (limit wzorca; nazwy encji i modele HA są krótsze).
HOSTILE_INPUTS = ["a" * 200, "a" * 199 + "!", "0" * 199 + "!", "a_" * 99 + "!", "A" * 199 + "-",
                  "=" * 50 + "a" * 149 + "!", "aab" * 66 + "!"]
SEARCH_BUDGET_S = 0.5


@pytest.mark.parametrize("rx", HOSTILE)
def test_hostile_patterns_are_rejected(rx):
    assert profile_schema.regex_problem(rx) is not None


def _accepted_patterns() -> list[str]:
    fixture = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "regex-safety.json")
                         .read_text(encoding="utf-8"))
    packaged = [rx for path in sorted(PROFILES_DIR.glob("*.json"))
                for _, rx in profile_schema.iter_regex_fields(json.loads(path.read_text(encoding="utf-8")))]
    return list(fixture["accepted"]) + packaged


def test_accepted_patterns_search_within_budget():
    # Każdy wzorzec, który reguła przyjmuje, kończy `re.search` na wrogich wejściach w budżecie czasu.
    slow = {}
    for rx in _accepted_patterns():
        assert profile_schema.regex_problem(rx) is None, rx
        pattern = re.compile(rx)
        for text in HOSTILE_INPUTS:
            t0 = time.perf_counter()
            pattern.search(text)
            if (dt := time.perf_counter() - t0) > SEARCH_BUDGET_S:
                slow[rx] = dt
    assert not slow, slow
