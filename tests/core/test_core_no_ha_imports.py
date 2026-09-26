"""Rdzeń core/ nie może importować Home Assistanta (warunek warstwy HA)."""
from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INTEGRATION_DIR = REPO_ROOT / "custom_components" / "volcast"
CORE = INTEGRATION_DIR / "core"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module)
    return out


def test_core_has_python_files():
    assert list(CORE.rglob("*.py")), "brak plików rdzenia"


def test_no_homeassistant_imports_in_core():
    bad = {
        str(p.relative_to(CORE)): sorted(m for m in _imports(p) if m.split(".")[0] == "homeassistant")
        for p in CORE.rglob("*.py")
    }
    assert {k: v for k, v in bad.items() if v} == {}


def test_core_importable_as_top_level_package_without_homeassistant_stub(tmp_path):
    """Rdzeń musi dać się zaimportować jako pakiet `core` bez pakietu Home Assistant
    i bez atrap z conftest.py — czyli tak, jak zrobi to warstwa HA/most.

    Kopia `core/` w katalogu tymczasowym + tryb izolowany (`-I`: bez cwd na ścieżce,
    bez PYTHON* ze środowiska) — żaden moduł-sąsiad integracji (np. `const`) nie może
    się przypadkiem zaimportować. Po imporcie KAŻDEGO modułu rdzenia w `sys.modules`
    nie może być nic z `homeassistant` (łapie też importy dynamiczne).
    """
    shutil.copytree(CORE, tmp_path / "core",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    script = (
        "import sys, pkgutil\n"
        f"sys.path.insert(0, {str(tmp_path)!r})\n"
        "import core\n"
        f"assert core.__file__.startswith({str(tmp_path)!r}), core.__file__\n"
        "names = [core.__name__]\n"
        "for m in pkgutil.walk_packages(core.__path__, core.__name__ + '.'):\n"
        "    __import__(m.name)\n"
        "    names.append(m.name)\n"
        "assert len(names) > 1, 'nie znaleziono modułów rdzenia'\n"
        "ha = sorted(m for m in sys.modules if m.split('.')[0] == 'homeassistant')\n"
        "assert not ha, f'rdzeń wciągnął Home Assistanta: {ha}'\n"
        "print(len(names))\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"import rdzenia jako `core` bez atrap HA nie powiódł się:\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    # każdy plik .py rdzenia to jeden moduł (pakiet = jego __init__.py)
    expected = sum(1 for f in CORE.rglob("*.py") if "__pycache__" not in f.parts)
    assert int(result.stdout.strip()) == expected
