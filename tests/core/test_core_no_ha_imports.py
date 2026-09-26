"""Rdzeń core/ nie może importować Home Assistanta (warunek warstwy HA)."""
from __future__ import annotations

import ast
import pkgutil
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


def test_core_importable_as_top_level_package_without_homeassistant_stub():
    """Rdzeń musi dać się zaimportować jako pakiet `core` bez pakietu Home Assistant
    zainstalowanego w środowisku i bez atrap z conftest.py — czyli tak, jak
    zrobi to warstwa HA/most, a nie tylko tak, jak robi to pytest."""
    script = (
        "import pkgutil\n"
        "import core\n"
        "names = [core.__name__]\n"
        "for m in pkgutil.walk_packages(core.__path__, core.__name__ + '.'):\n"
        "    __import__(m.name)\n"
        "    names.append(m.name)\n"
        "assert len(names) > 1, 'nie znaleziono modułów rdzenia'\n"
        "print('\\n'.join(sorted(names)))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(INTEGRATION_DIR),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"import rdzenia jako `core` bez atrap HA nie powiódł się:\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "homeassistant" not in result.stdout
